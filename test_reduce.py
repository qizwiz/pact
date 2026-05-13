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
    find_hubs,
    find_passthroughs,
    find_sccs,
    _build_digraph,
)
from .extractor import FunctionManifest, CallSite, ArgConstraint
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
    return Violation(file=file, line=1, call="x()", missing=["something"], context="optional_dereference")


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

    def test_violation_urgency_increases_score(self):
        """A tangle with violations scores higher than one without."""
        funcs = [_func("A", "a.py"), _func("B", "a.py")]
        calls = [_call("A", "B"), _call("B", "A")]
        viols = [_viol("a.py")]
        G, func_by_name = _build_digraph(funcs, calls)
        result_with = find_sccs(G, func_by_name, viols)
        result_without = find_sccs(G, func_by_name, [])
        assert result_with[0].score > result_without[0].score

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
            _func("cycle_a", "a.py"), _func("cycle_b", "a.py"),  # tangle
            _func("pass_x", "b.py"), _func("pass_src", "b.py"), _func("pass_dst", "b.py"),  # passthrough
            _func("hub", "c.py"),    # hub
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
