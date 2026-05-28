"""Tests for pact pipeline orchestrator (pipeline.py)."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .pipeline import (
    PipelineResult,
    StepResult,
    _execute_heal,
    _intent_summary,
    _render_tla_spec,
    _topo_sort,
    run_pipeline,
)


def _mock_llm(response_text: str):
    """Return a context manager that patches anthropic.Anthropic to return response_text."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=response_text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    return patch("anthropic.Anthropic", return_value=mock_client)


# ---------------------------------------------------------------------------
# _topo_sort
# ---------------------------------------------------------------------------


class TestTopoSort:
    def test_no_deps_preserves_order(self):
        steps = [
            {"step": 1, "tool": "z3", "depends_on": []},
            {"step": 2, "tool": "tla", "depends_on": []},
        ]
        result = _topo_sort(steps)
        assert [s["step"] for s in result] == [1, 2]

    def test_dep_comes_first(self):
        steps = [
            {"step": 2, "tool": "hypothesis", "depends_on": [1]},
            {"step": 1, "tool": "z3", "depends_on": []},
        ]
        result = _topo_sort(steps)
        assert result[0]["step"] == 1
        assert result[1]["step"] == 2

    def test_chain_deps(self):
        steps = [
            {"step": 3, "tool": "heal", "depends_on": [2]},
            {"step": 2, "tool": "hypothesis", "depends_on": [1]},
            {"step": 1, "tool": "z3", "depends_on": []},
        ]
        result = _topo_sort(steps)
        assert [s["step"] for s in result] == [1, 2, 3]

    def test_missing_dep_skipped_gracefully(self):
        steps = [
            {"step": 2, "tool": "hypothesis", "depends_on": [99]},  # dep 99 not in list
        ]
        result = _topo_sort(steps)
        assert len(result) == 1
        assert result[0]["step"] == 2

    def test_no_steps_returns_empty(self):
        assert _topo_sort([]) == []


# ---------------------------------------------------------------------------
# _intent_summary
# ---------------------------------------------------------------------------


class TestIntentSummary:
    def _make_intent(self, modules):
        return {"modules": modules}

    def test_empty_modules_returns_placeholder(self):
        intent = self._make_intent([])
        assert _intent_summary(intent) == "(no actionable findings)"

    def test_module_without_violations_or_obligations_skipped(self):
        intent = self._make_intent(
            [
                {
                    "path": "/a/b/c.py",
                    "understanding": {"behavioral_contract": "does stuff"},
                    "violations": [],
                    "invariants": [],
                }
            ]
        )
        assert _intent_summary(intent) == "(no actionable findings)"

    def test_module_with_violations_included(self):
        intent = self._make_intent(
            [
                {
                    "path": "/a/b/checker.py",
                    "understanding": {
                        "behavioral_contract": "checks constraints",
                        "resource_obligations": "",
                    },
                    "violations": [
                        {"severity": "high", "explanation": "missing guard on response"}
                    ],
                    "invariants": [],
                }
            ]
        )
        summary = _intent_summary(intent)
        assert "checker.py" in summary
        assert "missing guard" in summary

    def test_module_with_obligations_included(self):
        intent = self._make_intent(
            [
                {
                    "path": "/app/worker.py",
                    "understanding": {
                        "behavioral_contract": "spawns processes",
                        "resource_obligations": "each spawned process must be joined",
                    },
                    "violations": [],
                    "invariants": [],
                }
            ]
        )
        summary = _intent_summary(intent)
        assert "worker.py" in summary
        assert "joined" in summary

    def test_high_confidence_intent_gap_included(self):
        intent = self._make_intent(
            [
                {
                    "path": "/app/heal.py",
                    "understanding": {"behavioral_contract": "produces fixes"},
                    "violations": [{"severity": "medium", "explanation": "x"}],
                    "invariants": [
                        {
                            "id": "H1",
                            "type": "intent_gap",
                            "confidence": 0.92,
                            "statement": "heal must verify fix with Z3",
                        }
                    ],
                }
            ]
        )
        summary = _intent_summary(intent)
        assert "heal must verify fix with Z3" in summary

    def test_low_confidence_gap_excluded(self):
        intent = self._make_intent(
            [
                {
                    "path": "/app/heal.py",
                    "understanding": {"behavioral_contract": "produces fixes"},
                    "violations": [{"severity": "low", "explanation": "y"}],
                    "invariants": [
                        {
                            "id": "H2",
                            "type": "intent_gap",
                            "confidence": 0.60,
                            "statement": "should not appear",
                        }
                    ],
                }
            ]
        )
        summary = _intent_summary(intent)
        assert "should not appear" not in summary


