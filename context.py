"""
pact context -- extract violation signals from git history, changelog, and inline notes.

Output feeds directly into pact find as a prior, replacing uniform sampling with
signals from confirmed real-world failures.

Usage:
    pact context <file> [--repo <git-root>] [--out context.json] [--verbose]
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

_PROMPT_DIR = Path(__file__).parent / "prompts"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_SYSTEM = (
    "You are a violation signal extractor. "
    "Return JSON only — no markdown fences, no text outside the JSON."
)

_FIX_PATTERN = re.compile(
    r"\b(fix|fixes|fixed|bug|error|crash|fail|broken|wrong|incorrect|"
    r"edge.?case|off.?by.?one|overflow|underflow|truncat|corrupt|invalid|"
    r"handle|guard|check|assert|ensure|prevent|avoid|workaround)\b",
    re.IGNORECASE,
)


def _run(cmd: list[str], cwd: Path, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return r.stdout
    except Exception:
        return ""


def _git_log(file_path: Path, repo_root: Path, max_commits: int = 40) -> str:
    """Commits touching this file that mention fixes or edge cases."""
    log = _run(
        ["git", "log", "--follow", "--oneline", "-n", "200", "--", str(file_path)],
        cwd=repo_root,
    )
    fix_shas = []
    for line in log.splitlines():
        if _FIX_PATTERN.search(line):
            sha = line.split()[0]
            fix_shas.append(sha)
        if len(fix_shas) >= max_commits:
            break

    if not fix_shas:
        return log[:3000] if log else "(no git history)"

    # Get diffs for fix commits (truncated)
    out = []
    for sha in fix_shas[:15]:
        diff = _run(
            ["git", "show", "--stat", "-p", "--follow", sha, "--", str(file_path)],
            cwd=repo_root,
        )
        out.append(diff[:1500])

    return "\n---\n".join(out)[:8000]


def _changelog(file_path: Path, repo_root: Path) -> str:
    """Changelog lines that mention this file's stem or its key function names."""
    stem = file_path.stem
    candidates = [
        "CHANGES.rst",
        "CHANGELOG.rst",
        "CHANGELOG.md",
        "CHANGES.md",
        "HISTORY.rst",
    ]
    for name in candidates:
        p = repo_root / name
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            relevant = [ln for ln in lines if stem in ln or _FIX_PATTERN.search(ln)]
            return "\n".join(relevant[:80])
    return "(no changelog found)"


def _inline_notes(file_path: Path) -> str:
    """TODO / FIXME / HACK / XXX comments from the source file."""
    tags = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG|NOTE)\b", re.IGNORECASE)
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = []
        for i, line in enumerate(lines, 1):
            if tags.search(line):
                hits.append(f"line {i:4d}: {line.strip()}")
        return "\n".join(hits) if hits else "(none)"
    except Exception:
        return "(unreadable)"


def _parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = re.sub(r"```\s*$", "", text).strip()
    for candidate in [text, text[text.find("{") :] if "{" in text else ""]:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    raise RuntimeError(f"no valid JSON: {text[:300]}")


