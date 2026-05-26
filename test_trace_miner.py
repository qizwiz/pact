"""
Tests for trace_miner.py — Daikon-style execution trace miner.

All tests run without external processes; subprocess.run is mocked where needed.
"""

from __future__ import annotations

import pickle
import sys
from unittest.mock import MagicMock, patch


from .trace_miner import (
    MinedInvariant,
    _eval_templates_for_observations,
    _filter_to_project,
    _find_python,
    _pytest_args_from_cmd,
    check_identity,
    check_membership,
    check_monotone_decrease,
    check_monotone_increase,
    check_nonneg,
    check_not_none,
    check_pair_diff_nonneg,
    check_pair_eq,
    check_pair_leq,
    check_valid_len,
    mine_invariants,
)

# ---------------------------------------------------------------------------
# MinedInvariant dataclass
# ---------------------------------------------------------------------------


class TestMinedInvariantRender:
    def test_render_universal(self):
        inv = MinedInvariant(
            function="mymod::my_func",
            variable="result",
            template="result >= 0",
            confidence=1.0,
            call_count=10,
        )
        rendered = inv.render()
        assert "empirically_mined" in rendered
        assert "mymod::my_func" in rendered
        assert "result >= 0" in rendered
        assert "n=10" in rendered
        assert "100%" in rendered

    def test_render_partial_confidence(self):
        inv = MinedInvariant(
            function="mod::fn",
            variable="x",
            template="x is not None",
            confidence=0.75,
            call_count=4,
        )
        rendered = inv.render()
        assert "75%" in rendered

    def test_render_includes_type(self):
        inv = MinedInvariant(
            function="mod::fn",
            variable="result",
            template="result >= 0",
            confidence=1.0,
            call_count=5,
        )
        assert inv.type == "empirically_mined"
        assert "empirically_mined" in inv.render()


class TestMinedInvariantToDict:
    def test_has_required_keys(self):
        inv = MinedInvariant(
            function="mymod::f",
            variable="result",
            template="result >= 0",
            confidence=1.0,
            call_count=7,
        )
        d = inv.to_dict()
        assert set(d.keys()) == {
            "function",
            "variable",
            "template",
            "confidence",
            "call_count",
            "type",
        }

    def test_values_correct(self):
        inv = MinedInvariant(
            function="mod::fn",
            variable="x",
            template="x is not None",
            confidence=0.9,
            call_count=20,
        )
        d = inv.to_dict()
        assert d["function"] == "mod::fn"
        assert d["variable"] == "x"
        assert d["template"] == "x is not None"
        assert d["confidence"] == 0.9
        assert d["call_count"] == 20
        assert d["type"] == "empirically_mined"

    def test_type_field_default(self):
        inv = MinedInvariant(
            function="a::b",
            variable="r",
            template="r >= 0",
            confidence=1.0,
            call_count=3,
        )
        assert inv.to_dict()["type"] == "empirically_mined"


# ---------------------------------------------------------------------------
# Template checker functions (pure unit tests)
# ---------------------------------------------------------------------------


class TestTemplateCheckers:
    # check_nonneg
    def test_nonneg_positive(self):
        assert check_nonneg(5) is True

    def test_nonneg_zero(self):
        assert check_nonneg(0) is True

    def test_nonneg_negative(self):
        assert check_nonneg(-1) is False

    def test_nonneg_non_numeric(self):
        assert check_nonneg("hello") is False

    def test_nonneg_none(self):
        assert check_nonneg(None) is False

    # check_not_none
    def test_not_none_value(self):
        assert check_not_none(0) is True
        assert check_not_none("") is True
        assert check_not_none([]) is True

    def test_not_none_none(self):
        assert check_not_none(None) is False

    # check_valid_len
    def test_valid_len_list(self):
        assert check_valid_len([1, 2, 3]) is True

    def test_valid_len_empty(self):
        assert check_valid_len([]) is True

    def test_valid_len_string(self):
        assert check_valid_len("abc") is True

    def test_valid_len_int(self):
        assert check_valid_len(42) is False

    def test_valid_len_none(self):
        assert check_valid_len(None) is False

    # check_identity
    def test_identity_equal(self):
        assert check_identity(5, 5) is True

    def test_identity_not_equal(self):
        assert check_identity(5, 6) is False

    def test_identity_list(self):
        assert check_identity([1, 2], [1, 2]) is True

    # check_monotone_increase
    def test_monotone_increase_greater(self):
        assert check_monotone_increase(10, 5) is True

    def test_monotone_increase_equal(self):
        assert check_monotone_increase(5, 5) is True

    def test_monotone_increase_less(self):
        assert check_monotone_increase(3, 5) is False

    # check_monotone_decrease
    def test_monotone_decrease_less(self):
        assert check_monotone_decrease(3, 5) is True

    def test_monotone_decrease_equal(self):
        assert check_monotone_decrease(5, 5) is True

    def test_monotone_decrease_greater(self):
        assert check_monotone_decrease(7, 5) is False

    # check_membership
    def test_membership_in_list(self):
        assert check_membership(2, [1, 2, 3]) is True

    def test_membership_not_in_list(self):
        assert check_membership(5, [1, 2, 3]) is False

    def test_membership_non_iterable(self):
        assert check_membership(2, 5) is False

    # check_pair_leq
    def test_pair_leq_less(self):
        assert check_pair_leq(1, 2) is True

    def test_pair_leq_equal(self):
        assert check_pair_leq(2, 2) is True

    def test_pair_leq_greater(self):
        assert check_pair_leq(3, 2) is False

    # check_pair_eq
    def test_pair_eq_equal(self):
        assert check_pair_eq(5, 5) is True

    def test_pair_eq_not_equal(self):
        assert check_pair_eq(5, 6) is False

    # check_pair_diff_nonneg
    def test_pair_diff_nonneg_positive(self):
        assert check_pair_diff_nonneg(1, 5) is True  # y - x = 4

    def test_pair_diff_nonneg_zero(self):
        assert check_pair_diff_nonneg(3, 3) is True

    def test_pair_diff_nonneg_negative(self):
        assert check_pair_diff_nonneg(5, 1) is False  # y - x = -4