# ---------------------------------------------------------------------------
# _render_tla_spec
# ---------------------------------------------------------------------------


class TestRenderTlaSpec:
    def test_resource_lifecycle_contains_module_name(self):
        spec = _render_tla_spec(
            "checker", "run", "process must be joined", "resource_lifecycle"
        )
        assert "checker" in spec
        assert "resource_count" in spec
        assert "ResourceBounded" in spec

    def test_ordering_spec(self):
        spec = _render_tla_spec("pipeline", "execute", "setup before run", "ordering")
        assert "phase" in spec
        assert "OrderingRespected" in spec

    def test_accumulation_spec(self):
        spec = _render_tla_spec(
            "store", "append", "size bounded by MaxSize", "accumulation"
        )
        assert "MaxSize" in spec
        assert "AccumulationBounded" in spec

    def test_liveness_spec_default(self):
        spec = _render_tla_spec(
            "worker", "process", "must eventually finish", "liveness"
        )
        assert "EventualCompletion" in spec
        assert "Reset" in spec

    def test_unknown_template_falls_back_to_liveness(self):
        spec = _render_tla_spec("x", "y", "something", "nonexistent_template")
        assert "EventualCompletion" in spec

    def test_module_name_sanitized(self):
        spec = _render_tla_spec("my-module.v2", "fn", "obs", "resource_lifecycle")
        assert "my_module_v2" in spec
        # Extract just the module name between MODULE and the closing ----
        module_name = spec.split("MODULE")[1].split("----")[0].strip()
        assert "-" not in module_name


# ---------------------------------------------------------------------------
# PipelineResult
# ---------------------------------------------------------------------------


class TestPipelineResult:
    def _make_result(self, statuses):
        results = [
            StepResult(
                step=i + 1, tool="z3", module_path="/a.py", status=s, summary="ok"
            )
            for i, s in enumerate(statuses)
        ]
        return PipelineResult(intent_file="x.json", plan=[], results=results)

    def test_violated_steps_filters_correctly(self):
        r = self._make_result(["verified", "violated", "unknown"])
        assert len(r.violated_steps()) == 1
        assert r.violated_steps()[0].step == 2

    def test_summary_counts(self):
        r = self._make_result(["verified", "violated", "unknown"])
        s = r.summary()
        assert "3 step(s)" in s
        assert "1 verified" in s
        assert "1 violated" in s

    def test_to_json_is_valid(self):
        r = self._make_result(["verified"])
        data = json.loads(r.to_json())
        assert "results" in data
        assert data["results"][0]["status"] == "verified"


# ---------------------------------------------------------------------------
# run_pipeline (integration — mocked LLM + tool execution)
# ---------------------------------------------------------------------------


