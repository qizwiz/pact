"""
Tests for tda.py — persistent homology for call graphs.

Coverage:
- PersistencePair.persistence computation (finite and infinite)
- PersistenceResult.render() produces expected keys
- PersistenceResult.to_dict() contains required keys
- compute_persistence on a triangle (3-node cycle)
- compute_persistence on a tree (no cycles) — beta1=0, total=0
- Graceful fallback when gudhi is not available
- total_persistence_h1 > 0 for a graph with a long-lived cycle
"""

from __future__ import annotations

import math

import networkx as nx

from . import tda
from .tda import PersistencePair, PersistenceResult, compute_persistence

# ---------------------------------------------------------------------------
# Unit: PersistencePair
# ---------------------------------------------------------------------------


def test_pair_finite_persistence():
    p = PersistencePair(dimension=1, birth=0.2, death=0.8)
    assert math.isclose(p.persistence, 0.6)


def test_pair_infinite_persistence():
    p = PersistencePair(dimension=1, birth=0.0, death=math.inf)
    assert math.isinf(p.persistence)


def test_pair_zero_persistence():
    p = PersistencePair(dimension=0, birth=0.5, death=0.5)
    assert p.persistence == 0.0


# ---------------------------------------------------------------------------
# Unit: PersistenceResult
# ---------------------------------------------------------------------------


def _make_result() -> PersistenceResult:
    pairs = [
        PersistencePair(dimension=0, birth=0.0, death=math.inf),
        PersistencePair(dimension=1, birth=0.3, death=0.9),
        PersistencePair(dimension=1, birth=0.1, death=math.inf),
    ]
    return PersistenceResult(
        pairs=pairs,
        total_persistence_h1=0.6,
        beta1=1,
        beta0=1,
        backend="gudhi",
    )


def test_result_render_contains_expected_sections():
    r = _make_result()
    rendered = r.render()
    assert "β₀" in rendered
    assert "β₁" in rendered
    assert "total_persistence_H1" in rendered
    assert "H1 bars" in rendered


def test_result_to_dict_required_keys():
    d = _make_result().to_dict()
    assert "beta0" in d
    assert "beta1" in d
    assert "total_persistence_h1" in d
    assert "backend" in d
    assert "h1_bars" in d


def test_result_to_dict_h1_bars():
    d = _make_result().to_dict()
    # Two H1 pairs: one finite (persistence=0.6), one infinite
    assert len(d["h1_bars"]) == 2
    finite = [b for b in d["h1_bars"] if b["persistence"] is not None]
    assert len(finite) == 1
    assert math.isclose(finite[0]["persistence"], 0.6)


def test_result_h1_pairs_filter():
    r = _make_result()
    h1 = r.h1_pairs()
    assert all(p.dimension == 1 for p in h1)
    assert len(h1) == 2


# ---------------------------------------------------------------------------
# Integration: triangle graph (3-node cycle)
# ---------------------------------------------------------------------------


def test_triangle_flag_complex_contractible():
    """
    Triangle (3-clique): in the flag/clique complex, the 2-simplex fills the
    cycle at the same filtration as the last edge — so the H1 bar has zero
    persistence and is not reported.  A 3-clique is contractible; beta1=0.

    The nx_fallback path (Euler formula, no 2-simplices) reports beta1=1.
    """
    G = nx.DiGraph()
    G.add_edges_from([(0, 1), (1, 2), (2, 0)])
    result = compute_persistence(G)
    # Flag complex: triangle is filled immediately — no persistent H1
    assert result.beta1 == 0
    assert result.total_persistence_h1 == 0.0


def test_triangle_fallback_has_beta1(monkeypatch):
    """nx_fallback uses Euler formula: triangle has β₁=1 without 2-simplices."""
    monkeypatch.setattr(tda, "_HAS_GUDHI", False)
    G = nx.DiGraph()
    G.add_edges_from([(0, 1), (1, 2), (2, 0)])
    result = compute_persistence(G)
    assert result.backend == "nx_fallback"
    assert result.beta1 >= 1


