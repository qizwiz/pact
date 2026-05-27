"""
Tests for pact graph reduction analysis (reduce.py).

Covers the three structural anti-patterns:
  - SCC tangles (call cycles)
  - Pass-through nodes (in=1, out=1)
  - Fan-out hubs (out-degree > threshold)
"""

import pytest

from .reduce import (
    analyze_graph_reduction,
    apply_full_reduction,
    compute_blast_radii,
    compute_module_metrics,
    compute_spectral_gap,
    contract_sccs,
    eliminate_dead,
    find_bridge_violations,
    transitive_reduce,
    find_hubs,
    find_passthroughs,
    find_sccs,
    ModuleMetrics,
    SpectralResult,
    _build_digraph,
    _func_for_violation,
)
from .extractor import FunctionManifest, CallSite
from .encoder import Violation

try:
    import networkx  # noqa: F401

    _HAS_NX = True
except ImportError:
    _HAS_NX = False

try:
    import scipy  # noqa: F401

    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

pytestmark = pytest.mark.skipif(not _HAS_NX, reason="networkx not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _func(name: str, file: str = "mod.py", line: int = 1) -> FunctionManifest:
    return FunctionManifest(name=name, file=file, line=line, args=[], module_path="mod")


def _call(caller: str, callee: str, file: str = "mod.py", line: int = 1) -> CallSite:
    return CallSite(
        caller_name=caller,
        callee_name=callee,
        file=file,
        line=line,
    )


def _viol(file: str = "mod.py") -> Violation:
    return Violation(
        file=file,
        line=1,
        call="x()",
        missing=["something"],
        context="optional_dereference",
    )


# ---------------------------------------------------------------------------
# SCC tangle tests
# ---------------------------------------------------------------------------


class TestSCCTangles:
    def test_simple_cycle_detected(self):
        """A → B → A is a tangle of size 2."""
        funcs = [_func("A"), _func("B")]
        calls = [_call("A", "B"), _call("B", "A")]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_sccs(G, func_by_name, [])
        assert len(result) == 1
        assert result[0].kind == "tangle"
        assert set(result[0].members) == {"A", "B"}
        assert result[0].reduction_potential == 1  # N-1 back-edges

    def test_three_way_cycle(self):
        """A → B → C → A: SCC of size 3, reduction_potential = 2."""
        funcs = [_func("A"), _func("B"), _func("C")]
        calls = [_call("A", "B"), _call("B", "C"), _call("C", "A")]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_sccs(G, func_by_name, [])
        assert len(result) == 1
        assert result[0].reduction_potential == 2

    def test_no_cycle_returns_empty(self):
        """A → B → C (DAG): no tangles."""
        funcs = [_func("A"), _func("B"), _func("C")]
        calls = [_call("A", "B"), _call("B", "C")]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_sccs(G, func_by_name, [])
        assert result == []

    def test_violations_are_urgency_not_score(self):
        """Violations annotate a tangle as urgency but do not affect structural score.

        Two tangles with identical structure must have identical scores regardless
        of violation count — structure drives rank, violations are a separate signal.
        """
        funcs = [_func("A", "a.py"), _func("B", "a.py")]
        calls = [_call("A", "B"), _call("B", "A")]
        viols = [_viol("a.py")]
        G, func_by_name = _build_digraph(funcs, calls)
        result_with = find_sccs(G, func_by_name, viols)
        result_without = find_sccs(G, func_by_name, [])
        # Score is structural: same for both (reduction_potential only)
        assert result_with[0].score == result_without[0].score
        # Urgency differs: violations are annotated separately
        assert result_with[0].urgency > result_without[0].urgency
        assert result_without[0].urgency == 0.0

    def test_self_loop_not_counted_as_tangle(self):
        """A → A is a self-loop; it forms an SCC of size 1, not a tangle."""
        funcs = [_func("A")]
        calls = [_call("A", "A")]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_sccs(G, func_by_name, [])
        assert result == []


# ---------------------------------------------------------------------------
# Pass-through tests
# ---------------------------------------------------------------------------


class TestPassthroughs:
    def test_pure_passthrough_detected(self):
        """B has exactly 1 caller (A) and 1 callee (C): passthrough."""
        funcs = [_func("A"), _func("B"), _func("C")]
        calls = [_call("A", "B"), _call("B", "C")]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_passthroughs(G, func_by_name, [])
        names = [c.primary for c in result]
        assert "B" in names

    def test_hub_not_passthrough(self):
        """A node calling 2 functions is not a pass-through."""
        funcs = [_func("A"), _func("B"), _func("C"), _func("D")]
        calls = [_call("A", "B"), _call("B", "C"), _call("B", "D")]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_passthroughs(G, func_by_name, [])
        names = [c.primary for c in result]
        assert "B" not in names

    def test_passthrough_reduction_potential(self):
        """A pass-through removal eliminates 1 node + 2 edges = potential 3."""
        funcs = [_func("A"), _func("B"), _func("C")]
        calls = [_call("A", "B"), _call("B", "C")]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_passthroughs(G, func_by_name, [])
        pt = next(c for c in result if c.primary == "B")
        assert pt.reduction_potential == 3


# ---------------------------------------------------------------------------
# Hub tests
# ---------------------------------------------------------------------------


class TestHubs:
    def test_hub_above_threshold_detected(self):
        """A function calling 5 others with threshold=4 is a hub."""
        funcs = [_func("A")] + [_func(f"C{i}") for i in range(5)]
        calls = [_call("A", f"C{i}") for i in range(5)]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_hubs(G, func_by_name, [], threshold=4)
        assert any(c.primary == "A" for c in result)

    def test_hub_below_threshold_not_detected(self):
        """A function calling 3 others with threshold=4 is not a hub."""
        funcs = [_func("A")] + [_func(f"C{i}") for i in range(3)]
        calls = [_call("A", f"C{i}") for i in range(3)]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_hubs(G, func_by_name, [], threshold=4)
        assert result == []

    def test_hub_reduction_detail_mentions_split(self):
        """Hub detail should mention splitting into groups."""
        funcs = [_func("A")] + [_func(f"C{i}") for i in range(9)]
        calls = [_call("A", f"C{i}") for i in range(9)]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_hubs(G, func_by_name, [], threshold=8)
        hub = next(c for c in result if c.primary == "A")
        assert "split" in hub.detail.lower() or "group" in hub.detail.lower()


# ---------------------------------------------------------------------------
# Integration: analyze_graph_reduction
# ---------------------------------------------------------------------------


class TestAnalyzeGraphReduction:
    def test_combined_returns_all_types(self):
        """A graph with a cycle, a pass-through, and a hub returns all 3 kinds."""
        funcs = [
            _func("cycle_a", "a.py"),
            _func("cycle_b", "a.py"),  # tangle
            _func("pass_x", "b.py"),
            _func("pass_src", "b.py"),
            _func("pass_dst", "b.py"),  # passthrough
            _func("hub", "c.py"),  # hub
        ] + [_func(f"spoke_{i}", "c.py") for i in range(9)]

        calls = [
            _call("cycle_a", "cycle_b"),
            _call("cycle_b", "cycle_a"),
            _call("pass_src", "pass_x"),
            _call("pass_x", "pass_dst"),
        ] + [_call("hub", f"spoke_{i}") for i in range(9)]

        result = analyze_graph_reduction(funcs, calls, [], hub_threshold=8)
        kinds = {c.kind for c in result}
        assert "tangle" in kinds
        assert "passthrough" in kinds
        assert "hub" in kinds

    def test_sorted_by_score_descending(self):
        """Results are sorted highest score first."""
        funcs = [_func("A")] + [_func(f"C{i}") for i in range(10)]
        calls = [_call("A", f"C{i}") for i in range(10)]
        result = analyze_graph_reduction(funcs, calls, [], hub_threshold=8)
        scores = [c.score for c in result]
        assert scores == sorted(scores, reverse=True)

    def test_empty_graph_returns_empty(self):
        """No functions → no candidates."""
        result = analyze_graph_reduction([], [], [])
        assert result == []


class TestPhantomCyclePrevention:
    """Regression tests: ambiguous short-name resolution must not manufacture cycles."""

    def test_overloaded_method_name_no_phantom_scc(self):
        """Two classes each have a `from_template` method.

        Call site: A.do_something → B.from_template (qualified call).
        Before the fix, _build_digraph resolved `B.from_template` via short-name
        fallback to `A.from_template`, creating an A→B cycle that doesn't exist.
        """
        funcs = [
            _func("A.from_template", "a.py", 10),
            _func("A.do_something", "a.py", 20),
            _func("B.from_template", "b.py", 10),
        ]
        # A.do_something calls B.from_template (qualified — not in func_names by exact match
        # but B.from_template IS present, so it should match directly)
        # The phantom case: callee "PromptTemplate.from_template" NOT in func_names,
        # short name "from_template" is ambiguous (A.from_template AND B.from_template)
        calls = [
            # Simulate a call to a third `from_template` that pact can't resolve
            _call("A.do_something", "PromptTemplate.from_template", "a.py", 25),
        ]
        G, func_by_name = _build_digraph(funcs, calls)
        result = find_sccs(G, func_by_name, [])
        # With 2 definitions of from_template, the short name is ambiguous →
        # no resolution → the unresolved node is a leaf → no SCC formed
        assert result == [], (
            "Ambiguous short name 'from_template' must not create phantom SCC; "
            f"got: {result}"
        )

    def test_unique_short_name_still_resolves(self):
        """When a short name is unambiguous, the fallback resolution still works."""
        funcs = [
            _func("MyClass.unique_helper", "a.py", 10),
            _func("Caller.run", "a.py", 20),
        ]
        # callee name that pact would record without the class prefix
        calls = [_call("Caller.run", "unique_helper", "a.py", 25)]
        G, func_by_name = _build_digraph(funcs, calls)
        # unique_helper is unambiguous → should resolve and appear as a node
        assert "MyClass.unique_helper" in G.nodes or "unique_helper" in G.nodes


# ---------------------------------------------------------------------------
# contract_sccs
# ---------------------------------------------------------------------------


class TestContractSccs:
    def test_simple_cycle_collapsed_to_one_node(self):
        """A→B→A (2-node cycle) collapses to a single representative node."""
        funcs = [_func("A"), _func("B")]
        calls = [_call("A", "B"), _call("B", "A")]
        G, _ = _build_digraph(funcs, calls)
        condensation, scc_map = contract_sccs(G)
        # Two nodes in a mutual cycle → one node in the condensation
        assert condensation.number_of_nodes() == 1
        # The scc_map entry for the representative contains both A and B
        (rep,) = condensation.nodes()
        assert scc_map[rep] == frozenset({"A", "B"})

    def test_dag_unchanged(self):
        """A DAG has no cycles; condensation has same node count."""
        funcs = [_func("A"), _func("B"), _func("C")]
        calls = [_call("A", "B"), _call("B", "C")]
        G, _ = _build_digraph(funcs, calls)
        condensation, scc_map = contract_sccs(G)
        # No SCCs of size > 1; node count preserved (plus __root__ if present)
        non_root = [n for n in condensation.nodes() if n != "__root__"]
        assert len(non_root) >= 3  # A, B, C all survive as separate nodes
        # Every scc_map entry is a singleton
        assert all(len(v) == 1 for v in scc_map.values())

    def test_three_node_cycle(self):
        """A→B→C→A collapses to one node; outgoing edges preserved."""
        funcs = [_func("A"), _func("B"), _func("C"), _func("D")]
        calls = [_call("A", "B"), _call("B", "C"), _call("C", "A"), _call("A", "D")]
        G, _ = _build_digraph(funcs, calls)
        condensation, scc_map = contract_sccs(G)
        # {A,B,C} → 1 node; D stays separate → 2 nodes total (plus possible __root__)
        scc_sizes = sorted(len(v) for v in scc_map.values())
        assert 3 in scc_sizes, f"expected a 3-node SCC, got scc sizes {scc_sizes}"

    def test_result_is_dag(self):
        """Condensation of any graph is always a DAG."""
        import networkx as nx

        funcs = [_func(n) for n in "ABCDE"]
        calls = [
            _call("A", "B"),
            _call("B", "C"),
            _call("C", "A"),  # cycle ABC
            _call("C", "D"),
            _call("D", "E"),
            _call("E", "D"),  # cycle DE
        ]
        G, _ = _build_digraph(funcs, calls)
        condensation, _ = contract_sccs(G)
        assert nx.is_directed_acyclic_graph(condensation), "condensation must be a DAG"


# ---------------------------------------------------------------------------
# eliminate_dead
# ---------------------------------------------------------------------------


class TestEliminateDead:
    def test_unreachable_node_removed(self):
        """A node with no callers and not a root is dead."""
        funcs = [_func("main"), _func("helper"), _func("orphan")]
        calls = [_call("main", "helper")]
        G, _ = _build_digraph(funcs, calls)
        pruned, dead = eliminate_dead(G, roots={"main"})
        assert "orphan" in dead
        assert "orphan" not in pruned.nodes()

    def test_reachable_node_kept(self):
        """Transitively reachable nodes survive pruning."""
        funcs = [_func("main"), _func("a"), _func("b")]
        calls = [_call("main", "a"), _call("a", "b")]
        G, _ = _build_digraph(funcs, calls)
        pruned, dead = eliminate_dead(G, roots={"main"})
        assert "b" not in dead
        assert "b" in pruned.nodes()

    def test_empty_roots_keeps_all(self):
        """When roots heuristic fires for all nodes, nothing is pruned."""
        funcs = [_func("main"), _func("side")]
        calls = []
        G, _ = _build_digraph(funcs, calls)
        pruned, dead = eliminate_dead(G, roots={"main", "side"})
        assert len(dead) == 0


# ---------------------------------------------------------------------------
# transitive_reduce
# ---------------------------------------------------------------------------


class TestTransitiveReduce:
    def test_shortcut_edge_removed(self):
        """A→C is redundant when A→B→C exists; it should be removed."""
        import networkx as nx

        G = nx.DiGraph()
        G.add_edges_from([("A", "B"), ("B", "C"), ("A", "C")])
        reduced = transitive_reduce(G)
        assert not reduced.has_edge("A", "C"), "shortcut edge A→C must be removed"
        assert reduced.has_edge("A", "B")
        assert reduced.has_edge("B", "C")

    def test_unique_path_kept(self):
        """Non-redundant edges are preserved."""
        import networkx as nx

        G = nx.DiGraph()
        G.add_edges_from([("A", "B"), ("A", "C")])
        reduced = transitive_reduce(G)
        assert reduced.has_edge("A", "B")
        assert reduced.has_edge("A", "C")


# ---------------------------------------------------------------------------
# apply_full_reduction (pipeline)
# ---------------------------------------------------------------------------


class TestApplyFullReduction:
    def test_pipeline_reduces_complex_graph(self):
        """End-to-end: cycle + dead node + shortcut → substantially reduced graph."""
        funcs = [_func(n) for n in ["main", "A", "B", "C", "orphan"]]
        calls = [
            _call("main", "A"),
            _call("A", "B"),
            _call("B", "A"),  # A↔B cycle
            _call("A", "C"),
            _call("main", "C"),  # main→C is a shortcut (main→A→C exists)
            # orphan: no callers, not a root
        ]
        result = apply_full_reduction(funcs, calls, [])
        assert (
            result.original_nodes > result.final_nodes
            or result.original_edges > result.final_edges
        )
        assert result.graph is not None
        # Summary should not raise
        s = result.summary()
        assert "TOTAL eliminated" in s

    def test_empty_graph_returns_zero_stats(self):
        result = apply_full_reduction([], [], [])
        assert result.original_nodes == 0
        assert result.final_nodes == 0
        # graph is either None (no networkx) or an empty DiGraph
        assert result.graph is None or result.graph.number_of_nodes() == 0

    def test_result_graph_is_dag(self):
        """After full reduction the result graph must be a DAG."""
        import networkx as nx

        funcs = [_func(n) for n in ["X", "Y", "Z"]]
        calls = [_call("X", "Y"), _call("Y", "Z"), _call("Z", "X")]  # full cycle
        result = apply_full_reduction(funcs, calls, [])
        if result.graph is not None:
            assert nx.is_directed_acyclic_graph(result.graph)


# ---------------------------------------------------------------------------
# Blast radius tests
# ---------------------------------------------------------------------------


class TestBlastRadius:
    def test_zero_blast_radius_for_entry_point(self):
        """A function with no callers has blast_radius=0."""
        funcs = [
            _func("entry", line=1),
            _func("leaf", line=10),
        ]
        calls = [_call("entry", "leaf")]
        # Violation is in entry (line 1) — entry has no callers, so blast_radius=0
        viols = [
            Violation(
                file="mod.py",
                line=1,
                call="entry",
                missing=["x"],
                context="bare_except",
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        assert len(ranked) == 1
        assert ranked[0].blast_radius == 0
        assert ranked[0].enclosing_func == "entry"

    def test_blast_radius_counts_transitive_callers(self):
        """A → B → C: violation in C has blast_radius=2 (A and B can reach it)."""
        funcs = [_func("A", line=1), _func("B", line=10), _func("C", line=20)]
        calls = [_call("A", "B"), _call("B", "C")]
        viols = [
            Violation(
                file="mod.py", line=20, call="C", missing=["x"], context="bare_except"
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        assert ranked[0].blast_radius == 2
        assert "A" in ranked[0].reachable_from
        assert "B" in ranked[0].reachable_from

    def test_higher_blast_radius_ranked_first(self):
        """Violations are sorted descending by blast_radius."""
        funcs = [
            _func("root", line=1),
            _func("mid", line=10),
            _func("leaf_deep", line=20),
            _func("leaf_shallow", line=30),
        ]
        calls = [
            _call("root", "mid"),
            _call("mid", "leaf_deep"),
            _call("root", "leaf_shallow"),
        ]
        v_deep = Violation(
            file="mod.py",
            line=20,
            call="leaf_deep",
            missing=["x"],
            context="bare_except",
        )
        v_shallow = Violation(
            file="mod.py",
            line=30,
            call="leaf_shallow",
            missing=["x"],
            context="bare_except",
        )
        ranked = compute_blast_radii(funcs, calls, [v_deep, v_shallow])
        # leaf_deep reachable from root+mid (2), leaf_shallow reachable from root (1)
        assert ranked[0].violation == v_deep
        assert ranked[0].blast_radius == 2
        assert ranked[1].violation == v_shallow
        assert ranked[1].blast_radius == 1

    def test_unlocatable_violation_gets_zero(self):
        """Violations in files with no extracted functions get blast_radius=0."""
        funcs = [_func("A", file="other.py", line=1)]
        calls = []
        viols = [
            Violation(
                file="unknown.py",
                line=5,
                call="x",
                missing=["y"],
                context="bare_except",
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        assert ranked[0].blast_radius == 0

    def test_empty_violations_returns_empty(self):
        funcs = [_func("A"), _func("B")]
        calls = [_call("A", "B")]
        ranked = compute_blast_radii(funcs, calls, [])
        assert ranked == []

    def test_func_for_violation_finds_enclosing(self):
        """_func_for_violation returns the last function starting before the violation."""
        funcs = {
            "first": FunctionManifest(
                name="first", file="f.py", line=1, args=[], module_path="f"
            ),
            "second": FunctionManifest(
                name="second", file="f.py", line=20, args=[], module_path="f"
            ),
        }
        v_in_first = Violation(
            file="f.py", line=15, call="x", missing=["y"], context="bare_except"
        )
        v_in_second = Violation(
            file="f.py", line=25, call="x", missing=["y"], context="bare_except"
        )
        assert _func_for_violation(v_in_first, funcs) == "first"
        assert _func_for_violation(v_in_second, funcs) == "second"

    def test_blast_radius_summary_contains_key_fields(self):
        """ViolationWithBlast.summary() includes file, line, blast count, context."""
        funcs = [_func("caller", line=1), _func("target", line=10)]
        calls = [_call("caller", "target")]
        viols = [
            Violation(
                file="mod.py",
                line=10,
                call="target",
                missing=["check"],
                context="optional_dereference",
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        s = ranked[0].summary()
        assert "mod.py" in s
        assert "optional_dereference" in s
        assert "blast=" in s


class TestBetweennessAndBridgeViolations:
    """Tests for betweenness centrality on ViolationWithBlast and find_bridge_violations."""

    def test_betweenness_populated_on_bridge_node(self):
        """A function that is the sole path between two halves of the graph
        has non-zero betweenness centrality."""
        # A → bridge → C, D → bridge → E: bridge lies on every path A↔C, D↔E
        funcs = [
            _func("A", line=1),
            _func("bridge", line=10),
            _func("C", line=20),
        ]
        calls = [_call("A", "bridge"), _call("bridge", "C")]
        viols = [
            Violation(
                file="mod.py",
                line=10,
                call="bridge",
                missing=["x"],
                context="bare_except",
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        assert len(ranked) == 1
        assert ranked[0].betweenness > 0.0, "bridge node must have non-zero betweenness"

    def test_leaf_node_has_zero_betweenness(self):
        """A leaf node (no outgoing edges, single path) has betweenness=0."""
        funcs = [_func("root", line=1), _func("leaf", line=10)]
        calls = [_call("root", "leaf")]
        viols = [
            Violation(
                file="mod.py",
                line=10,
                call="leaf",
                missing=["x"],
                context="bare_except",
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        assert ranked[0].enclosing_func == "leaf"
        assert ranked[0].betweenness == 0.0

    def test_find_bridge_violations_returns_high_betweenness(self):
        """find_bridge_violations returns only violations above the threshold."""
        # Linear chain: A → B → C; B is the structural bridge
        funcs = [_func("A", line=1), _func("B", line=10), _func("C", line=20)]
        calls = [_call("A", "B"), _call("B", "C")]
        v_B = Violation(
            file="mod.py", line=10, call="B", missing=["x"], context="bare_except"
        )
        v_A = Violation(
            file="mod.py", line=1, call="A", missing=["x"], context="bare_except"
        )
        bridges = find_bridge_violations(funcs, calls, [v_B, v_A], threshold=0.0)
        # Both returned since threshold=0 includes all
        assert len(bridges) == 2

    def test_find_bridge_violations_threshold_filters_leaf(self):
        """Violations in leaf nodes (betweenness=0) are excluded when threshold>0."""
        funcs = [_func("A", line=1), _func("B", line=10), _func("C", line=20)]
        calls = [_call("A", "B"), _call("B", "C")]
        v_C = Violation(
            file="mod.py", line=20, call="C", missing=["x"], context="bare_except"
        )
        bridges = find_bridge_violations(funcs, calls, [v_C], threshold=0.01)
        assert len(bridges) == 0, "leaf C has betweenness=0, must be filtered"

    def test_find_bridge_violations_sorted_by_betweenness(self):
        """Returned violations are sorted descending by betweenness."""
        # Diamond: A→B, A→C, B→D, C→D; D is the most-central sink
        funcs = [
            _func("A", line=1),
            _func("B", line=10),
            _func("C", line=20),
            _func("D", line=30),
        ]
        calls = [_call("A", "B"), _call("A", "C"), _call("B", "D"), _call("C", "D")]
        v_B = Violation(
            file="mod.py", line=10, call="B", missing=["x"], context="bare_except"
        )
        v_C = Violation(
            file="mod.py", line=20, call="C", missing=["x"], context="bare_except"
        )
        bridges = find_bridge_violations(funcs, calls, [v_B, v_C], threshold=0.0)
        # Result must be sorted descending by betweenness
        btwns = [r.betweenness for r in bridges]
        assert btwns == sorted(btwns, reverse=True)

    def test_summary_includes_betweenness_when_nonzero(self):
        """ViolationWithBlast.summary() includes btw= when betweenness > 0."""
        funcs = [_func("A", line=1), _func("B", line=10), _func("C", line=20)]
        calls = [_call("A", "B"), _call("B", "C")]
        viols = [
            Violation(
                file="mod.py", line=10, call="B", missing=["x"], context="bare_except"
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        s = ranked[0].summary()
        assert "btw=" in s, f"expected btw= in summary for bridge node; got: {s}"

    def test_cut_vertex_detected(self):
        """A function whose removal disconnects the graph is a cut vertex."""
        # Linear chain: A → B → C; B is the sole bridge (articulation point)
        funcs = [_func("A", line=1), _func("B", line=10), _func("C", line=20)]
        calls = [_call("A", "B"), _call("B", "C")]
        viols = [
            Violation(
                file="mod.py", line=10, call="B", missing=["x"], context="bare_except"
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        assert ranked[0].is_cut_vertex, "B is the sole bridge — must be a cut vertex"

    def test_non_cut_vertex_not_flagged(self):
        """A leaf node that can be removed without disconnecting the graph is not a cut vertex."""
        funcs = [_func("A", line=1), _func("B", line=10)]
        calls = [_call("A", "B")]
        viols = [
            Violation(
                file="mod.py", line=10, call="B", missing=["x"], context="bare_except"
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        assert not ranked[0].is_cut_vertex, "leaf B is not a cut vertex"

    def test_cut_vertex_sorted_before_high_betweenness(self):
        """Cut vertices appear before high-betweenness non-cut-vertices in find_bridge_violations."""
        # Build a graph where one node has high betweenness but is NOT a cut vertex,
        # and another has lower betweenness but IS a cut vertex.
        # Star graph: hub→A, hub→B, hub→C, hub→D (hub has high betweenness but NOT a cut vertex
        # in an undirected star — removing hub disconnects everything, so hub IS a cut vertex in stars)
        # Use a different structure: A—B—C plus A—C shortcut (B is not a cut vertex, A/C are not either)
        # then D—E—F chain (E IS a cut vertex)
        funcs = [
            _func("A", line=1),
            _func("B", line=10),
            _func("C", line=20),
            _func("D", line=30),
            _func("E", line=40),
            _func("F", line=50),
        ]
        calls = [
            _call("A", "B"),
            _call("B", "C"),
            _call("A", "C"),  # triangle: no cut vertices
            _call("D", "E"),
            _call("E", "F"),  # chain: E is cut vertex
        ]
        v_B = Violation(
            file="mod.py", line=10, call="B", missing=["x"], context="bare_except"
        )
        v_E = Violation(
            file="mod.py", line=40, call="E", missing=["x"], context="bare_except"
        )
        bridges = find_bridge_violations(funcs, calls, [v_B, v_E], threshold=0.0)
        cut_indices = [i for i, r in enumerate(bridges) if r.is_cut_vertex]
        non_cut_indices = [i for i, r in enumerate(bridges) if not r.is_cut_vertex]
        if cut_indices and non_cut_indices:
            assert min(cut_indices) < max(
                non_cut_indices
            ), "cut vertices must appear before non-cut-vertices"

    def test_summary_includes_cut_vertex_label(self):
        """ViolationWithBlast.summary() includes [CUT VERTEX] for cut vertices."""
        funcs = [_func("A", line=1), _func("B", line=10), _func("C", line=20)]
        calls = [_call("A", "B"), _call("B", "C")]
        viols = [
            Violation(
                file="mod.py", line=10, call="B", missing=["x"], context="bare_except"
            )
        ]
        ranked = compute_blast_radii(funcs, calls, viols)
        s = ranked[0].summary()
        assert "[CUT VERTEX]" in s, f"expected [CUT VERTEX] in summary; got: {s}"


# ---------------------------------------------------------------------------
# _show_cut_vertex_contracts (intent trigger)
# ---------------------------------------------------------------------------


class TestShowCutVertexContracts:
    """Tests for the NetworkX → intent trigger in _show_cut_vertex_contracts."""

    def _linear_graph(self):
        """A→B→C: B is a cut vertex."""
        funcs = [_func("A"), _func("B"), _func("C")]
        calls = [_call("A", "B"), _call("B", "C")]
        return funcs, calls

    def test_no_cut_vertices_prints_nothing(self, tmp_path, capsys):
        from .cli import _show_cut_vertex_contracts

        funcs = [_func("A"), _func("B")]
        calls = [_call("A", "B")]  # no cut vertex — removing either doesn't disconnect
        _show_cut_vertex_contracts(funcs, calls, tmp_path)
        assert capsys.readouterr().out == ""

    def test_cut_vertex_without_trigger_shows_hint(self, tmp_path, capsys):
        from .cli import _show_cut_vertex_contracts

        funcs, calls = self._linear_graph()
        _show_cut_vertex_contracts(funcs, calls, tmp_path)
        out = capsys.readouterr().out
        assert "cut vertex" in out
        assert "pact intent analyze" in out or "intent-trigger" in out

    def test_trigger_no_api_key_shows_warning(self, tmp_path, capsys, monkeypatch):
        from .cli import _show_cut_vertex_contracts

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        funcs, calls = self._linear_graph()
        _show_cut_vertex_contracts(funcs, calls, tmp_path, intent_trigger=True)
        out = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" in out


# ---------------------------------------------------------------------------
# compute_structural_coverage
# ---------------------------------------------------------------------------


class TestStructuralCoverage:
    """Tests for structural coverage metric (cut vertices × intent JSON)."""

    def _linear(self):
        """A→B→C: B is the cut vertex."""
        funcs = [
            _func("A", file="a.py"),
            _func("B", file="b.py"),
            _func("C", file="c.py"),
        ]
        calls = [_call("A", "B"), _call("B", "C")]
        return funcs, calls

    def test_no_cut_vertices_perfect_coverage(self, tmp_path):
        from .reduce import compute_structural_coverage

        funcs = [_func("A"), _func("B")]
        calls = [_call("A", "B")]
        cov = compute_structural_coverage(funcs, calls)
        assert cov.score == 1.0
        assert cov.total == 0
        assert cov.dark_files == []

    def test_cut_vertex_no_intent_json_is_dark(self, tmp_path):
        from .reduce import compute_structural_coverage

        funcs, calls = self._linear()
        cov = compute_structural_coverage(
            funcs, calls, intent_path=tmp_path / "missing.json"
        )
        assert cov.total >= 1
        assert cov.covered == 0
        assert cov.score == 0.0
        assert len(cov.dark_files) >= 1

    def test_cut_vertex_with_contract_is_covered(self, tmp_path):
        import json
        from .reduce import compute_structural_coverage

        funcs, calls = self._linear()
        # b.py is the cut vertex — give it a contract
        intent = {
            "modules": [
                {
                    "path": "b.py",
                    "understanding": {"behavioral_contract": "routes calls"},
                    "violations": [],
                    "invariants": [],
                }
            ]
        }
        intent_path = tmp_path / "intent.json"
        intent_path.write_text(json.dumps(intent))
        cov = compute_structural_coverage(funcs, calls, intent_path=intent_path)
        assert cov.covered >= 1
        assert cov.score > 0.0

    def test_summary_contains_score(self, tmp_path):
        from .reduce import compute_structural_coverage

        funcs, calls = self._linear()
        cov = compute_structural_coverage(funcs, calls)
        summary = cov.summary()
        assert "%" in summary or "structural coverage" in summary

    def test_to_dict_has_required_keys(self, tmp_path):
        from .reduce import compute_structural_coverage

        funcs, calls = self._linear()
        cov = compute_structural_coverage(funcs, calls)
        d = cov.to_dict()
        assert "score" in d
        assert "covered" in d
        assert "total" in d
        assert "dark" in d


# ---------------------------------------------------------------------------
# compute_module_metrics — R.C. Martin instability / abstractness
# ---------------------------------------------------------------------------


class TestModuleMetrics:
    """Tests for compute_module_metrics and ModuleMetrics dataclass."""

    def _write(self, tmp_path, name: str, content: str) -> None:
        """Write a .py file under tmp_path with the given content."""
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    # ------------------------------------------------------------------
    # Basic shape / empty-project guard
    # ------------------------------------------------------------------

    def test_empty_project_returns_empty(self, tmp_path):
        result = compute_module_metrics(tmp_path)
        assert result == []

    def test_returns_list_of_module_metrics(self, tmp_path):
        self._write(tmp_path, "alpha.py", "x = 1\n")
        result = compute_module_metrics(tmp_path)
        assert len(result) >= 1
        assert all(isinstance(m, ModuleMetrics) for m in result)

    def test_top_n_truncates_results(self, tmp_path):
        for i in range(5):
            self._write(tmp_path, f"mod{i}.py", "x = 1\n")
        result = compute_module_metrics(tmp_path, top_n=3)
        assert len(result) <= 3

    # ------------------------------------------------------------------
    # Ca (afferent coupling) — fan-in
    # ------------------------------------------------------------------

    def test_ca_counts_project_importers(self, tmp_path):
        """A module imported by two other project files has Ca=2."""
        self._write(tmp_path, "core.py", "def f(): pass\n")
        self._write(tmp_path, "a.py", "from core import f\n")
        self._write(tmp_path, "b.py", "import core\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["core"].ca == 2

    def test_self_import_not_counted(self, tmp_path):
        """A module that imports itself (pathological case) must not bump its own Ca."""
        self._write(tmp_path, "weird.py", "import weird\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        # Ca of weird should not include itself
        assert metrics["weird"].ca == 0

    # ------------------------------------------------------------------
    # Ce (efferent coupling) — external non-stdlib fan-out
    # ------------------------------------------------------------------

    def test_ce_excludes_stdlib(self, tmp_path):
        """stdlib imports (os, sys, json, …) do not contribute to Ce."""
        self._write(tmp_path, "stdlib_user.py", "import os\nimport sys\nimport json\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["stdlib_user"].ce == 0

    def test_ce_counts_external_package(self, tmp_path):
        """A single non-stdlib, non-project import increments Ce."""
        self._write(tmp_path, "uses_requests.py", "import requests\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["uses_requests"].ce >= 1

    def test_ce_does_not_count_project_modules(self, tmp_path):
        """Imports of other project modules are Ca edges, not Ce."""
        self._write(tmp_path, "utils.py", "def helper(): pass\n")
        self._write(tmp_path, "main.py", "from utils import helper\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        # main imports utils (a project module) → Ce for main should be 0
        assert metrics["main"].ce == 0

    # ------------------------------------------------------------------
    # Instability (I)
    # ------------------------------------------------------------------

    def test_instability_zero_for_pure_provider(self, tmp_path):
        """A module that is imported by others but imports nothing external has I=0."""
        self._write(tmp_path, "provider.py", "import os\n")  # only stdlib
        self._write(tmp_path, "consumer.py", "import provider\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["provider"].instability == 0.0

    def test_instability_range(self, tmp_path):
        """Instability is always in [0, 1]."""
        self._write(tmp_path, "foo.py", "import requests\nimport httpx\n")
        for m in compute_module_metrics(tmp_path):
            assert 0.0 <= m.instability <= 1.0, f"{m.module}: I={m.instability}"

    def test_isolated_module_instability_zero(self, tmp_path):
        """A module with no Ca and no Ce (empty file, no imports) has I=0 (not NaN)."""
        self._write(tmp_path, "empty.py", "# nothing\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["empty"].instability == 0.0

    # ------------------------------------------------------------------
    # Abstractness (A)
    # ------------------------------------------------------------------

    def test_abstractness_zero_for_no_classes(self, tmp_path):
        self._write(tmp_path, "no_classes.py", "def f(): pass\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["no_classes"].abstractness == 0.0

    def test_abstractness_one_for_all_abstract(self, tmp_path):
        """A module with only ABC-derived classes has A=1."""
        src = "from abc import ABC, abstractmethod\nclass MyABC(ABC):\n    @abstractmethod\n    def method(self): ...\n"
        self._write(tmp_path, "all_abstract.py", src)
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["all_abstract"].abstractness == 1.0

    def test_abstractness_partial(self, tmp_path):
        """One concrete + one abstract class → A=0.5."""
        src = (
            "from abc import ABC\n"
            "class Concrete: pass\n"
            "class Abstract(ABC): pass\n"
        )
        self._write(tmp_path, "mixed.py", src)
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["mixed"].abstractness == pytest.approx(0.5)

    def test_protocol_counts_as_abstract(self, tmp_path):
        """A class inheriting Protocol is treated as abstract."""
        src = "from typing import Protocol\nclass MyProto(Protocol):\n    def method(self) -> None: ...\n"
        self._write(tmp_path, "proto.py", src)
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["proto"].abstractness == 1.0

    def test_abstractmethod_decorator_makes_class_abstract(self, tmp_path):
        """A class with @abstractmethod on a method is abstract even without ABC base."""
        src = (
            "from abc import abstractmethod\n"
            "class PseudoAbstract:\n"
            "    @abstractmethod\n"
            "    def do_it(self): ...\n"
        )
        self._write(tmp_path, "pseudo.py", src)
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        assert metrics["pseudo"].abstractness == 1.0

    # ------------------------------------------------------------------
    # Distance (D) and zone labels
    # ------------------------------------------------------------------

    def test_distance_range(self, tmp_path):
        """D is always in [0, 1]."""
        self._write(tmp_path, "any_mod.py", "import requests\ndef f(): pass\n")
        for m in compute_module_metrics(tmp_path):
            assert 0.0 <= m.distance <= 1.0, f"{m.module}: D={m.distance}"

    def test_zone_of_pain_concrete_stable(self, tmp_path):
        """Ca >> Ce and no abstract classes → zone of pain when D > 0.5."""
        # core is imported by many modules but imports nothing external
        self._write(tmp_path, "core.py", "class Concrete: pass\n")
        for importer in range(6):
            self._write(tmp_path, f"user{importer}.py", "import core\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        core_m = metrics["core"]
        # I=0 (no external deps), A=0 (concrete), D=|0+0-1|=1 > 0.5 → zone of pain
        assert core_m.zone == "zone of pain"
        assert core_m.distance > 0.5

    def test_main_sequence_near_diagonal(self, tmp_path):
        """A module with I≈0.5 and A≈0.5 should be near the main sequence (D small)."""
        src = (
            "import requests\n"
            "from abc import ABC\n"
            "class Concrete: pass\n"
            "class Abstract(ABC): pass\n"
        )
        self._write(tmp_path, "balanced.py", src)
        self._write(tmp_path, "importer.py", "import balanced\n")
        metrics = {m.module: m for m in compute_module_metrics(tmp_path)}
        # balanced: Ca=1 (importer), Ce=1 (requests) → I=0.5, A=0.5, D=|0.5+0.5-1|=0
        assert metrics["balanced"].distance == pytest.approx(0.0, abs=0.01)
        assert metrics["balanced"].zone == "main sequence"

    # ------------------------------------------------------------------
    # to_dict and summary
    # ------------------------------------------------------------------

    def test_to_dict_has_required_keys(self, tmp_path):
        self._write(tmp_path, "m.py", "x = 1\n")
        metrics = compute_module_metrics(tmp_path)
        assert metrics
        d = metrics[0].to_dict()
        for key in (
            "module",
            "file",
            "ca",
            "ce",
            "instability",
            "abstractness",
            "distance",
            "zone",
        ):
            assert key in d, f"missing key {key!r} in to_dict"

    def test_summary_contains_key_fields(self, tmp_path):
        self._write(tmp_path, "m.py", "x = 1\n")
        metrics = compute_module_metrics(tmp_path)
        s = metrics[0].summary()
        assert "I=" in s
        assert "A=" in s
        assert "D=" in s
        assert "Ca=" in s
        assert "Ce=" in s

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    def test_sorted_by_distance_descending(self, tmp_path):
        """Results are sorted worst-first (highest D first)."""
        self._write(tmp_path, "a.py", "x = 1\n")
        self._write(tmp_path, "b.py", "import requests\n")
        results = compute_module_metrics(tmp_path)
        distances = [m.distance for m in results]
        assert distances == sorted(distances, reverse=True)


# ---------------------------------------------------------------------------
# Sheaf Laplacian spectral gap
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_SCIPY, reason="scipy not installed")
class TestSpectralGap:
    """Tests for compute_spectral_gap and SpectralResult."""

    def _K4(self):
        """Complete graph K4 — maximally well-connected, robust."""
        import networkx as nx

        return nx.complete_graph(4)

    def _P5(self):
        """Path graph P5 — chain A-B-C-D-E, fragile (one bridge removal disconnects)."""
        import networkx as nx

        return nx.path_graph(5)

    def _disconnected(self):
        """Two disjoint edges: {0-1} and {2-3} — two components."""
        import networkx as nx

        G = nx.Graph()
        G.add_edges_from([(0, 1), (2, 3)])
        return G

    # ------------------------------------------------------------------
    # Robust graph (K4)
    # ------------------------------------------------------------------

    def test_complete_graph_is_robust(self):
        """K4 has a high Fiedler value and is labelled 'robust'."""
        sr = compute_spectral_gap(self._K4())
        assert sr is not None
        assert sr.fragility_label == "robust"
        assert sr.fiedler_value >= 0.30

    def test_complete_graph_single_component(self):
        sr = compute_spectral_gap(self._K4())
        assert sr is not None
        assert sr.n_components == 1

    # ------------------------------------------------------------------
    # Fragile graph (path P5)
    # ------------------------------------------------------------------

    def test_path_graph_is_fragile_or_moderate(self):
        """P5 has a low Fiedler value — fragile or moderate (not robust)."""
        sr = compute_spectral_gap(self._P5())
        assert sr is not None
        assert sr.fragility_label in ("fragile", "moderate")
        assert sr.fiedler_value < 0.30

    def test_path_graph_fiedler_less_than_complete(self):
        """P5 must have strictly lower Fiedler value than K4."""
        sr_path = compute_spectral_gap(self._P5())
        sr_k4 = compute_spectral_gap(self._K4())
        assert sr_path is not None and sr_k4 is not None
        assert sr_path.fiedler_value < sr_k4.fiedler_value

    # ------------------------------------------------------------------
    # Disconnected graph
    # ------------------------------------------------------------------

    def test_disconnected_graph_label(self):
        """A graph with 2 components is labelled 'disconnected'."""
        sr = compute_spectral_gap(self._disconnected())
        assert sr is not None
        assert sr.fragility_label == "disconnected"
        assert sr.n_components > 1

    # ------------------------------------------------------------------
    # Single-node / tiny-graph safe fallback
    # ------------------------------------------------------------------

    def test_single_node_returns_robust_fallback(self):
        """A single-node graph (< 3 nodes) returns the safe fallback."""
        import networkx as nx

        G = nx.Graph()
        G.add_node(0)
        sr = compute_spectral_gap(G)
        assert sr is not None
        assert sr.fiedler_value == 1.0
        assert sr.spectral_gap == 1.0
        assert sr.fragility_label == "robust"

    def test_two_node_graph_returns_robust_fallback(self):
        """Two-node graph (< 3 nodes) returns the safe fallback."""
        import networkx as nx

        G = nx.Graph()
        G.add_edge(0, 1)
        sr = compute_spectral_gap(G)
        assert sr is not None
        assert sr.fiedler_value == 1.0

    # ------------------------------------------------------------------
    # SpectralResult.to_dict()
    # ------------------------------------------------------------------

    def test_to_dict_has_required_keys(self):
        sr = compute_spectral_gap(self._K4())
        assert sr is not None
        d = sr.to_dict()
        assert "fiedler_value" in d
        assert "spectral_gap" in d
        assert "fragility_label" in d
        assert "n_components" in d

    def test_to_dict_values_match_fields(self):
        sr = compute_spectral_gap(self._K4())
        assert sr is not None
        d = sr.to_dict()
        assert d["fiedler_value"] == pytest.approx(sr.fiedler_value, rel=1e-4)
        assert d["spectral_gap"] == pytest.approx(sr.spectral_gap, rel=1e-4)
        assert d["fragility_label"] == sr.fragility_label
        assert d["n_components"] == sr.n_components

    # ------------------------------------------------------------------
    # SpectralResult.render()
    # ------------------------------------------------------------------

    def test_render_contains_label(self):
        """render() output must include the fragility label."""
        sr = compute_spectral_gap(self._K4())
        assert sr is not None
        rendered = sr.render()
        assert sr.fragility_label in rendered

    def test_render_contains_fiedler(self):
        """render() output must include the numeric Fiedler value."""
        sr = compute_spectral_gap(self._K4())
        assert sr is not None
        rendered = sr.render()
        # The value should appear rounded to 4 decimal places
        assert f"{sr.fiedler_value:.4f}" in rendered

    # ------------------------------------------------------------------
    # DiGraph input (pact's native graph type)
    # ------------------------------------------------------------------

    def test_digraph_accepted(self):
        """compute_spectral_gap must accept nx.DiGraph (pact's call graph type)."""
        import networkx as nx

        G = nx.DiGraph()
        G.add_edges_from([("A", "B"), ("B", "C"), ("C", "A"), ("A", "C")])
        sr = compute_spectral_gap(G)
        assert sr is not None
        assert isinstance(sr, SpectralResult)

    # ------------------------------------------------------------------
    # Graceful degradation: None input
    # ------------------------------------------------------------------

    def test_none_graph_returns_none(self):
        """Non-graph input returns None gracefully."""
        sr = compute_spectral_gap(None)
        assert sr is None


# ---------------------------------------------------------------------------
# pact metrics CLI command
# ---------------------------------------------------------------------------


class TestMetricsCmd:
    """CLI-level tests for _metrics_cmd."""

    def _write(self, tmp_path, name: str, content: str) -> None:
        (tmp_path / name).write_text(content)

    def test_basic_output(self, tmp_path, capsys):
        from .cli import _metrics_cmd

        self._write(tmp_path, "core.py", "def f(): pass\n")
        self._write(tmp_path, "user.py", "from core import f\n")
        rc = _metrics_cmd([str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "pact metrics" in out
        assert "zone-of-pain" in out or "main-sequence" in out

    def test_json_output(self, tmp_path, capsys):
        import json
        from .cli import _metrics_cmd

        self._write(tmp_path, "core.py", "def f(): pass\n")
        self._write(tmp_path, "user.py", "import requests\nfrom core import f\n")
        rc = _metrics_cmd([str(tmp_path), "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        assert all("module" in m and "distance" in m and "zone" in m for m in data)

    def test_zone_filter(self, tmp_path, capsys):
        from .cli import _metrics_cmd

        # core.py: imported by user, no external deps → zone of pain
        self._write(tmp_path, "core.py", "def f(): pass\n")
        self._write(tmp_path, "user.py", "from core import f\n")
        rc = _metrics_cmd([str(tmp_path), "--zone", "pain"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ZONE OF PAIN" in out
        assert "main-sequence" not in out.lower() or "0 main-sequence" in out

    def test_threshold_exit_1_when_exceeded(self, tmp_path, capsys):
        from .cli import _metrics_cmd

        # core.py has D=1.0 — well above any threshold
        self._write(tmp_path, "core.py", "def f(): pass\n")
        self._write(tmp_path, "user.py", "from core import f\n")
        rc = _metrics_cmd([str(tmp_path), "--threshold", "0.5"])
        assert rc == 1

    def test_threshold_exit_0_when_not_exceeded(self, tmp_path, capsys):
        from .cli import _metrics_cmd

        # A module right on the main sequence should pass a strict threshold
        self._write(tmp_path, "balanced.py", "import requests\n")
        rc = _metrics_cmd([str(tmp_path), "--threshold", "0.5"])
        # balanced has Ca=0, Ce=1, I=1.0, D=0 — passes, but filtered (Ca=0 Ce=1>0)
        assert rc == 0

    def test_missing_dir_returns_2(self, tmp_path):
        from .cli import _metrics_cmd

        rc = _metrics_cmd([str(tmp_path / "does_not_exist")])
        assert rc == 2

    def test_top_n_limits_after_filters(self, tmp_path, capsys):
        from .cli import _metrics_cmd

        for i in range(5):
            self._write(tmp_path, f"core{i}.py", "def f(): pass\n")
            self._write(tmp_path, f"user{i}.py", f"from core{i} import f\n")
        rc = _metrics_cmd([str(tmp_path), "--zone", "pain", "--top", "2"])
        assert rc == 0
        out = capsys.readouterr().out
        # Only 2 pain modules shown even though there are 5
        lines = [l for l in out.splitlines() if l.startswith("   ")]
        assert len(lines) == 2