class TestRunPipeline:
    def _make_intent_file(self, tmp_path, modules=None) -> Path:
        intent = {
            "modules": modules
            or [
                {
                    "path": str(tmp_path / "target.py"),
                    "understanding": {
                        "behavioral_contract": "returns None on empty input",
                        "resource_obligations": "",
                    },
                    "violations": [
                        {"severity": "high", "explanation": "missing null guard"}
                    ],
                    "invariants": [],
                }
            ]
        }
        (tmp_path / "target.py").write_text("def f(x): return x or None")
        p = tmp_path / "intent.json"
        p.write_text(json.dumps(intent))
        return p

    def test_empty_plan_returns_no_results(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        intent_path = self._make_intent_file(tmp_path)
        with _mock_llm("[]"):
            result = run_pipeline(intent_path, verbose=False)
        assert result.results == []
        assert result.plan == []

    def test_tla_step_generates_spec_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        intent_path = self._make_intent_file(tmp_path)
        plan = [
            {
                "step": 1,
                "tool": "tla",
                "module_path": str(tmp_path / "target.py"),
                "function_name": "f",
                "obligation": "process must be joined before exit",
                "spec_template": "resource_lifecycle",
                "rationale": "spawns processes",
                "depends_on": [],
            }
        ]
        with _mock_llm(json.dumps(plan)):
            result = run_pipeline(intent_path, verbose=False)
        assert len(result.results) == 1
        r = result.results[0]
        assert r.tool == "tla"
        assert r.status in ("verified", "unknown")  # "unknown" if TLC jar absent
        spec_path = Path(r.details["spec_path"])
        assert spec_path.exists()
        assert "resource_count" in spec_path.read_text()

    def test_heal_skipped_when_no_prior_violation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        intent_path = self._make_intent_file(tmp_path)
        plan = [
            {
                "step": 1,
                "tool": "z3",
                "module_path": str(tmp_path / "target.py"),
                "function_name": "f",
                "contract": "returns None on empty",
                "depends_on": [],
                "rationale": "verify contract",
            },
            {
                "step": 2,
                "tool": "heal",
                "module_path": str(tmp_path / "target.py"),
                "function_name": "f",
                "violation_summary": "missing guard",
                "depends_on": [1],
                "rationale": "fix violation",
            },
        ]

        mock_z3_result = MagicMock()
        mock_z3_result.status = "unsat"  # verified — no violation
        mock_z3_result.explanation = "contract holds"
        mock_z3_result.counterexample = None
        mock_z3_result.encoding_approach = "direct"

        with _mock_llm(json.dumps(plan)):
            with patch(
                "pact.contract_encoder.verify_contract", return_value=mock_z3_result
            ):
                result = run_pipeline(intent_path, verbose=False)

        assert len(result.results) == 2
        heal_result = next(r for r in result.results if r.tool == "heal")
        assert heal_result.status == "skipped"

    def test_max_steps_capped_at_8(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        intent_path = self._make_intent_file(tmp_path)
        plan = [
            {
                "step": i,
                "tool": "tla",
                "module_path": str(tmp_path / "target.py"),
                "function_name": "f",
                "obligation": f"obligation {i}",
                "spec_template": "liveness",
                "depends_on": [],
                "rationale": f"step {i}",
            }
            for i in range(1, 15)  # 14 steps — should be capped at 8
        ]
        with _mock_llm(json.dumps(plan)):
            result = run_pipeline(intent_path, verbose=False)
        assert len(result.results) <= 8

    def test_execute_heal_maps_zero_patches_to_unknown(self, tmp_path, monkeypatch):
        # _execute_heal with an intent JSON that has 0 violations → 0 patches → "unknown".
        # Requires an oracle marker so we reach heal_project (which short-circuits at 0 violations).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        # intent JSON with no violations — heal_project will find nothing to fix
        intent_path = tmp_path / "intent.json"
        intent_path.write_text(json.dumps({"modules": []}))
        (tmp_path / "target.py").write_text("def f(x): return x or None")
        step = {
            "step": 1,
            "tool": "heal",
            "module_path": str(tmp_path / "target.py"),
            "function_name": "f",
            "violation_summary": "missing guard",
            "depends_on": [],
            "rationale": "fix violation",
        }
        r = _execute_heal(step, intent_path, "fake", "claude-sonnet-4-6", False)
        assert r.tool == "heal"
        assert r.status == "unknown"
        assert r.details["violations_attempted"] == 0
        assert r.details["patches_accepted"] == 0

    def test_execute_heal_detects_oracle_and_applies(self, tmp_path, monkeypatch):
        """When project root has pytest markers, heal uses apply=True."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "target.py").write_text("def f(x): return x or None")
        intent_path = tmp_path / "intent.json"
        intent_path.write_text(json.dumps({"modules": []}))
        step = {
            "step": 1,
            "tool": "heal",
            "module_path": str(tmp_path / "target.py"),
            "function_name": "f",
            "violation_summary": "missing guard",
            "depends_on": [],
            "rationale": "fix violation",
        }
        r = _execute_heal(step, intent_path, "fake", "claude-sonnet-4-6", False)
        assert r.details["oracle"] != "none"
        assert r.details["applied"] is True
        assert "oracle-verified" in r.summary

    def test_execute_heal_no_oracle_skipped(self, tmp_path, monkeypatch):
        """When no project markers found, heal is skipped (not dry-run) to avoid unverified patches."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        (tmp_path / "target.py").write_text("def f(x): return x or None")
        intent_path = tmp_path / "intent.json"
        intent_path.write_text(json.dumps({"modules": []}))
        step = {
            "step": 1,
            "tool": "heal",
            "module_path": str(tmp_path / "target.py"),
            "function_name": "f",
            "violation_summary": "missing guard",
            "depends_on": [],
            "rationale": "fix violation",
        }
        r = _execute_heal(step, intent_path, "fake", "claude-sonnet-4-6", False)
        assert r.status == "skipped"
        assert r.details["oracle"] == "none"
        assert r.details["applied"] is False
        assert "oracle" in r.summary

    def test_missing_api_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("PACT_LLM_API_KEY", raising=False)
        monkeypatch.delenv("PACT_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        intent_path = self._make_intent_file(tmp_path)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            run_pipeline(intent_path, api_key=None)

    def test_inv_z3_index_built_from_intent(self, tmp_path, monkeypatch):
        """Pipeline builds invariant index from z3_encoding fields in intent JSON."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        z3_script = 'import z3\nprint(\'{"status": "unsat"}\')'
        intent = {
            "modules": [
                {
                    "path": str(tmp_path / "target.py"),
                    "understanding": {
                        "behavioral_contract": "never raises",
                        "resource_obligations": "none",
                    },
                    "violations": [{"severity": "high", "explanation": "gap"}],
                    "invariants": [
                        {
                            "statement": "never raises KeyError",
                            "z3_encoding": z3_script,
                            "contract_kind": "behavioral",
                            "tla_template": "liveness",
                        }
                    ],
                }
            ]
        }
        (tmp_path / "target.py").write_text("def f(x): return x")
        p = tmp_path / "intent.json"
        p.write_text(json.dumps(intent))

        plan_json = json.dumps(
            [
                {
                    "step": 1,
                    "tool": "z3",
                    "module_path": str(tmp_path / "target.py"),
                    "function_name": "f",
                    "contract": "never raises KeyError",
                    "rationale": "test IR",
                    "depends_on": [],
                }
            ]
        )
        with _mock_llm(plan_json):
            result = run_pipeline(p, api_key="fake")

        # "preencoded" encoding_approach means verify_contract used the IR script,
        # not the LLM round-trip.
        assert result.results[0].details.get("encoding") == "preencoded"
        assert result.results[0].status == "verified"


# ---------------------------------------------------------------------------
# Contract IR — _classify_contract_kind
# ---------------------------------------------------------------------------


class TestClassifyContractKind:
    def _classify(self, encoding_approach: str, statement: str = ""):
        from pact.intent import _classify_contract_kind

        return _classify_contract_kind(encoding_approach, statement)

    def test_ordering_from_encoding_approach(self):
        kind, tla = self._classify("models ordering constraint between phases")
        assert kind == "ordering"
        assert tla == "ordering"

    def test_guard_requirement_maps_to_ordering(self):
        kind, tla = self._classify(
            "", "TypeScript dispatch is gated on path.suffix in _TS_SUFFIXES"
        )
        assert kind == "ordering"

    def test_error_contract_from_exception_keywords(self):
        kind, _ = self._classify(
            "", "`_parse_py_cached` catches SyntaxError and returns None"
        )
        assert kind == "error_contract"

    def test_resource_lifecycle_from_statement(self):
        kind, tla = self._classify("", "open file handle must be closed when done")
        assert kind == "resource_lifecycle"
        assert tla == "resource_lifecycle"

    def test_accumulation_from_encoding(self):
        kind, tla = self._classify("models unbounded state accumulation")
        assert kind == "accumulation"
        assert tla == "accumulation"

    def test_flag_invariant(self):
        kind, tla = self._classify("", "_HAS_Z3 flag silently disables checks")
        assert kind == "flag_invariant"
        assert tla == "liveness"

    def test_subset_relation(self):
        kind, tla = self._classify("", "required_args must be subset of provided args")
        assert kind == "subset_relation"
        assert tla == "liveness"

    def test_behavioral_default(self):
        kind, tla = self._classify(
            "models function return value", "returns correct result"
        )
        assert kind == "behavioral"
        assert tla == "liveness"


# ---------------------------------------------------------------------------
# Contract IR — preencoded_z3_script in verify_contract
# ---------------------------------------------------------------------------


class TestPreencodedZ3Script:
    def test_preencoded_skips_llm(self, monkeypatch):
        """verify_contract uses preencoded script without calling the LLM."""
        from pact.contract_encoder import verify_contract

        z3_script = 'import z3\nprint(\'{"status": "unsat", "counterexample": null, "explanation": "holds"}\')'

        called = []

        def fake_call_llm(prompt, model, key):
            called.append(prompt)
            return {}

        monkeypatch.setattr("pact.contract_encoder._call_llm", fake_call_llm)
        result = verify_contract(
            contract="always returns non-None",
            function_source="def f(x): return x or 1",
            function_name="f",
            api_key="fake",
            preencoded_z3_script=z3_script,
        )
        assert called == []  # LLM not called
        assert result.status == "unsat"
        assert result.encoding_approach == "preencoded"

    def test_invalid_preencoded_falls_through_to_llm(self, monkeypatch):
        """Script without 'import z3' is treated as invalid — falls through to LLM."""
        from pact.contract_encoder import verify_contract

        called = []

        def fake_call_llm(prompt, model, key):
            called.append(True)
            raise RuntimeError("no key")

        monkeypatch.setattr("pact.contract_encoder._call_llm", fake_call_llm)
        try:
            verify_contract(
                contract="test",
                function_source="def f(): pass",
                function_name="f",
                api_key="fake",
                preencoded_z3_script="print('no z3 import here')",
            )
        except Exception:
            pass  # intentional — verifies oracle fallback
        assert called  # LLM was called (preencoded rejected, fell through)


# ---------------------------------------------------------------------------
# Contract templates — unit tests for render_z3_template
# ---------------------------------------------------------------------------

_HAS_Z3 = importlib.util.find_spec("z3") is not None
pytestmark_z3 = pytest.mark.skipif(not _HAS_Z3, reason="z3-solver not installed")


@pytestmark_z3
class TestContractTemplates:
    """Tests that each template produces the expected SAT/UNSAT result when run directly."""

    def _run(self, script: str) -> dict:
        """Run a Z3 script string and return the parsed JSON result."""
        import json
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(script)
            path = f.name
        try:
            result = subprocess.run(
                [sys.executable, path],
                capture_output=True,
                text=True,
                timeout=15,
            )
            for line in reversed(result.stdout.strip().splitlines()):
                if line.strip().startswith("{"):
                    return json.loads(line.strip())
            raise AssertionError(
                f"No JSON output from Z3 script.\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def test_flag_invariant_sat_when_silent_true(self):
        """flag_invariant with silent_when_false=True returns SAT (bug: flag suppresses check)."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "flag_invariant",
            {
                "flag_name": "enabled",
                "check_name": "constraint_check",
                "silent_when_false": True,
            },
        )
        result = self._run(script)
        assert result["status"] == "sat", f"Expected SAT (bug), got: {result}"
        assert result["counterexample"] is not None

    def test_flag_invariant_unsat_when_silent_false(self):
        """flag_invariant with silent_when_false=False returns UNSAT (fixed: guard always runs)."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "flag_invariant",
            {
                "flag_name": "enabled",
                "check_name": "constraint_check",
                "silent_when_false": False,
            },
        )
        result = self._run(script)
        assert result["status"] == "unsat", f"Expected UNSAT (fixed), got: {result}"

    def test_subset_relation_sat_when_violated(self):
        """subset_relation returns SAT when set_a is not a subset of set_b."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "subset_relation",
            {"set_a": "required_args", "set_b": "provided_args"},
        )
        result = self._run(script)
        assert (
            result["status"] == "sat"
        ), f"Expected SAT (subset violated), got: {result}"
        assert result["counterexample"] is not None

    def test_ordering_sat_when_no_guard(self):
        """ordering with guard_exists=False returns SAT (second_op can be called first)."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "ordering",
            {"first_op": "setup", "second_op": "run", "guard_exists": False},
        )
        result = self._run(script)
        assert result["status"] == "sat", f"Expected SAT (no guard), got: {result}"
        assert result["counterexample"] is not None

    def test_ordering_unsat_when_guard_exists(self):
        """ordering with guard_exists=True returns UNSAT (guard enforces order)."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "ordering",
            {"first_op": "setup", "second_op": "run", "guard_exists": True},
        )
        result = self._run(script)
        assert result["status"] == "unsat", f"Expected UNSAT (guarded), got: {result}"

    def test_nullable_contract_sat_when_skips(self):
        """nullable_contract with skips_on_none=True returns SAT (None skips check)."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "nullable_contract",
            {
                "field_name": "response",
                "check_name": "validation",
                "skips_on_none": True,
            },
        )
        result = self._run(script)
        assert (
            result["status"] == "sat"
        ), f"Expected SAT (None skips check), got: {result}"

    def test_resource_lifecycle_sat_when_no_release(self):
        """resource_lifecycle with release_guaranteed=False returns SAT (acquire without release)."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "resource_lifecycle",
            {"resource": "connection", "release_guaranteed": False},
        )
        result = self._run(script)
        assert result["status"] == "sat", f"Expected SAT (no release), got: {result}"
        assert "connection" in str(result.get("counterexample", ""))

    def test_resource_lifecycle_unsat_when_release_guaranteed(self):
        """resource_lifecycle with release_guaranteed=True returns UNSAT."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "resource_lifecycle",
            {"resource": "connection", "release_guaranteed": True},
        )
        result = self._run(script)
        assert (
            result["status"] == "unsat"
        ), f"Expected UNSAT (release guaranteed), got: {result}"

    def test_error_contract_sat_when_silent(self):
        """error_contract with silent_on_exception=True returns SAT."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "error_contract",
            {
                "exception_name": "SyntaxError",
                "function_name": "_parse_py_cached",
                "silent_on_exception": True,
            },
        )
        result = self._run(script)
        assert (
            result["status"] == "sat"
        ), f"Expected SAT (silent swallow), got: {result}"
        assert result["counterexample"]["caller_notified"] == "False"

    def test_error_contract_unsat_when_notified(self):
        """error_contract with silent_on_exception=False returns UNSAT."""
        from pact.contract_templates import render_z3_template

        script = render_z3_template(
            "error_contract",
            {
                "exception_name": "OSError",
                "function_name": "_parse_ts_cached",
                "silent_on_exception": False,
            },
        )
        result = self._run(script)
        assert (
            result["status"] == "unsat"
        ), f"Expected UNSAT (caller notified), got: {result}"

    def test_unsupported_kind_raises_key_error(self):
        """render_z3_template raises KeyError for unknown contract_kind."""
        from pact.contract_templates import render_z3_template

        with pytest.raises(KeyError):
            render_z3_template("nonexistent_kind", {})

    def test_all_templates_contain_z3_import(self):
        """Every template produces a script that contains 'import z3'."""
        from pact.contract_templates import SUPPORTED_KINDS, render_z3_template

        for kind in SUPPORTED_KINDS:
            params: dict = {}
            script = render_z3_template(kind, params)
            assert "import z3" in script, f"Template '{kind}' missing 'import z3'"
            assert "import json" in script, f"Template '{kind}' missing 'import json'"