def test_four_cycle_shortcut_has_h1_persistence():
    """
    4-node cycle 0→1→2→3→0 with shortcut 1→3 at higher filtration.
    The shortcut triangle 1-2-3 is filled at 0.8, giving H1 bar (0.2, 0.8).
    total_persistence_h1 should be positive.
    """
    G = nx.DiGraph()
    G.add_edge(0, 1, weight=0.2)
    G.add_edge(1, 2, weight=0.2)
    G.add_edge(2, 3, weight=0.2)
    G.add_edge(3, 0, weight=0.2)
    G.add_edge(1, 3, weight=0.8)
    result = compute_persistence(G, weight_attr="weight")

    h1_finite = [p for p in result.h1_pairs() if not math.isinf(p.persistence)]
    assert (
        len(h1_finite) >= 1
    ), f"Expected at least one finite H1 bar; got pairs={result.h1_pairs()}"
    assert result.total_persistence_h1 > 0


# ---------------------------------------------------------------------------
# Integration: tree (no cycles)
# ---------------------------------------------------------------------------


def test_tree_has_zero_beta1_and_zero_persistence():
    """Tree has no cycles — beta1 and total_persistence_h1 must both be 0."""
    G = nx.DiGraph()
    G.add_edges_from([(0, 1), (0, 2), (1, 3), (1, 4)])
    result = compute_persistence(G)
    assert result.beta1 == 0
    assert result.total_persistence_h1 == 0.0


def test_single_node_graph():
    G = nx.DiGraph()
    G.add_node(0)
    result = compute_persistence(G)
    assert result.beta1 == 0
    assert result.total_persistence_h1 == 0.0
    assert result.beta0 >= 1


def test_empty_graph():
    G = nx.DiGraph()
    result = compute_persistence(G)
    assert result.beta1 == 0
    assert result.total_persistence_h1 == 0.0


# ---------------------------------------------------------------------------
# Integration: long-lived cycle
# ---------------------------------------------------------------------------


def test_long_lived_cycle_total_persistence():
    """
    4-node square 0-1-2-3-0 with a shortcut 1-3 at higher filtration.
    The cycle 0-1-2-3-0 forms at weight 0.2, shortcut 1-3 appears at 0.8.
    The triangle 1-2-3 is filled at 0.8, giving H1 bar (0.2, 0.8), pers=0.6.
    total_persistence_h1 should be > 0.
    """
    G = nx.DiGraph()
    G.add_edge(0, 1, weight=0.2)
    G.add_edge(1, 2, weight=0.2)
    G.add_edge(2, 3, weight=0.2)
    G.add_edge(3, 0, weight=0.2)
    G.add_edge(1, 3, weight=0.8)  # shortcut — fills part of the cycle
    result = compute_persistence(G, weight_attr="weight")
    assert (
        result.total_persistence_h1 > 0
    ), f"Expected total_persistence_h1 > 0; got {result.total_persistence_h1}"


# ---------------------------------------------------------------------------
# Fallback: graceful degradation when gudhi not available
# ---------------------------------------------------------------------------


def test_fallback_when_gudhi_unavailable(monkeypatch):
    """When gudhi is not importable, compute_persistence uses nx_fallback."""
    # Temporarily hide gudhi from the module
    monkeypatch.setattr(tda, "_HAS_GUDHI", False)

    G = nx.DiGraph()
    G.add_edges_from([(0, 1), (1, 2), (2, 0)])
    result = compute_persistence(G)

    assert result.backend == "nx_fallback"
    # Fallback should still detect the cycle (beta1 >= 1)
    assert result.beta1 >= 1


def test_fallback_tree_no_cycles(monkeypatch):
    """Fallback path: tree still reports beta1=0."""
    monkeypatch.setattr(tda, "_HAS_GUDHI", False)

    G = nx.DiGraph()
    G.add_edges_from([(0, 1), (0, 2), (1, 3)])
    result = compute_persistence(G)

    assert result.backend == "nx_fallback"
    assert result.beta1 == 0


# ---------------------------------------------------------------------------
# sheaf_summary integration: new keys present
# ---------------------------------------------------------------------------


def test_sheaf_summary_has_total_persistence_key():
    """sheaf_summary output dict must contain 'total_persistence' key."""
    import tempfile
    import os
    import textwrap
    from .pact_sheaf import sheaf_summary

    src = textwrap.dedent("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            return r.choices[0].message.content
    """)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(src)
        name = f.name
    try:
        summary = sheaf_summary(name)
        assert (
            "total_persistence" in summary
        ), f"'total_persistence' key missing from sheaf_summary output: {list(summary)}"
        assert (
            "persistence" in summary
        ), f"'persistence' key missing from sheaf_summary output: {list(summary)}"
    finally:
        os.unlink(name)