# ---------------------------------------------------------------------------
# _eval_templates_for_observations
# ---------------------------------------------------------------------------


def _make_obs(func: str, return_value, enter_args: dict, raised: bool = False) -> dict:
    return {
        "function": func,
        "enter_args": enter_args,
        "return_value": return_value,
        "raised": raised,
    }


class TestEvalTemplates:
    def test_minimum_3_obs_required(self):
        obs = [_make_obs("m::f", 5, {"x": 1}), _make_obs("m::f", 6, {"x": 2})]
        result = _eval_templates_for_observations(obs)
        assert result == []

    def test_nonneg_result_universally_holds(self):
        obs = [_make_obs("m::f", i, {"x": i}) for i in range(5)]
        invs = _eval_templates_for_observations(obs)
        templates = [i.template for i in invs]
        assert "result >= 0" in templates

    def test_nonneg_result_fails_when_negative(self):
        obs = [_make_obs("m::f", i - 2, {"x": i}) for i in range(5)]
        invs = _eval_templates_for_observations(obs)
        templates = [i.template for i in invs]
        assert "result >= 0" not in templates

    def test_raised_observations_excluded(self):
        # 5 obs but 3 raised — only 2 clean, below threshold
        obs = [_make_obs("m::f", 5, {}, raised=True) for _ in range(3)]
        obs += [_make_obs("m::f", 5, {}) for _ in range(2)]
        invs = _eval_templates_for_observations(obs)
        assert invs == []

    def test_identity_template(self):
        # function returns its first arg unchanged
        obs = [_make_obs("m::f", v, {"x": v}) for v in [1, 2, 3, 4]]
        invs = _eval_templates_for_observations(obs)
        templates = [i.template for i in invs]
        assert "result == x_enter" in templates

    def test_monotone_increase_detected(self):
        # result is always x + 10, so result >= x_enter
        obs = [_make_obs("m::f", v + 10, {"x": v}) for v in range(5)]
        invs = _eval_templates_for_observations(obs)
        templates = [i.template for i in invs]
        assert "result >= x_enter" in templates

    def test_pair_leq_detected(self):
        # always a <= b
        obs = [_make_obs("m::f", None, {"a": i, "b": i + 5}) for i in range(5)]
        invs = _eval_templates_for_observations(obs)
        templates = [i.template for i in invs]
        assert "a <= b" in templates

    def test_confidence_is_1_for_universal(self):
        obs = [_make_obs("m::f", i, {"x": i}) for i in range(5)]
        invs = _eval_templates_for_observations(obs)
        for inv in invs:
            assert inv.confidence == 1.0

    def test_call_count_correct(self):
        obs = [_make_obs("m::f", i, {}) for i in range(7)]
        invs = _eval_templates_for_observations(obs)
        # Some templates should hold; all should have call_count=7
        for inv in invs:
            assert inv.call_count == 7

    def test_none_result_passes_not_none_when_universal(self):
        # If all results are not None, the template should hold
        obs = [_make_obs("m::f", i + 1, {}) for i in range(5)]
        invs = _eval_templates_for_observations(obs)
        templates = [i.template for i in invs]
        assert "result is not None" in templates


# ---------------------------------------------------------------------------
# _filter_to_project — stdlib filtering
# ---------------------------------------------------------------------------