# ---------------------------------------------------------------------------
# verify_contract_typed — uses template (mock LLM params call, Z3 runs directly)
# ---------------------------------------------------------------------------


@pytestmark_z3
class TestVerifyContractTyped:
    def test_uses_template_with_mocked_params(self, monkeypatch):
        """verify_contract_typed uses the template when LLM returns valid params."""
        from pact.contract_encoder import verify_contract_typed

        # Mock _call_llm to return known params for flag_invariant
        def fake_call_llm(prompt, model, key):
            return {
                "flag_name": "enabled",
                "check_name": "constraint_check",
                "silent_when_false": True,
            }

        monkeypatch.setattr("pact.contract_encoder._call_llm", fake_call_llm)

        result = verify_contract_typed(
            contract="enabled flag silently disables constraint checking when False",
            function_source="def check(enabled, data): enabled and validate(data)",
            function_name="check",
            contract_kind="flag_invariant",
            api_key="fake",
        )
        # SAT because silent_when_false=True in mock params
        assert result.status == "sat"
        assert "typed_template:flag_invariant" in result.encoding_approach
        assert result.z3_script and "import z3" in result.z3_script

    def test_unknown_kind_returns_unknown(self, monkeypatch):
        """verify_contract_typed returns 'unknown' for unsupported kind."""
        from pact.contract_encoder import verify_contract_typed

        result = verify_contract_typed(
            contract="some contract",
            function_source="def f(): pass",
            function_name="f",
            contract_kind="completely_unknown_kind",
            api_key="fake",
        )
        assert result.status == "unknown"

    def test_llm_failure_returns_unknown_not_encoding_failed(self, monkeypatch):
        """verify_contract_typed returns 'unknown' (not 'encoding_failed') on LLM error."""
        from pact.contract_encoder import verify_contract_typed

        def fake_call_llm(prompt, model, key):
            raise RuntimeError("LLM unavailable")

        monkeypatch.setattr("pact.contract_encoder._call_llm", fake_call_llm)

        result = verify_contract_typed(
            contract="flag silently suppresses checks",
            function_source="def f(flag): flag and check()",
            function_name="f",
            contract_kind="flag_invariant",
            api_key="fake",
        )
        assert result.status == "unknown"
        assert "param extraction failed" in result.explanation

    def test_verify_contract_uses_typed_path_when_kind_known(self, monkeypatch):
        """verify_contract routes to typed path when contract_kind is in SUPPORTED_KINDS."""
        from pact.contract_encoder import verify_contract

        typed_calls = []

        def fake_verify_contract_typed(
            contract, function_source, function_name, contract_kind, api_key, model
        ):
            typed_calls.append(contract_kind)
            from pact.contract_encoder import ContractVerificationResult

            return ContractVerificationResult(
                function_name=function_name,
                contract=contract,
                status="sat",
                explanation="template found violation",
                encoding_approach=f"typed_template:{contract_kind}",
            )

        monkeypatch.setattr(
            "pact.contract_encoder.verify_contract_typed", fake_verify_contract_typed
        )

        verify_contract(
            contract="flag suppresses checks",
            function_source="def f(flag): pass",
            function_name="f",
            api_key="fake",
            contract_kind="flag_invariant",
        )
        assert typed_calls == ["flag_invariant"]


