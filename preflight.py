"""
pact preflight — run the target repo's own linters on a patched file BEFORE
filing a PR, so lint failures are caught locally instead of in CI.

Reads .pre-commit-config.yaml (if present) to discover the exact linter
version the repo uses, then runs it via `uvx <tool>@<rev>` so the check
matches CI exactly.  Falls back to the local ruff/black if pre-commit is
absent.

Usage (CLI)
-----------
    pact preflight <repo_dir> <file_relative_path>
    pact preflight /private/tmp/lm-eval-harness lm_eval/models/anthropic_llms.py

Programmatic
------------
    from pact.preflight import run_preflight
    result = run_preflight(repo_dir, file_path)
    if not result.clean:
        print(result.report())
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PreflightResult:
    file: str
    checks: list[dict] = field(default_factory=list)  # {tool, version, passed, output}

    @property
    def clean(self) -> bool:
        return all(c["passed"] for c in self.checks)

    def report(self) -> str:
        lines = [f"preflight: {self.file}"]
        for c in self.checks:
            mark = "✓" if c["passed"] else "✗"
            lines.append(f"  {mark}  {c['tool']}@{c['version']}")
            if not c["passed"] and c["output"].strip():
                for ln in c["output"].strip().splitlines():
                    lines.append(f"       {ln}")
        return "\n".join(lines)


def _parse_precommit(repo_dir: Path) -> dict[str, str]:
    """
    Parse .pre-commit-config.yaml and return {hook_id: rev} for known linters.

    Recognised hooks: ruff-check, ruff-format, ruff, black, isort, flake8.
    """
    cfg_path = repo_dir / ".pre-commit-config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        return {}
    try:
        cfg = yaml.safe_load(cfg_path.read_text())
    except Exception:
        return {}

    result: dict[str, str] = {}
    _known = {"ruff-check", "ruff-format", "ruff", "black", "isort", "flake8"}
    for repo_entry in cfg.get("repos", []):
        rev = repo_entry.get("rev", "")
        for hook in repo_entry.get("hooks", []):
            hid = hook.get("id", "")
            if hid in _known:
                result[hid] = rev.lstrip("v")  # strip leading 'v' from tag
    return result


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command, return (returncode, combined output)."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def _ruff_cmd(rev: str, file: str, fix: bool = False) -> list[str]:
    """Build a ruff check command using the exact pinned version via uvx."""
    base = ["uvx", f"ruff@{rev}", "check"]
    if fix:
        base.append("--fix")
    base.append(file)
    return base


def _ruff_format_cmd(rev: str, file: str) -> list[str]:
    return ["uvx", f"ruff@{rev}", "format", file]


def _black_cmd(rev: str, file: str) -> list[str]:
    return ["uvx", f"black@{rev}", file]


def _isort_cmd(rev: str, file: str) -> list[str]:
    return ["uvx", f"isort@{rev}", file]


def run_preflight(repo_dir: Path | str, file_rel: str | Path) -> PreflightResult:
    """
    Run the target repo's linters on *file_rel* (relative to *repo_dir*).

    Returns a PreflightResult.  If .pre-commit-config.yaml is absent or
    unreadable, falls back to running the local ruff on the file.
    """
    repo_dir = Path(repo_dir).resolve()
    file_rel = str(file_rel)
    file_abs = str(repo_dir / file_rel)
    result = PreflightResult(file=file_rel)

    hooks = _parse_precommit(repo_dir)

    # ── ruff ─────────────────────────────────────────────────────────────────
    ruff_rev = hooks.get("ruff-check") or hooks.get("ruff")
    ruff_format_rev = hooks.get("ruff-format") or ruff_rev

    if ruff_rev:
        # Step 1: auto-fix
        _run(_ruff_cmd(ruff_rev, file_abs, fix=True), cwd=repo_dir)
        # Step 2: check for unfixable remaining errors
        rc, out = _run(_ruff_cmd(ruff_rev, file_abs, fix=False), cwd=repo_dir)
        result.checks.append(
            {
                "tool": "ruff-check",
                "version": ruff_rev,
                "passed": rc == 0,
                "output": out,
            }
        )
    else:
        # Fallback: local ruff
        _run(["ruff", "check", "--fix", file_abs], cwd=repo_dir)
        rc, out = _run(["ruff", "check", file_abs], cwd=repo_dir)
        result.checks.append(
            {
                "tool": "ruff-check",
                "version": "local",
                "passed": rc == 0,
                "output": out,
            }
        )

    if ruff_format_rev:
        rc, out = _run(_ruff_format_cmd(ruff_format_rev, file_abs), cwd=repo_dir)
        result.checks.append(
            {
                "tool": "ruff-format",
                "version": ruff_format_rev,
                "passed": rc == 0,
                "output": out,
            }
        )
    else:
        rc, out = _run(["ruff", "format", file_abs], cwd=repo_dir)
        result.checks.append(
            {
                "tool": "ruff-format",
                "version": "local",
                "passed": rc == 0,
                "output": out,
            }
        )

    # ── black (if configured separately) ─────────────────────────────────────
    black_rev = hooks.get("black")
    if black_rev:
        rc, out = _run(_black_cmd(black_rev, file_abs), cwd=repo_dir)
        result.checks.append(
            {
                "tool": "black",
                "version": black_rev,
                "passed": rc == 0,
                "output": out,
            }
        )

    # ── isort ─────────────────────────────────────────────────────────────────
    isort_rev = hooks.get("isort")
    if isort_rev:
        rc, out = _run(_isort_cmd(isort_rev, file_abs), cwd=repo_dir)
        result.checks.append(
            {
                "tool": "isort",
                "version": isort_rev,
                "passed": rc == 0,
                "output": out,
            }
        )

    return result


def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="pact preflight",
        description="Run the target repo's linters on a patched file before filing a PR.",
    )
    p.add_argument("repo_dir", metavar="REPO_DIR", help="Root of the cloned repository")
    p.add_argument(
        "file",
        metavar="FILE",
        help="File path relative to REPO_DIR (or absolute)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit result as JSON instead of human-readable text",
    )
    args = p.parse_args(argv)

    import json

    repo_dir = Path(args.repo_dir).resolve()
    file_rel = args.file
    if Path(file_rel).is_absolute():
        file_rel = str(Path(file_rel).relative_to(repo_dir))

    result = run_preflight(repo_dir, file_rel)

    if args.json:
        import dataclasses

        print(json.dumps(dataclasses.asdict(result), indent=2))
    else:
        print(result.report())

    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
