"""
Every LLM subcommand must honour PACT_LLM_MODEL when --model is not passed.

argparse fills a non-None default even when the flag is absent, so a
`default="claude-..."` silently overrides the env var (CLAUDE.md,
HARD-LEARNED PATTERNS). Each test stubs the worker the CLI dispatches to and
records the model it receives.
"""

from __future__ import annotations

import importlib

import pytest


class _Called(Exception):
    pass


def _stub(seen):
    def fn(*args, **kwargs):
        seen["model"] = kwargs.get("model")
        raise _Called

    return fn


@pytest.mark.parametrize(
    "module, worker, argv",
    [
        ("context", "extract_context", ["x.py"]),
        ("find", "find_violations", ["x.py"]),
        ("heal", "heal_project", [".", "--violations", "v.json"]),
        ("pipeline", "run_pipeline", ["intent.json"]),
        ("spec_learner", "improve", ["improve"]),
    ],
)
def test_subcommand_honours_pact_llm_model(monkeypatch, module, worker, argv):
    mod = importlib.import_module(f"{__package__}.{module}")
    seen: dict = {}
    monkeypatch.setattr(mod, worker, _stub(seen))
    monkeypatch.setenv("PACT_LLM_MODEL", "env-model")
    with pytest.raises(_Called):
        mod.main(argv)
    assert seen["model"] == "env-model"


@pytest.mark.parametrize(
    "module, worker, argv",
    [
        ("context", "extract_context", ["x.py"]),
        ("heal", "heal_project", [".", "--violations", "v.json"]),
    ],
)
def test_explicit_model_flag_still_wins(monkeypatch, module, worker, argv):
    mod = importlib.import_module(f"{__package__}.{module}")
    seen: dict = {}
    monkeypatch.setattr(mod, worker, _stub(seen))
    monkeypatch.setenv("PACT_LLM_MODEL", "env-model")
    with pytest.raises(_Called):
        mod.main(argv + ["--model", "flag-model"])
    assert seen["model"] == "flag-model"


def test_spec_complete_honours_pact_llm_model(monkeypatch, tmp_path):
    from . import cli, llm

    seen: dict = {}

    class _Client:
        class messages:  # noqa: N801 — mirrors anthropic client shape
            @staticmethod
            def create(**kw):
                seen["model"] = kw["model"]
                raise _Called

    monkeypatch.setattr(llm, "make_client", lambda key=None: _Client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("PACT_LLM_MODEL", "env-model")
    src = tmp_path / "m.py"
    src.write_text("x = 1\n")
    cli.main(["spec", "complete", str(src)])
    assert seen["model"] == "env-model"


def test_intent_trigger_honours_pact_llm_model(monkeypatch, tmp_path):
    from . import cli, intent

    seen: dict = {}

    def fake_extract(path, model=None, api_key=None, **kw):
        seen["model"] = model
        raise _Called

    monkeypatch.setattr(intent, "extract_file_intent", fake_extract)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("PACT_LLM_MODEL", "env-model")
    (tmp_path / "a.py").write_text(
        "def a():\n    b()\n\ndef b():\n    c()\n\ndef c():\n    pass\n"
    )
    cli.main([str(tmp_path), "--reduce", "--intent-trigger"])
    assert seen.get("model") == "env-model", seen