# ---------------------------------------------------------------------------
# Hypothesis ← Z3 counterexample seeding
# ---------------------------------------------------------------------------


class TestHypothesisZ3Seeding:
    """Verify that Z3 counterexamples are threaded into Hypothesis stress_contract calls."""

    def _make_intent_file(self, tmp_path) -> Path:
        intent = {
            "modules": [
                {
                    "path": str(tmp_path / "target.py"),
                    "understanding": {
                        "behavioral_contract": "never returns empty list",
                        "resource_obligations": "",
                    },
                    "violations": [
                        {"severity": "high", "explanation": "may return []"}
                    ],
                    "invariants": [],
                }
            ]
        }
        (tmp_path / "target.py").write_text("def f(x): return x or []")
        p = tmp_path / "intent.json"
        p.write_text(json.dumps(intent))
        return p

    def test_z3_counterexample_passed_to_stress_contract(self, tmp_path, monkeypatch):
        """When a Z3 step is violated, its counterexample reaches stress_contract."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")

        import pact.hypothesis_generator as _hg

        # Plan: z3 step 1 (violated), hypothesis step 2 depending on it
        plan_json = json.dumps(
            [
                {
                    "step": 1,
                    "tool": "z3",
                    "module_path": str(tmp_path / "target.py"),
                    "function_name": "f",
                    "contract": "never returns empty list",
                    "depends_on": [],
                    "rationale": "check contract",
                },
                {
                    "step": 2,
                    "tool": "hypothesis",
                    "module_path": str(tmp_path / "target.py"),
                    "function_name": "f",
                    "contract": "never returns empty list",
                    "depends_on": [1],
                    "rationale": "adversarial stress",
                },
            ]
        )

        received_ce: list[str] = []

        def fake_stress_contract(**kwargs):
            received_ce.append(kwargs.get("z3_counterexample") or "")
            return _hg.HypothesisStressResult(
                function_name=kwargs["function_name"],
                contract=kwargs["contract"],
                status="passed",
                counterexample=None,
                explanation="held for 10 examples",
            )

        # Z3 step returns violated with a counterexample
        fake_z3_result = MagicMock()
        fake_z3_result.status = "sat"
        fake_z3_result.explanation = "x=None triggers empty return"
        fake_z3_result.counterexample = {"x": None}
        fake_z3_result.encoding_approach = "typed_template"

        intent_path = self._make_intent_file(tmp_path)

        # Use monkeypatch.setattr on the module object — more reliable than
        # patch() string form under pytest --import-mode=importlib.
        monkeypatch.setattr(_hg, "stress_contract", fake_stress_contract)

        with _mock_llm(plan_json):
            with patch(
                "pact.contract_encoder.verify_contract", return_value=fake_z3_result
            ):
                result = run_pipeline(intent_path, verbose=False)

        assert len(result.results) >= 2
        hyp_results = [r for r in result.results if r.tool == "hypothesis"]
        assert hyp_results, "no hypothesis step ran"
        assert received_ce, "stress_contract was never called"
        assert received_ce[0], "z3_counterexample was None/empty — not threaded through"

    def test_hypothesis_auto_injected_for_uncovered_z3_violation(
        self, tmp_path, monkeypatch
    ):
        """When LLM plan has only a Z3 step that violates, pipeline auto-injects Hypothesis."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")

        import pact.hypothesis_generator as _hg

        # Plan: only z3 step — no hypothesis step planned
        plan_json = json.dumps(
            [
                {
                    "step": 1,
                    "tool": "z3",
                    "module_path": str(tmp_path / "target.py"),
                    "function_name": "f",
                    "contract": "never returns empty list",
                    "depends_on": [],
                    "rationale": "check contract",
                }
            ]
        )

        stress_calls: list[dict] = []

        def fake_stress_contract(**kwargs):
            stress_calls.append(kwargs)
            return _hg.HypothesisStressResult(
                function_name=kwargs["function_name"],
                contract=kwargs["contract"],
                status="falsified",
                counterexample="None",
                explanation="found counterexample",
            )

        fake_z3_result = MagicMock()
        fake_z3_result.status = "sat"
        fake_z3_result.explanation = "contract violated"
        fake_z3_result.counterexample = {"x": None}
        fake_z3_result.encoding_approach = "typed_template"

        intent_path = self._make_intent_file(tmp_path)

        monkeypatch.setattr(_hg, "stress_contract", fake_stress_contract)

        with _mock_llm(plan_json):
            with patch(
                "pact.contract_encoder.verify_contract", return_value=fake_z3_result
            ):
                result = run_pipeline(intent_path, verbose=False)

        hyp_results = [r for r in result.results if r.tool == "hypothesis"]
        assert hyp_results, "pipeline did not auto-inject Hypothesis after Z3 violation"
        assert len(stress_calls) == 1, "stress_contract should have been called once"
        assert stress_calls[0].get(
            "z3_counterexample"
        ), "Z3 counterexample not passed to auto-injected step"

    def test_hypothesis_not_injected_when_z3_verified(self, tmp_path, monkeypatch):
        """When Z3 verifies a contract, no Hypothesis step is auto-injected."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")

        plan_json = json.dumps(
            [
                {
                    "step": 1,
                    "tool": "z3",
                    "module_path": str(tmp_path / "target.py"),
                    "function_name": "f",
                    "contract": "always safe",
                    "depends_on": [],
                    "rationale": "check contract",
                }
            ]
        )

        fake_z3_result = MagicMock()
        fake_z3_result.status = "unsat"
        fake_z3_result.explanation = "contract verified"
        fake_z3_result.counterexample = None
        fake_z3_result.encoding_approach = "typed_template"

        intent_path = self._make_intent_file(tmp_path)

        with _mock_llm(plan_json):
            with patch(
                "pact.contract_encoder.verify_contract", return_value=fake_z3_result
            ):
                result = run_pipeline(intent_path, verbose=False)

        hyp_results = [r for r in result.results if r.tool == "hypothesis"]
        assert not hyp_results, "Hypothesis should not be injected when Z3 verifies"
        assert result.violated_steps() == [], "No violations expected when Z3 verifies"
