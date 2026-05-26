"""
Persistent homology for call graphs.

Public API::

    compute_persistence(G, weight_attr="weight") -> PersistenceResult

Uses `gudhi` (preferred) or falls back to the NetworkX-based β₁ count from
`pact_tda._beta1` when neither gudhi nor pyflagser is available.

**Filtration design**

Edge weights are derived from betweenness centrality: a high-centrality edge
appears early in the filtration (low value) because it carries more structural
load.  The formula is::

    filtration_value(edge) = 1 / (edge_betweenness_centrality + ε)

where ε = 1e-6 prevents division by zero.  Edges with no centrality data (e.g.
isolated pairs) default to 1.0.

Two-simplices (triangles) are inserted at the maximum filtration value of their
three edges.  This follows the clique/flag complex construction: a triangle is
filled as soon as all three edges are present and the heaviest one has appeared.

**What the bars mean**

- An H1 bar (b, d) with d < ∞ represents a cycle that forms at filtration
  value b and is filled by a triangle at value d.  Persistence = d − b.
  A long bar is a topologically robust cycle; a short bar may be noise.

- An H1 bar (b, ∞) is an *essential* class — the cycle is never filled.
  These count toward β₁.

**Fragility score**

``total_persistence_h1`` — the sum of finite H1 persistences — replaces the
raw β₁ count used previously in ``sheaf_summary``.  It weights long-lived
cycles heavily (structural fragility) and discounts transient noise cycles.

Implementation notes
---------------------
- Directed graph → undirected (ignoring directionality for topology).
- Isolated nodes (no edges) contribute β₀ but not β₁.
- The fallback path (no gudhi) uses the Euler-characteristic formula
  β₁ = E − V + β₀ via Union-Find on the undirected edge set.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

try:
    import networkx as nx

    _HAS_NX = True
except ImportError:
    _HAS_NX = False

try:
    import gudhi  # type: ignore[import]

    _HAS_GUDHI = True
except ImportError:
    _HAS_GUDHI = False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PersistencePair:
    """A single birth-death pair from a persistence diagram."""

    dimension: int  # 0 = components, 1 = cycles, 2 = voids …
    birth: float
    death: float  # math.inf means the class never dies (essential)

    @property
    def persistence(self) -> float:
        """death − birth.  Returns math.inf for essential classes."""
        return self.death - self.birth


@dataclass
class PersistenceResult:
    """
    Full persistence diagram for a call graph.

    Attributes
    ----------
    pairs:
        All birth-death pairs, ordered by dimension then birth.
    total_persistence_h1:
        Sum of finite H1 persistence values.  The primary fragility score —
        replaces the raw β₁ count used in previous versions of sheaf_summary.
    beta1:
        Count of *essential* H1 classes (infinite bars) — the true β₁.
    beta0:
        Number of connected components (essential H0 classes).
    backend:
        ``"gudhi"`` or ``"nx_fallback"`` — which implementation was used.
    """

    pairs: list[PersistencePair] = field(default_factory=list)
    total_persistence_h1: float = 0.0
    beta1: int = 0
    beta0: int = 1
    backend: str = "nx_fallback"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def h1_pairs(self) -> list[PersistencePair]:
        """Return only the H1 pairs."""
        return [p for p in self.pairs if p.dimension == 1]

    def render(self) -> str:
        """Human-readable persistence diagram summary (markdown-friendly)."""
        lines = [
            f"## Persistence Diagram (backend={self.backend})",
            f"- β₀ = {self.beta0}  (connected components)",
            f"- β₁ = {self.beta1}  (essential cycles)",
            f"- total_persistence_H1 = {self.total_persistence_h1:.4f}",
        ]
        h1 = self.h1_pairs()
        if h1:
            lines.append("")
            lines.append("### H1 bars")
            for p in sorted(h1, key=lambda x: x.persistence, reverse=True):
                d_str = "∞" if math.isinf(p.death) else f"{p.death:.4f}"
                pers_str = "∞" if math.isinf(p.persistence) else f"{p.persistence:.4f}"
                lines.append(f"  birth={p.birth:.4f}  death={d_str}  pers={pers_str}")
        else:
            lines.append("- (no H1 bars)")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialisable representation for JSON output / sheaf_summary."""
        return {
            "beta0": self.beta0,
            "beta1": self.beta1,
            "total_persistence_h1": self.total_persistence_h1,
            "backend": self.backend,
            "h1_bars": [
                {
                    "birth": p.birth,
                    "death": p.death if not math.isinf(p.death) else None,
                    "persistence": (
                        p.persistence if not math.isinf(p.persistence) else None
                    ),
                }
                for p in self.h1_pairs()
            ],
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _edge_filtration(G: "nx.DiGraph", weight_attr: str) -> dict[tuple, float]:
    """
    Return a mapping ``{(u, v): filtration_value}`` for the undirected edge set.

    Strategy:
      1. Use provided edge weights if weight_attr exists.
      2. Otherwise fall back to 1 / (betweenness + ε) so high-centrality edges
         appear early in the filtration.
      3. For parallel directed edges (u→v AND v→u), take the minimum filtration
         value (i.e. the edge appears as soon as either direction does).
    """
    if G.number_of_edges() == 0:
        return {}

    # Check whether the graph has custom weights
    has_custom_weight = any(weight_attr in data for _, _, data in G.edges(data=True))

    if has_custom_weight:
        raw: dict[tuple, float] = {}
        for u, v, data in G.edges(data=True):
            e = (min(u, v), max(u, v))
            w = float(data.get(weight_attr, 1.0))
            raw[e] = min(raw.get(e, w), w)
        return raw

    # Use betweenness centrality
    bc = nx.edge_betweenness_centrality(G)
    eps = 1e-6
    raw = {}
    for (u, v), centrality in bc.items():
        e = (min(u, v), max(u, v))
        val = 1.0 / (centrality + eps)
        raw[e] = min(raw.get(e, val), val)
    return raw


def _build_simplex_tree(
    G: "nx.DiGraph",
    edge_filt: dict[tuple, float],
) -> "gudhi.SimplexTree":
    """
    Build a gudhi SimplexTree (flag/clique complex) from edge filtrations.

    Vertices are inserted at filtration 0.  Edges at their filtration value.
    Triangles at max(3 edge filtration values) — standard flag complex.
    Higher simplices are not needed for H0/H1.

    gudhi requires integer vertex labels; we map arbitrary node labels to ints.
    """
    # Map arbitrary node labels → integer indices (gudhi requires Sequence[int])
    node_to_idx = {n: i for i, n in enumerate(G.nodes())}

    st = gudhi.SimplexTree()

    for node in G.nodes():
        st.insert([node_to_idx[node]], filtration=0.0)

    for (u, v), filt in edge_filt.items():
        st.insert([node_to_idx[u], node_to_idx[v]], filtration=filt)

    # Add triangles: iterate over all 3-cliques in the undirected edge set
    undirected = nx.Graph()
    for (u, v), filt in edge_filt.items():
        undirected.add_edge(u, v, weight=filt)

    for clique in nx.enumerate_all_cliques(undirected):
        if len(clique) == 3:
            pairs = [(clique[i], clique[j]) for i in range(3) for j in range(i + 1, 3)]
            triangle_filt = max(undirected[a][b]["weight"] for a, b in pairs)
            st.insert([node_to_idx[n] for n in clique], filtration=triangle_filt)

    st.make_filtration_non_decreasing()
    return st


def _parse_gudhi_diagram(
    st: "gudhi.SimplexTree",
) -> PersistenceResult:
    """
    Run gudhi persistence and parse into a PersistenceResult.

    persistence_dim_max=True is required so gudhi computes H1 on 1-dimensional
    complexes (without it, gudhi only computes up to max_dim-1 = H0).
    homology_coeff_field=2 uses GF(2) — correct for call-graph topology.
    """
    # persistence_dim_max=True: compute H1 on 1-dimensional complexes
    # (default only goes up to max_dim-1 = H0 for edge-only complexes).
    # Use persistence_intervals_in_dimension() — st.persistence() returns a
    # stale cache when called after compute_persistence() with different flags.
    st.compute_persistence(homology_coeff_field=2, persistence_dim_max=True)

    pairs: list[PersistencePair] = []
    for dim in range(2):
        for birth, death in st.persistence_intervals_in_dimension(dim):
            d = death if not math.isinf(death) else math.inf
            pairs.append(PersistencePair(dimension=dim, birth=float(birth), death=d))

    h1 = [p for p in pairs if p.dimension == 1]
    total_h1 = sum(p.persistence for p in h1 if not math.isinf(p.persistence))
    beta1 = sum(1 for p in h1 if math.isinf(p.persistence))
    # β₀ = number of essential H0 bars
    beta0 = sum(1 for p in pairs if p.dimension == 0 and math.isinf(p.persistence))
    beta0 = max(beta0, 1)  # at least 1 component for non-empty graphs

    return PersistenceResult(
        pairs=pairs,
        total_persistence_h1=total_h1,
        beta1=beta1,
        beta0=beta0,
        backend="gudhi",
    )


def _nx_fallback(G: "nx.DiGraph") -> PersistenceResult:
    """
    Compute β₀ and β₁ via Euler characteristic (Union-Find).

    No filtration — only produces β counts, not bars.
    total_persistence_h1 is set to float(beta1) for backward compatibility
    so that downstream code gets the same qualitative ordering as before.
    """
    nodes: set = set(G.nodes())
    edges: set = {(min(u, v), max(u, v)) for u, v in G.edges()}

    V = len(nodes)
    E = len(edges)
    if V == 0:
        return PersistenceResult(backend="nx_fallback")

    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    comps = V
    for u, v in edges:
        pu, pv = find(u), find(v)
        if pu != pv:
            parent[pu] = pv
            comps -= 1

    beta0 = comps
    beta1 = max(E - V + beta0, 0)

    # Synthesise trivial H1 pairs so beta1 is reflected in pairs list
    pairs = [
        PersistencePair(dimension=1, birth=0.0, death=math.inf) for _ in range(beta1)
    ]
    pairs += [
        PersistencePair(dimension=0, birth=0.0, death=math.inf) for _ in range(beta0)
    ]

    return PersistenceResult(
        pairs=pairs,
        total_persistence_h1=float(beta1),  # proxy: no filtration available
        beta1=beta1,
        beta0=beta0,
        backend="nx_fallback",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_persistence(
    G: "nx.DiGraph",
    weight_attr: str = "weight",
) -> PersistenceResult:
    """
    Compute the persistent homology of a directed call graph.

    Parameters
    ----------
    G:
        A ``networkx.DiGraph``.  Node labels may be any hashable type.
    weight_attr:
        Edge attribute name used as the filtration weight.  If no edges
        carry this attribute, betweenness centrality is used instead.

    Returns
    -------
    PersistenceResult
        With ``total_persistence_h1`` as the primary fragility score.

    Fallback
    --------
    If ``gudhi`` is not installed, the function degrades gracefully to the
    β₁-count path (Euler characteristic via Union-Find).  The result will
    have ``backend="nx_fallback"`` and ``total_persistence_h1 == float(beta1)``.
    """
    if not _HAS_NX:
        raise ImportError("networkx is required for compute_persistence")

    if G.number_of_nodes() == 0:
        return PersistenceResult(
            beta0=0, backend="gudhi" if _HAS_GUDHI else "nx_fallback"
        )

    if not _HAS_GUDHI:
        return _nx_fallback(G)

    edge_filt = _edge_filtration(G, weight_attr)
    if not edge_filt:
        # No edges — only isolated nodes
        beta0 = G.number_of_nodes()
        return PersistenceResult(
            pairs=[
                PersistencePair(dimension=0, birth=0.0, death=math.inf)
                for _ in range(beta0)
            ],
            total_persistence_h1=0.0,
            beta1=0,
            beta0=beta0,
            backend="gudhi",
        )

    st = _build_simplex_tree(G, edge_filt)
    return _parse_gudhi_diagram(st)
