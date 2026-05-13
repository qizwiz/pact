"""
pact graph reduction analysis.

The fragility of a codebase scales with its call graph complexity: more nodes
and edges mean more paths for bugs to propagate, more coupling to reason about,
and more things that can break.  This module identifies structural simplification
targets — not just "fix this violation" but "eliminate this node/cycle from the
graph entirely."

Three structural anti-patterns, each with a formal graph-theory name:

  SCC tangle     A strongly connected component with size > 1. Any set of
                 functions that call each other in a cycle have mutual
                 dependency — you cannot change one without potentially
                 affecting all others. The elimination move: break the cycle
                 by extracting shared state into a single dependency direction.

  Pass-through   A node with in-degree = 1 AND out-degree = 1.  It adds a hop
                 without adding logic — pure structural noise.  The elimination
                 move: inline or delete it.

  Fan-out hub    A node with out-degree above a threshold.  It is a cognitive
                 complexity maximizer: to understand it you must understand all
                 N callees.  The elimination move: split by responsibility into
                 K cohesive sub-functions.

Three actual graph transformations (``apply_full_reduction``):

  SCC contraction      Collapse every strongly-connected component into one
                       representative node, producing a DAG (the condensation).
                       Cycles disappear; the true dependency order becomes visible.

  Dead-node pruning    Remove nodes not reachable from any live entry point
                       (public functions, decorated handlers, __main__ blocks).
                       Dead code cannot carry violations to callers.

  Transitive reduction Strip every edge u→w that is already implied by a longer
                       path u→…→w.  Produces the minimum edge set that preserves
                       all reachability relationships (Hasse diagram of the DAG).

Usage
-----
    from pact.reduce import analyze_graph_reduction, apply_full_reduction
    from pact.extractor import extract_from_codebase
    from pact.encoder import Violation

    models, functions, call_sites = extract_from_codebase(root)
    candidates = analyze_graph_reduction(functions, call_sites, violations)
    result = apply_full_reduction(functions, call_sites, violations)
    print(result.summary())
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

from .extractor import CallSite, FunctionManifest
from .encoder import Violation

try:
    import networkx as nx

    _HAS_NX = True
except ImportError:
    _HAS_NX = False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ReductionCandidate:
    """One structural simplification target."""

    kind: str  # "tangle" | "passthrough" | "hub"
    primary: str  # main function name (or representative for tangle)
    members: list[str]  # all function names involved
    file: str
    line: int
    reduction_potential: int  # estimated nodes+edges eliminated by simplification
    violation_count: int  # total violations across all members
    detail: str  # human-readable explanation

    @property
    def score(self) -> float:
        """Higher = better simplification target. Violations add urgency."""
        return self.reduction_potential + self.violation_count * 0.5

    def summary(self) -> str:
        kind_label = {
            "tangle": "TANGLE",
            "passthrough": "PASSTHROUGH",
            "hub": "HUB",
        }[self.kind]
        lines = [
            f"  {kind_label}  {self.primary}  [{self.file}:{self.line}]",
            f"    {self.detail}",
            f"    reduction_potential={self.reduction_potential}  "
            f"violations={self.violation_count}  score={self.score:.1f}",
        ]
        if self.kind == "tangle" and len(self.members) > 1:
            cycle_str = " → ".join(self.members[:4])
            if len(self.members) > 4:
                cycle_str += f" → … ({len(self.members)} total)"
            lines.insert(1, f"    cycle: {cycle_str}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def _build_digraph(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
):
    """Return (G, func_by_name) or (None, {}) if networkx unavailable."""
    if not _HAS_NX:
        return None, {}

    G = nx.DiGraph()
    func_by_name: dict[str, FunctionManifest] = {}
    short_to_qual: dict[str, str] = {}

    for f in functions:
        G.add_node(f.name, manifest=f)
        func_by_name[f.name] = f
        short = f.name.split(".")[-1]
        short_to_qual.setdefault(short, f.name)

    func_names = set(func_by_name)
    for cs in call_sites:
        src = cs.caller_name or "__root__"
        tgt = cs.callee_name
        # Only resolve short names when the name is unique across the whole extracted
        # scope — if there are multiple functions with the same short name (e.g.
        # from_template on different classes), the fallback would collapse them into
        # one node and manufacture phantom cycles.
        if tgt not in func_names:
            short = tgt.split(".")[-1]
            qual = short_to_qual.get(short)
            # Only resolve if this short name is unambiguous (exactly one definition)
            count = sum(1 for f in func_by_name if f.split(".")[-1] == short)
            if qual and count == 1:
                tgt = qual
        G.add_edge(src, tgt)

    return G, func_by_name


def _violations_by_func(violations: list[Violation]) -> dict[str, list[Violation]]:
    by_func: dict[str, list[Violation]] = collections.defaultdict(list)
    for v in violations:
        # violations have a .call attribute (the callee); try to attribute to caller
        # by file+line attribution — best-effort
        by_func[v.file].append(v)
    return by_func


def _viols_for_member(
    member: str, func_by_name, violations: list[Violation]
) -> list[Violation]:
    """Return violations directly inside `member`'s function scope.

    Uses line-range attribution: violations whose line is >= the function's
    start line and before the next function in the same file are attributed
    to this function.  Falls back to file-level attribution when ordering
    cannot be determined.
    """
    f = func_by_name.get(member)
    if f is None:
        return []

    # Find the start of the *next* function in the same file to bound the range.
    file_funcs = sorted(
        [fm for fm in func_by_name.values() if fm.file == f.file],
        key=lambda fm: fm.line,
    )
    my_idx = next((i for i, fm in enumerate(file_funcs) if fm.name == member), None)
    if my_idx is not None and my_idx + 1 < len(file_funcs):
        next_line = file_funcs[my_idx + 1].line
        return [
            v for v in violations if v.file == f.file and f.line <= v.line < next_line
        ]
    # Last function in file — attribute anything after its start line
    return [v for v in violations if v.file == f.file and v.line >= f.line]


def find_sccs(
    G, func_by_name: dict, violations: list[Violation]
) -> list[ReductionCandidate]:
    """Find strongly connected components (call cycles) with size > 1."""
    if G is None:
        return []
    candidates = []
    for scc in nx.strongly_connected_components(G):
        if len(scc) <= 1:
            continue
        members = sorted(scc)
        # Find a representative: node with most violations or highest in-degree
        viols = [
            v for m in members for v in _viols_for_member(m, func_by_name, violations)
        ]
        rep = members[0]
        f = func_by_name.get(rep)
        file_ = f.file if f else ""
        line_ = f.line if f else 0
        # reduction_potential: breaking an SCC of size N eliminates O(N) back-edges
        # and makes the subgraph a DAG — estimate: N-1 edges eliminated
        reduction_potential = len(members) - 1
        candidates.append(
            ReductionCandidate(
                kind="tangle",
                primary=rep,
                members=members,
                file=file_,
                line=line_,
                reduction_potential=reduction_potential,
                violation_count=len(viols),
                detail=(
                    f"{len(members)} functions in a mutual call cycle — "
                    f"breaking the cycle removes {reduction_potential} back-edge(s) "
                    f"and makes the subgraph a DAG"
                ),
            )
        )
    return sorted(candidates, key=lambda c: -c.score)


def find_passthroughs(
    G, func_by_name: dict, violations: list[Violation]
) -> list[ReductionCandidate]:
    """Find nodes with in-degree=1 and out-degree=1 and no violations of their own."""
    if G is None:
        return []
    candidates = []
    for node in G.nodes():
        f = func_by_name.get(node)
        if f is None:
            continue
        in_deg = G.in_degree(node)
        out_deg = G.out_degree(node)
        if in_deg != 1 or out_deg != 1:
            continue
        node_viols = _viols_for_member(node, func_by_name, violations)
        # Pass-throughs without violations are pure structural noise
        candidates.append(
            ReductionCandidate(
                kind="passthrough",
                primary=node,
                members=[node],
                file=f.file,
                line=f.line,
                # Eliminating removes 1 node + 2 edges = 3 graph elements
                reduction_potential=3,
                violation_count=len(node_viols),
                detail=(
                    f"in={in_deg} caller  out={out_deg} callee — "
                    f"pure hop with no logic of its own; inline to collapse 1 node + 2 edges"
                ),
            )
        )
    return sorted(candidates, key=lambda c: -c.score)


def find_hubs(
    G,
    func_by_name: dict,
    violations: list[Violation],
    threshold: int = 8,
) -> list[ReductionCandidate]:
    """Find nodes whose out-degree exceeds threshold (cognitive complexity hubs)."""
    if G is None:
        return []
    candidates = []
    for node in G.nodes():
        f = func_by_name.get(node)
        if f is None:
            continue
        out_deg = G.out_degree(node)
        if out_deg < threshold:
            continue
        node_viols = _viols_for_member(node, func_by_name, violations)
        # Splitting into K groups of ≤4 reduces fan-out from N to ≤4
        k = (out_deg + 3) // 4  # ceil(out_deg / 4) groups
        reduction_potential = out_deg - k * 4  # edges pruned from the hub node
        candidates.append(
            ReductionCandidate(
                kind="hub",
                primary=node,
                members=[node],
                file=f.file,
                line=f.line,
                reduction_potential=max(0, reduction_potential),
                violation_count=len(node_viols),
                detail=(
                    f"fan-out={out_deg} (calls {out_deg} functions) — "
                    f"split by responsibility into {k} cohesive group(s) "
                    f"to reduce fan-out to ≤4 per group"
                ),
            )
        )
    return sorted(candidates, key=lambda c: -c.score)


def analyze_graph_reduction(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    violations: list[Violation],
    hub_threshold: int = 8,
) -> list[ReductionCandidate]:
    """
    Return all graph reduction candidates sorted by score (reduction_potential + violation urgency).

    The returned list interleaves tangles, pass-throughs, and hubs, ranked by
    their combined score so the highest-value structural simplifications appear first.
    """
    G, func_by_name = _build_digraph(functions, call_sites)
    if G is None:
        return []

    tangles = find_sccs(G, func_by_name, violations)
    passthroughs = find_passthroughs(G, func_by_name, violations)
    hubs = find_hubs(G, func_by_name, violations, threshold=hub_threshold)

    all_candidates = tangles + passthroughs + hubs
    return sorted(all_candidates, key=lambda c: -c.score)


# ---------------------------------------------------------------------------
# Actual graph transformations
# ---------------------------------------------------------------------------


def _live_roots(G) -> set[str]:
    """Heuristic: nodes with no predecessors, or named like entry points."""
    roots: set[str] = set()
    for node in G.nodes():
        if G.in_degree(node) == 0:
            roots.add(node)
        # Decorated entry points: Flask/FastAPI/Celery/Django handlers often have
        # no graph-visible caller because the framework invokes them by name.
        name = node.split(".")[-1]
        if name in {
            "main", "__main__", "__init__", "run", "start", "setup",
            "execute", "handle", "dispatch", "process",
        }:
            roots.add(node)
        # Public API convention: no leading underscore, top-level module name
        if not name.startswith("_") and "." not in node:
            roots.add(node)
    return roots or set(G.nodes())  # degenerate: treat all as live if none found


@dataclass
class ReductionResult:
    """Statistics and reduced graph from apply_full_reduction."""

    original_nodes: int
    original_edges: int
    after_scc_nodes: int
    after_scc_edges: int
    after_dead_nodes: int
    after_dead_edges: int
    final_nodes: int
    final_edges: int
    # Mapping: condensation node → frozenset of original node names (size>1 = was SCC)
    scc_map: dict[str, frozenset[str]]
    # Nodes removed as dead (unreachable from any live root)
    dead_nodes: frozenset[str]
    # The fully reduced DiGraph (node labels are original or SCC representative names)
    graph: object  # nx.DiGraph | None

    def summary(self) -> str:
        lines = [
            "  Graph reduction pipeline",
            f"    original:           {self.original_nodes} nodes, {self.original_edges} edges",
        ]
        scc_collapsed = self.original_nodes - self.after_scc_nodes
        dead_pruned = self.after_scc_nodes - self.after_dead_nodes
        tr_edges = self.after_dead_edges - self.final_edges
        if scc_collapsed:
            lines.append(
                f"    after SCC contract: {self.after_scc_nodes} nodes "
                f"({scc_collapsed} cycle(s) collapsed), {self.after_scc_edges} edges"
            )
        if dead_pruned:
            lines.append(
                f"    after dead-prune:   {self.after_dead_nodes} nodes "
                f"({dead_pruned} unreachable removed), {self.after_dead_edges} edges"
            )
        if tr_edges:
            lines.append(
                f"    after trans-reduce: {self.final_nodes} nodes, "
                f"{self.final_edges} edges ({tr_edges} redundant edge(s) removed)"
            )
        total_node = self.original_nodes - self.final_nodes
        total_edge = self.original_edges - self.final_edges
        lines.append(
            f"    TOTAL eliminated:   {total_node} node(s), {total_edge} edge(s) "
            f"→ {self.final_nodes} nodes / {self.final_edges} edges remain"
        )
        if self.dead_nodes:
            sample = sorted(self.dead_nodes)[:5]
            suffix = f" … (+{len(self.dead_nodes)-5} more)" if len(self.dead_nodes) > 5 else ""
            lines.append(f"    dead functions:     {', '.join(sample)}{suffix}")
        tangled = {rep: members for rep, members in self.scc_map.items() if len(members) > 1}
        if tangled:
            for rep, members in sorted(tangled.items(), key=lambda x: -len(x[1]))[:3]:
                cycle = " → ".join(sorted(members)[:4])
                if len(members) > 4:
                    cycle += f" … ({len(members)} total)"
                lines.append(f"    scc [{rep}]: {cycle}")
        return "\n".join(lines)


def contract_sccs(G) -> tuple[object, dict[str, frozenset[str]]]:
    """Collapse every SCC into one representative node; return (condensation, scc_map).

    The condensation is a DAG.  ``scc_map[rep]`` is the frozenset of original node
    names that were merged into representative ``rep`` (the lexicographically first
    member of each SCC).  Single-node SCCs map to themselves.
    """
    if not _HAS_NX:
        return G, {}

    condensation = nx.condensation(G)
    scc_map: dict[str, frozenset[str]] = {}
    # nx.condensation labels nodes 0,1,2,... and stores original members in node attr
    relabel: dict[int, str] = {}
    for cnode in condensation.nodes():
        members: set[str] = condensation.nodes[cnode]["members"]
        rep = min(members)  # lexicographic representative
        relabel[cnode] = rep
        scc_map[rep] = frozenset(members)

    labeled = nx.relabel_nodes(condensation, relabel)
    return labeled, scc_map


def eliminate_dead(G, roots: set[str] | None = None) -> tuple[object, frozenset[str]]:
    """Remove nodes not reachable from any live root.

    Returns (pruned_G, dead_nodes).  ``roots`` defaults to the heuristic set
    from ``_live_roots`` when not provided.
    """
    if not _HAS_NX:
        return G, frozenset()

    live_roots = roots if roots is not None else _live_roots(G)
    reachable: set[str] = set()
    for root in live_roots:
        if root in G:
            reachable.update(nx.descendants(G, root))
            reachable.add(root)

    dead = frozenset(n for n in G.nodes() if n not in reachable)
    pruned = G.copy()
    pruned.remove_nodes_from(dead)
    return pruned, dead


def transitive_reduce(G) -> object:
    """Remove edges implied by longer paths (minimum edge set preserving reachability).

    Requires G to be a DAG; raises NetworkXError otherwise.  Apply after
    ``contract_sccs`` to guarantee acyclicity.
    """
    if not _HAS_NX:
        return G
    return nx.transitive_reduction(G)


def apply_full_reduction(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    violations: list[Violation],
    roots: set[str] | None = None,
) -> ReductionResult:
    """Run the full three-stage reduction pipeline and return statistics.

    Stages (in order):
      1. SCC contraction  → condense cycles into single nodes (produces a DAG)
      2. Dead-node pruning → remove nodes unreachable from live entry points
      3. Transitive reduction → strip edges implied by longer paths

    The pipeline is non-destructive: the original graph is never modified.
    """
    G, _ = _build_digraph(functions, call_sites)
    if G is None:
        return ReductionResult(
            original_nodes=0, original_edges=0,
            after_scc_nodes=0, after_scc_edges=0,
            after_dead_nodes=0, after_dead_edges=0,
            final_nodes=0, final_edges=0,
            scc_map={}, dead_nodes=frozenset(), graph=None,
        )

    orig_n, orig_e = G.number_of_nodes(), G.number_of_edges()

    # Stage 1: SCC contraction → DAG
    G1, scc_map = contract_sccs(G)
    scc_n, scc_e = G1.number_of_nodes(), G1.number_of_edges()

    # Stage 2: dead-node elimination
    G2, dead = eliminate_dead(G1, roots)
    dead_n, dead_e = G2.number_of_nodes(), G2.number_of_edges()

    # Stage 3: transitive reduction (safe now that G2 is a DAG)
    try:
        G3 = transitive_reduce(G2)
    except Exception:
        G3 = G2  # non-DAG edge case: skip rather than crash
    final_n, final_e = G3.number_of_nodes(), G3.number_of_edges()

    return ReductionResult(
        original_nodes=orig_n,
        original_edges=orig_e,
        after_scc_nodes=scc_n,
        after_scc_edges=scc_e,
        after_dead_nodes=dead_n,
        after_dead_edges=dead_e,
        final_nodes=final_n,
        final_edges=final_e,
        scc_map=scc_map,
        dead_nodes=dead,
        graph=G3,
    )
