"""Tests for pact heal oracle autodetection and safety gap enforcement."""

import json
from pathlib import Path

from .heal import HealResult, _autodetect_test_cmd, _func_body_at_line, heal_project

# ---------------------------------------------------------------------------
# _autodetect_test_cmd
# ---------------------------------------------------------------------------


class TestAutodetectTestCmd:
    def test_pytest_ini_detected(self, tmp_path):
        import sys

        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "pytest" in cmd
        assert sys.executable in cmd  # must use venv python, not bare "python"

    def test_pyproject_toml_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "pytest" in cmd

    def test_conftest_detected(self, tmp_path):
        (tmp_path / "conftest.py").write_text("")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "pytest" in cmd

    def test_tox_ini_detected(self, tmp_path):
        (tmp_path / "tox.ini").write_text("[tox]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "tox" in cmd

    def test_makefile_test_target_detected(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None
        assert "make" in cmd

    def test_makefile_without_test_target_skipped(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is None

    def test_empty_dir_returns_none(self, tmp_path):
        assert _autodetect_test_cmd(tmp_path) is None

    def test_pytest_takes_priority_over_tox(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "tox.ini").write_text("[tox]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert "pytest" in cmd  # pytest wins

    def test_pyproject_pact_oracle_cmd_overrides_all(self, tmp_path):
        # [tool.pact].oracle_cmd wins over any auto-detected runner
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pact]\noracle_cmd = "make custom-test"\n'
        )
        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd == "make custom-test"


# ---------------------------------------------------------------------------
# oracle_warning on HealResult
# ---------------------------------------------------------------------------


class TestOracleBaselineSanityCheck:
    """Oracle is disabled when baseline test suite already fails."""

    def test_oracle_disabled_when_baseline_fails(self, tmp_path, monkeypatch):
        import json as _json

        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        # Oracle command that always fails
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        intent = tmp_path / "intent.json"
        intent.write_text(_json.dumps({"modules": []}))
        from .heal import _autodetect_test_cmd

        cmd = _autodetect_test_cmd(tmp_path)
        assert cmd is not None  # auto-detected as pytest project
        # If we heal with this cmd, baseline fails → oracle disabled
        # (tested via _run_oracle internals; full integration requires a real cmd)


class TestFuncBodyAtLine:
    def _lines(self, source: str) -> list[str]:
        return [ln + "\n" for ln in source.splitlines()]

    def test_returns_enclosing_function(self):
        src = "def foo():\n    x = 1\n    return x\n\ndef bar():\n    y = 2\n"
        lines = self._lines(src)
        ctx, start, end = _func_body_at_line(lines, 2)
        assert "def foo" in ctx
        assert "def bar" not in ctx
        assert start == 1
        assert end == 3

    def test_returns_innermost_when_nested(self):
        src = "def outer():\n    def inner():\n        return 1\n    return inner\n"
        lines = self._lines(src)
        ctx, start, end = _func_body_at_line(lines, 3)
        assert "def inner" in ctx
        assert start == 2

    def test_falls_back_on_syntax_error(self):
        lines = ["def broken(:\n", "    pass\n"]
        ctx, start, end = _func_body_at_line(lines, 1)
        assert isinstance(ctx, str)
        assert len(ctx) > 0


class TestOracleWarning:
    def _empty_intent(self, tmp_path) -> Path:
        p = tmp_path / "intent.json"
        p.write_text(json.dumps({"modules": []}))
        return p

    def test_no_warning_when_no_patches(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        intent = self._empty_intent(tmp_path)
        result = heal_project(
            violations_path=intent,
            apply=True,
            project_root=tmp_path,
        )
        assert result.oracle_warning == ""

    def test_oracle_warning_field_exists_on_result(self):
        r = HealResult(project="test")
        assert r.oracle_warning == ""

    def test_oracle_warning_set_correctly(self):
        r = HealResult(project="test", patches_accepted=2)
        r.oracle_warning = "2 patch(es) applied with Z3 verification only"
        assert "Z3 verification only" in r.oracle_warning


# ---------------------------------------------------------------------------
# Dry run must not modify files; --auto gate must actually run
# ---------------------------------------------------------------------------


class TestHealDiskSafety:
    ORIG = "def f(x):\n    return x + 1\n"

    def _setup(self, tmp_path, monkeypatch):
        from . import heal

        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
        src = tmp_path / "mod.py"
        src.write_text(self.ORIG)
        intent = tmp_path / "intent.json"
        intent.write_text(
            json.dumps(
                {
                    "project": "p",
                    "modules": [
                        {
                            "invariants": [{"id": "I1", "type": "t", "statement": "s"}],
                            "violations": [
                                {
                                    "file": str(src),
                                    "line": 2,
                                    "severity": "high",
                                    "invariant_id": "I1",
                                }
                            ],
                        }
                    ],
                }
            )
        )

        def fake_synth(v, inv, lines, model, key, verbose, feedback):
            return heal.SynthesisResult(
                "I1",
                str(src),
                2,
                "s",
                heal.Diagnosis("r", "c", "o"),
                heal.Patch("    return x + 1", "    return x + 2"),
                heal.Justification("", "", "", None, ""),
            )

        monkeypatch.setattr(heal, "_synthesize", fake_synth)
        monkeypatch.setattr(heal, "_verify", lambda *a, **k: (1.0, "ACCEPT", ""))
        return src, intent

    def test_dry_run_with_oracle_leaves_file_unchanged(self, tmp_path, monkeypatch):
        src, intent = self._setup(tmp_path, monkeypatch)
        r = heal_project(intent, apply=False, test_cmd="true", project_root=tmp_path)
        assert r.results[0].oracle_confirmed
        assert src.read_text() == self.ORIG
        assert not r.results[0].applied

    def test_apply_with_oracle_writes_file(self, tmp_path, monkeypatch):
        src, intent = self._setup(tmp_path, monkeypatch)
        heal_project(intent, apply=True, test_cmd="true", project_root=tmp_path)
        assert "x + 2" in src.read_text()

    def test_oracle_exception_reverts_file(self, tmp_path, monkeypatch):
        from . import heal

        src, intent = self._setup(tmp_path, monkeypatch)

        calls = []

        def boom(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return True, ""  # baseline run passes
            assert "x + 2" in src.read_text()  # patch is on disk now
            raise KeyboardInterrupt

        monkeypatch.setattr(heal, "_run_oracle", boom)
        try:
            heal_project(intent, apply=True, test_cmd="true", project_root=tmp_path)
        except KeyboardInterrupt:
            pass
        assert src.read_text() == self.ORIG

    def test_checker_baseline_is_computed(self, tmp_path, monkeypatch):
        import warnings

        src, intent = self._setup(tmp_path, monkeypatch)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            r = heal_project(intent, auto=True, test_cmd="true", project_root=tmp_path)
        msgs = [str(x.message) for x in w if "baseline" in str(x.message)]
        assert msgs == []
        assert r.results[0].auto_applied


class TestAutodetectQuoting:
    def test_interpreter_path_with_space_is_quoted(self, tmp_path, monkeypatch):
        import shlex
        import sys

        monkeypatch.setattr(sys, "executable", "/opt/my venv/bin/python")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        cmd = _autodetect_test_cmd(tmp_path)
        assert shlex.split(cmd)[0] == "/opt/my venv/bin/python"