class TestFilterToProject:
    def test_stdlib_filtered_out(self, tmp_path):
        stdlib = getattr(sys, "stdlib_module_names", frozenset({"os", "sys", "re"}))
        raw = {
            "os::path": [_make_obs("os::path", None, {})],
            "re::compile": [_make_obs("re::compile", None, {})],
            "mymod::func": [_make_obs("mymod::func", 1, {})],
        }
        filtered = _filter_to_project(raw, tmp_path)
        for key in filtered:
            pkg = key.split("::")[0].split(".")[0]
            assert pkg not in stdlib, f"stdlib module {pkg} was not filtered"

    def test_pytest_filtered_out(self, tmp_path):
        raw = {
            "_pytest.runner::func": [],
            "pytest::main": [],
            "usermod::f": [_make_obs("usermod::f", 1, {})],
        }
        filtered = _filter_to_project(raw, tmp_path)
        assert "_pytest.runner::func" not in filtered
        assert "pytest::main" not in filtered

    def test_user_module_retained(self, tmp_path):
        raw = {
            "mypackage.utils::helper": [_make_obs("mypackage.utils::helper", 1, {})],
        }
        filtered = _filter_to_project(raw, tmp_path)
        assert "mypackage.utils::helper" in filtered


# ---------------------------------------------------------------------------
# mine_invariants — subprocess mocked
# ---------------------------------------------------------------------------


class TestMineInvariantsSubprocess:
    def _make_obs_dict(self, func: str) -> dict:
        """Create a simple observations dict for pickling."""
        return {func: [_make_obs(func, i + 1, {"n": i}) for i in range(5)]}

    def test_returns_empty_when_obs_file_missing(self, tmp_path):
        """If subprocess produces no pickle file, return []."""
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            result = mine_invariants(tmp_path, test_cmd="pytest", timeout=5)

        assert result == []

    def test_returns_invariants_from_observations(self, tmp_path):
        """When subprocess writes a valid pickle, invariants are returned."""
        obs = self._make_obs_dict("usermod::compute")

        def fake_run(cmd, **kwargs):
            # Write observations to the path from env
            out_path = kwargs.get("env", {}).get("TRACE_MINER_OUT")
            if out_path:
                with open(out_path, "wb") as fh:
                    pickle.dump(obs, fh, protocol=4)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=fake_run):
            result = mine_invariants(tmp_path, test_cmd="pytest", timeout=5)

        # Should have mined at least one invariant from "usermod::compute"
        assert isinstance(result, list)
        for inv in result:
            assert isinstance(inv, MinedInvariant)
            assert inv.confidence == 1.0
            assert inv.call_count >= 3

    def test_stdlib_filtered_in_mine_invariants(self, tmp_path):
        """Stdlib functions are excluded from results."""
        obs = {
            "os::getcwd": [_make_obs("os::getcwd", "/home", {}) for _ in range(5)],
            "usermod::f": [_make_obs("usermod::f", i, {}) for i in range(5)],
        }

        def fake_run(cmd, **kwargs):
            out_path = kwargs.get("env", {}).get("TRACE_MINER_OUT")
            if out_path:
                with open(out_path, "wb") as fh:
                    pickle.dump(obs, fh, protocol=4)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            result = mine_invariants(tmp_path, test_cmd="pytest", timeout=5)

        funcs = {inv.function for inv in result}
        assert not any(f.startswith("os::") for f in funcs)


# ---------------------------------------------------------------------------
# _pytest_args_from_cmd
# ---------------------------------------------------------------------------


class TestPytestArgsFromCmd:
    def test_none_returns_defaults(self, tmp_path):
        args = _pytest_args_from_cmd(None, tmp_path)
        assert isinstance(args, list)
        assert len(args) > 0

    def test_strips_pytest_prefix(self, tmp_path):
        args = _pytest_args_from_cmd("pytest tests/ -q", tmp_path)
        assert "pytest" not in args
        assert "tests/" in args

    def test_strips_python_m_pytest(self, tmp_path):
        args = _pytest_args_from_cmd("python -m pytest tests/", tmp_path)
        assert "python" not in args
        assert "-m" not in args
        assert "pytest" not in args
        assert "tests/" in args

    def test_empty_after_strip_uses_defaults(self, tmp_path):
        args = _pytest_args_from_cmd("pytest", tmp_path)
        assert isinstance(args, list)
        assert len(args) > 0


# ---------------------------------------------------------------------------
# _find_python
# ---------------------------------------------------------------------------


class TestFindPython:
    def test_finds_venv_python(self, tmp_path):
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_py = venv_bin / "python"
        fake_py.write_text("#!/bin/sh\necho fake\n")
        result = _find_python(tmp_path)
        assert result == str(fake_py)

    def test_falls_back_to_sys_executable(self, tmp_path):
        # No .venv exists in tmp_path
        result = _find_python(tmp_path)
        assert result == sys.executable
