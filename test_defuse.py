"""
Tests for defuse.py -- variable-level taint analysis via beniget def-use chains.

beniget is an optional dependency.  We guard the import so that if it is not
installed the TaintFlow / to_dict / render tests still run (they don't need
beniget), and the integration tests are skipped.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

try:
    import beniget  # noqa: F401

    _BENIGET_AVAILABLE = True
except ImportError:
    _BENIGET_AVAILABLE = False

_skip_no_beniget = pytest.mark.skipif(
    not _BENIGET_AVAILABLE, reason="beniget not installed"
)

# ---------------------------------------------------------------------------
# Import under test — always available (pure-Python dataclasses)
# ---------------------------------------------------------------------------

from pact.defuse import TaintFlow, build_defuse  # noqa: E402

# ---------------------------------------------------------------------------
# TaintFlow unit tests — no beniget required
# ---------------------------------------------------------------------------


class TestTaintFlowRender:
    def _make(self, **kw) -> TaintFlow:
        defaults = dict(
            variable="api_key",
            defined_at=12,
            used_at=45,
            use_context="call",
            sink_name="logging.info",
            confidence=1.0,
        )
        defaults.update(kw)
        return TaintFlow(**defaults)

    def test_render_format_call(self):
        tf = self._make()
        r = tf.render()
        assert "api_key" in r
        assert "line 12" in r
        assert "line 45" in r
        assert "logging.info" in r
        assert "100%" in r

    def test_render_format_assign(self):
        tf = self._make(use_context="assign", sink_name="", confidence=0.8)
        r = tf.render()
        assert "assign" in r
        assert "80%" in r
        # sink_name is empty so no call-site label
        assert "call to" not in r

    def test_render_format_return(self):
        tf = self._make(use_context="return", sink_name="")
        r = tf.render()
        assert "return" in r

    def test_render_no_sink_when_not_call(self):
        tf = self._make(use_context="subscript", sink_name="")
        r = tf.render()
        assert "subscript" in r
        assert "call to" not in r


class TestTaintFlowToDict:
    def _make(self) -> TaintFlow:
        return TaintFlow(
            variable="password",
            defined_at=3,
            used_at=7,
            use_context="call",
            sink_name="send",
            confidence=0.8,
        )

    def test_to_dict_has_required_keys(self):
        d = self._make().to_dict()
        for key in (
            "variable",
            "defined_at",
            "used_at",
            "use_context",
            "sink_name",
            "confidence",
            "render",
        ):
            assert key in d, f"missing key: {key}"

    def test_to_dict_values(self):
        d = self._make().to_dict()
        assert d["variable"] == "password"
        assert d["defined_at"] == 3
        assert d["used_at"] == 7
        assert d["use_context"] == "call"
        assert d["sink_name"] == "send"
        assert abs(d["confidence"] - 0.8) < 1e-9

    def test_to_dict_render_is_string(self):
        d = self._make().to_dict()
        assert isinstance(d["render"], str)
        assert len(d["render"]) > 0

    def test_to_dict_no_private_fields(self):
        d = self._make().to_dict()
        # _chain_depth must NOT appear in the public dict
        assert "_chain_depth" not in d


# ---------------------------------------------------------------------------
# DefUseGraph integration tests — require beniget
# ---------------------------------------------------------------------------


@_skip_no_beniget
class TestDefUseGraphBasic:
    def test_direct_call_flow(self):
        """api_key = get_key(); log(api_key) → TaintFlow with sink_name='log'"""
        src = "api_key = get_key()\nlog(api_key)"
        g = build_defuse(src)
        flows = g.taint_flows(["api_key", "token", "secret", "password", "key"])
        call_flows = [f for f in flows if f.use_context == "call"]
        assert len(call_flows) >= 1
        assert any(f.sink_name == "log" for f in call_flows)
        match = next(f for f in call_flows if f.sink_name == "log")
        assert match.defined_at == 1
        assert match.used_at == 2
        assert match.variable == "api_key"

    def test_non_matching_variable_no_flows(self):
        """A variable whose name doesn't match source_names produces no flows."""
        src = "username = get_user()\nlog(username)"
        g = build_defuse(src)
        flows = g.taint_flows(["api_key", "token", "secret"])
        assert flows == []

    def test_syntax_error_returns_empty(self):
        """SyntaxError in source returns empty list."""
        g = build_defuse("def broken(")
        flows = g.taint_flows(["api_key"])
        assert flows == []

    def test_through_assign_chain(self):
        """api_key -> x -> log(x): two-hop flow should find log with ≤1.0 confidence."""
        src = "api_key = get_key()\nx = api_key\nlog(x)"
        g = build_defuse(src)
        flows = g.taint_flows(["api_key"])
        call_flows = [
            f for f in flows if f.use_context == "call" and f.sink_name == "log"
        ]
        assert len(call_flows) >= 1
        match = call_flows[0]
        assert match.variable == "api_key"
        assert match.defined_at == 1
        assert match.used_at == 3
        assert match.confidence < 1.0  # reduced by pass-through assign

    def test_depth_limit(self):
        """A chain longer than 5 hops does not produce a call flow at the sink."""
        # 7 hops: api_key -> a -> b -> c -> d -> e -> f -> log(f)
        lines = ["api_key = get_key()"]
        prev = "api_key"
        for letter in "abcdef":
            lines.append(f"{letter} = {prev}")
            prev = letter
        lines.append(f"log({prev})")
        src = "\n".join(lines)
        g = build_defuse(src)
        flows = g.taint_flows(["api_key"])
        # The log call is at hop 7 — beyond _MAX_DEPTH=5 — must NOT appear
        call_to_log = [
            f for f in flows if f.use_context == "call" and f.sink_name == "log"
        ]
        assert (
            call_to_log == []
        ), f"Depth limit not enforced: found call flows {call_to_log}"

    def test_case_insensitive_match(self):
        """Matching is case-insensitive (API_KEY matches 'api_key' pattern)."""
        src = "API_KEY = get_key()\nlog(API_KEY)"
        g = build_defuse(src)
        flows = g.taint_flows(["api_key"])
        assert any(f.use_context == "call" for f in flows)

    def test_attribute_call_sink_name(self):
        """logging.info(api_key) → sink_name == 'info'."""
        src = "api_key = get_key()\nlogging.info(api_key)"
        g = build_defuse(src)
        flows = g.taint_flows(["api_key"])
        call_flows = [f for f in flows if f.use_context == "call"]
        assert any(f.sink_name == "info" for f in call_flows)

    def test_to_dict_fields_present(self):
        """Each TaintFlow from taint_flows has a valid to_dict."""
        src = "secret = get_secret()\nsend(secret)"
        g = build_defuse(src)
        flows = g.taint_flows(["secret"])
        assert len(flows) > 0
        for tf in flows:
            d = tf.to_dict()
            for key in (
                "variable",
                "defined_at",
                "used_at",
                "use_context",
                "sink_name",
                "confidence",
                "render",
            ):
                assert key in d


@_skip_no_beniget
class TestDefUseGraphEdgeCases:
    def test_empty_source(self):
        g = build_defuse("")
        assert g.taint_flows(["api_key"]) == []

    def test_no_sources(self):
        src = "x = 1\nlog(x)"
        g = build_defuse(src)
        assert g.taint_flows([]) == []

    def test_return_context(self):
        """A variable returned from a function is classified as 'return'."""
        src = "def f():\n    api_key = get_key()\n    return api_key"
        g = build_defuse(src)
        flows = g.taint_flows(["api_key"])
        return_flows = [f for f in flows if f.use_context == "return"]
        assert len(return_flows) >= 1