def _call(prompt: str, model: str, key: str) -> dict:
    from .llm import make_client

    client = make_client(key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    return _parse(text)


def _improve_context_prompt(
    result: dict,
    parse_error: Optional[str],
    git_log_len: int,
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """Score context prompt and rewrite if output is empty despite rich git history."""
    n_violations = len(result.get("confirmed_violations", []))
    n_fragile = len(result.get("fragile_areas", []))

    # Only improve if there's a failure signal
    has_rich_history = git_log_len > 500
    if not parse_error and (n_violations > 0 or not has_rich_history):
        return

    failure_modes: list[str] = []
    if parse_error:
        failure_modes.append(f"parse_failure: {parse_error[:200]}")
    if has_rich_history and n_violations == 0:
        failure_modes.append(
            f"empty_output: 0 confirmed_violations despite {git_log_len} chars of git history"
        )
    if n_fragile == 0 and has_rich_history:
        failure_modes.append(
            "empty_fragile_areas: no fragile areas extracted from rich history"
        )

    good_samples = result.get("confirmed_violations", [])[:3]
    bad_samples: list[dict] = []
    if parse_error:
        bad_samples.append({"error": parse_error[:300]})
    if n_violations == 0:
        bad_samples.append({"result": result})

    try:
        template = (_PROMPT_DIR / "context_improve.md").read_text(encoding="utf-8")
        prompt = (
            template.replace(
                "{{prompt_text}}",
                (_PROMPT_DIR / "context.md").read_text(encoding="utf-8"),
            )
            .replace("{{good_samples}}", json.dumps(good_samples, indent=2))
            .replace("{{bad_samples}}", json.dumps(bad_samples, indent=2))
            .replace("{{failure_modes}}", "\n".join(failure_modes) or "none")
        )
        raw = _call(prompt, model, key)
        improved = raw.get("improved_prompt", "")
        overall = raw.get("overall_score", 0.0)
        if improved and overall < 0.8:
            (_PROMPT_DIR / "context.md").write_text(improved, encoding="utf-8")
            if verbose:
                print(
                    f"\n[context] ✓ context prompt rewritten (score was {overall:.2f})"
                )
    except Exception as exc:
        import warnings

        warnings.warn(
            f"context: prompt improvement failed ({type(exc).__name__}: {exc}); "
            "context.md was not updated",
            RuntimeWarning,
            stacklevel=2,
        )
        if verbose:
            print(f"\n[context] prompt improvement failed: {exc}")


def extract_context(
    file_path: Path,
    repo_root: Optional[Path] = None,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    output: Optional[Path] = None,
    verbose: bool = False,
    improve: bool = False,
) -> dict:
    from .llm import resolve_key

    key = resolve_key(api_key)

    if repo_root is None:
        repo_root = file_path.parent
        while repo_root != repo_root.parent:
            if (repo_root / ".git").exists():
                break
            repo_root = repo_root.parent

    if verbose:
        print(f"[context] {file_path.name} (repo: {repo_root})")

    git_log = _git_log(file_path, repo_root)
    changelog = _changelog(file_path, repo_root)
    notes = _inline_notes(file_path)

    if verbose:
        print(
            f"  git: {len(git_log)} chars, changelog: {len(changelog)} chars, notes: {notes.count(chr(10))+1} lines"
        )

    template = (_PROMPT_DIR / "context.md").read_text(encoding="utf-8")
    prompt = (
        template.replace("{{file_path}}", str(file_path))
        .replace("{{git_log}}", git_log)
        .replace("{{changelog}}", changelog)
        .replace("{{inline_notes}}", notes)
    )

    parse_error: Optional[str] = None
    try:
        result = _call(prompt, model, key)
    except Exception as exc:
        parse_error = str(exc)
        if verbose:
            print(f"  failed: {exc}")
        result = {
            "file": str(file_path),
            "confirmed_violations": [],
            "fragile_areas": [],
        }

    n_v = len(result.get("confirmed_violations", []))
    n_f = len(result.get("fragile_areas", []))

    if verbose:
        print(f"  → {n_v} confirmed violations, {n_f} fragile areas")
        for v in result.get("confirmed_violations", []):
            print(
                f"    [{v.get('severity','?')}] {v.get('function','?')}: {v.get('what_broke','')[:70]}"
            )
        for fa in result.get("fragile_areas", []):
            print(f"    fragile: {fa.get('function','?')} — {fa.get('reason','')[:60]}")

    if output:
        out = output if not output.is_dir() else output / "context.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if improve:
        _improve_context_prompt(result, parse_error, len(git_log), model, key, verbose)

    return result


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="pact context",
        description="Extract violation signals from git history, changelog, and inline notes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              pact context src/click/utils.py --verbose
              pact context src/click/utils.py --out context.json
        """),
    )
    p.add_argument("path", type=Path)
    p.add_argument("--repo", type=Path, help="git repo root (auto-detected if omitted)")
    p.add_argument("--out", type=Path)
    p.add_argument("--model", default=_DEFAULT_MODEL)
    p.add_argument("--api-key")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--improve",
        action="store_true",
        help="rewrite context.md if output is empty despite rich git history (self-improvement)",
    )

    args = p.parse_args(argv)
    extract_context(
        file_path=args.path.resolve(),
        repo_root=args.repo.resolve() if args.repo else None,
        model=args.model,
        api_key=args.api_key,
        output=args.out,
        verbose=args.verbose,
        improve=args.improve,
    )


if __name__ == "__main__":
    main()
