"""Tests for pact pipeline orchestrator (pipeline.py)."""

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
        # This exercises the real heal_project call path end-to-end without LLM calls
        # (heal_project short-circuits when violations_attempted == 0).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
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

    def test_missing_api_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
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
            pass
        assert called  # LLM was called (preencoded rejected, fell through)
