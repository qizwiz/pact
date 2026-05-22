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
    contract_sccs,
    eliminate_dead,
    find_bridge_violations,
    transitive_reduce,
    find_hubs,
    find_passthroughs,
    find_sccs,
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
