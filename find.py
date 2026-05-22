"""
pact find -- property-driven violation discovery.

Replaces pattern-matching checker with:
  1. find.md prompt  → candidate counterexamples per function
  2. Hypothesis      → confirms which candidates actually fail
  3. intent.json     → confirmed failures with real counterexamples

Usage:
    pact find <file_or_dir> [--out intent.json] [--verbose]
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Optional

_PROMPT_DIR = Path(__file__).parent / "prompts"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_SYSTEM = (
    "You are a formal property extractor. "
    "Return JSON only — no markdown fences, no text outside the JSON."
)

_READ_FILE_TOOL = {
    "name": "read_file_lines",
    "description": "Read a range of lines from a source file. Line numbers are 1-indexed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "default": 1},
            "end_line": {"type": "integer"},
        },
        "required": ["path"],
    },
}


def _execute_read_file(inp: dict) -> str:
    try:
        path = Path(inp["path"])
        if not path.exists():
            return f"[error: file not found: {path}]"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True
        )
        start = max(0, int(inp.get("start_line", 1)) - 1)
        end = int(inp["end_line"]) if inp.get("end_line") else len(lines)
        chunk = lines[start:end]
        return "".join(f"{start + i + 1:4d}  {line}" for i, line in enumerate(chunk))
    except Exception as exc:
        return f"[error: {exc}]"


def _parse(text: str) -> dict:
    import re

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = re.sub(r"```\s*$", "", text).strip()
    for candidate in [text, text[text.find("{") :] if "{" in text else ""]:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass
    raise RuntimeError(f"no valid JSON in response: {text[:300]}")


def _call_with_tools(prompt: str, model: str, key: str, max_rounds: int = 8) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for _ in range(max_rounds):
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=_SYSTEM,
            tools=[_READ_FILE_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _execute_read_file(block.input),
                        }
                    )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": results})
        else:
            text = "".join(b.text for b in response.content if hasattr(b, "text"))
            return _parse(text)

    raise RuntimeError(f"tool loop exhausted after {max_rounds} rounds")


def _confirm_with_hypothesis(
    prop: dict, file_path: Path, verbose: bool
) -> Optional[str]:
    """
    Run Hypothesis to confirm the candidate counterexample actually breaks the code.
    Returns the concrete counterexample string if confirmed, None if it doesn't fail.
    """
    strategy = prop.get("hypothesis_strategy", "")
    predicate = prop.get("hypothesis_predicate", "")
    hint = prop.get("counterexample_hint", "")

    if not strategy or not predicate:
        return hint if hint else None

    # Build a minimal test module and run it as a subprocess
    import subprocess
    import tempfile
    import sys

    test_src = textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, str({str(file_path.parent)!r}))
        from hypothesis import given, settings, HealthCheck
        from hypothesis import strategies as st
        import json, warnings

        counterexample = []

        @given({strategy})
        @settings(max_examples=200, suppress_health_check=list(HealthCheck))
        def test_property(x):
            pred = {predicate}
            try:
                result = pred(x)
                if result is False:
                    counterexample.append(repr(x))
            except Exception as e:
                counterexample.append(repr(x))

        test_property()
        if counterexample:
            print("FOUND:" + counterexample[0])
        else:
            print("NONE")
    """)

    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(test_src)
            tmp = f.name

        r = subprocess.run(
            [sys.executable, tmp],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(file_path.parent),
        )
        output = r.stdout.strip()
        if verbose:
            print(f"      hypothesis: {output[:80]}")
        if output.startswith("FOUND:"):
            return output[6:]
        return None
    except Exception as exc:
        if verbose:
            print(f"      hypothesis error: {exc}")
        return hint if hint else None
    finally:
        Path(tmp).unlink(missing_ok=True)


def find_violations(
    path: Path,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    output: Optional[Path] = None,
    verbose: bool = False,
    use_context: bool = True,
    improve: bool = False,
) -> dict:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    template = (_PROMPT_DIR / "find.md").read_text(encoding="utf-8")

    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    files = [
        f
        for f in files
        if not any(p in f.parts for p in ("__pycache__", ".venv", "node_modules"))
    ]

    modules = []
    parse_failures: list[dict] = []

    for fpath in files:
        source = fpath.read_text(encoding="utf-8", errors="replace")
        if len(source) > 60_000:
            source = (
                source[:60_000]
                + "\n# ... (truncated — use read_file_lines for the rest)"
            )

        if verbose:
            print(f"  → {fpath.name} ({len(source):,} bytes)")

        # Pull git/changelog/comment signals as a prior
        git_context = "{}"
        if use_context:
            try:
                from .context import extract_context

                ctx = extract_context(fpath, model=model, api_key=key, verbose=verbose)
                git_context = json.dumps(ctx, indent=2)
            except Exception as exc:
                if verbose:
                    print(f"    context skipped: {exc}")

        prompt = (
            template.replace("{{file_path}}", str(fpath))
            .replace("{{source}}", source)
            .replace("{{git_context}}", git_context)
        )

        try:
            raw = _call_with_tools(prompt, model, key)
        except Exception as exc:
            parse_failures.append({"file": str(fpath), "error": str(exc)})
            if verbose:
                print(f"    skipped: {exc}")
            continue

        props = raw.get("properties", [])
        if not props:
            if verbose:
                print("    no properties found")
            continue

        # Confirm each candidate with Hypothesis
        invariants = []
        violations = []

        for i, prop in enumerate(props):
            inv_id = f"{fpath.stem}_{i}"
            if verbose:
                print(
                    f"    [{prop.get('severity','?')}] {prop.get('function','?')}:{prop.get('line','?')} — {prop.get('statement','')[:60]}"
                )
                print(f"      hint: {prop.get('counterexample_hint','')[:60]}")

            confirmed = _confirm_with_hypothesis(prop, fpath, verbose)

            invariants.append(
                {
                    "id": inv_id,
                    "type": "property_violation",
                    "statement": prop.get("statement", ""),
                    "confidence": 0.95 if confirmed else 0.7,
                    "severity": prop.get("severity", "medium"),
                    "counterexample": confirmed,
                }
            )

            violations.append(
                {
                    "invariant_id": inv_id,
                    "file": str(fpath),
                    "line": prop.get("line", 0),
                    "severity": prop.get("severity", "medium"),
                    "evidence": prop.get("counterexample_hint", ""),
                    "explanation": prop.get("why_it_matters", ""),
                    "hypothesis_confirmed": confirmed is not None,
                    "counterexample": confirmed,
                }
            )

            status = "✓ confirmed" if confirmed else "? unconfirmed"
            if verbose:
                print(f"      {status}")

        modules.append(
            {
                "path": str(fpath),
                "purpose": f"{fpath.name}",
                "invariants": invariants,
                "violations": violations,
            }
        )

    result = {
        "project": path.name,
        "generated_by": "pact.find",
        "modules": modules,
    }

    total_v = sum(len(m["violations"]) for m in modules)
    total_confirmed = sum(
        1 for m in modules for v in m["violations"] if v.get("hypothesis_confirmed")
    )

    if verbose or True:
        print(
            f"\n[find] {len(modules)} files, {total_v} violations, {total_confirmed} hypothesis-confirmed"
        )

    if output:
        out = output if not output.is_dir() else output / "find.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        if verbose:
            print(f"[find] wrote {out}")

    if improve:
        _improve_find_prompt(result, parse_failures, model, key, verbose)

    return result


# ---------------------------------------------------------------------------
# Self-improvement (find_improve.md rubric)
# ---------------------------------------------------------------------------


def _improve_find_prompt(
    result: dict,
    parse_failures: list[dict],
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """Score find prompt and rewrite if hypothesis confirmation rate < 30% or parse failures exist."""
    all_violations = [
        v for m in result.get("modules", []) for v in m.get("violations", [])
    ]
    if not all_violations and not parse_failures:
        return

    total = len(all_violations)
    confirmed = sum(1 for v in all_violations if v.get("hypothesis_confirmed"))
    confirm_rate = confirmed / total if total else 0.0

    missing_strategy = [
        v
        for v in all_violations
        if not v.get("counterexample") and not v.get("hypothesis_confirmed")
    ]

    # Only improve if there's a real signal of failure
    if (
        confirm_rate >= 0.30
        and not parse_failures
        and len(missing_strategy) < total * 0.5
    ):
        return

    confirmed_samples = [v for v in all_violations if v.get("hypothesis_confirmed")][:3]
    unconfirmed_samples = missing_strategy[:5]

    failure_modes: list[str] = []
    for pf in parse_failures[:5]:
        failure_modes.append(
            f"parse_failure: {pf.get('file','?')} — {pf.get('error','?')[:120]}"
        )
    if total > 0:
        failure_modes.append(
            f"hypothesis_confirmation_rate: {confirmed}/{total} = {confirm_rate:.0%} (threshold 30%)"
        )
    no_strategy = sum(1 for v in all_violations if not v.get("counterexample"))
    if no_strategy:
        failure_modes.append(
            f"missing_hypothesis_strategy: {no_strategy}/{total} violations have no runnable strategy"
        )

    try:
        template = (_PROMPT_DIR / "find_improve.md").read_text(encoding="utf-8")
        prompt = (
            template.replace(
                "{{prompt_text}}", (_PROMPT_DIR / "find.md").read_text(encoding="utf-8")
            )
            .replace("{{confirmed_samples}}", json.dumps(confirmed_samples, indent=2))
            .replace(
                "{{unconfirmed_samples}}", json.dumps(unconfirmed_samples, indent=2)
            )
            .replace("{{failure_modes}}", "\n".join(failure_modes) or "none")
        )

        import anthropic

        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system="You are a prompt engineer. Return JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        raw = _parse(text)
        improved = raw.get("improved_prompt", "")
        overall = raw.get("overall_score", 0.0)
        if improved and overall < 0.8:
            (_PROMPT_DIR / "find.md").write_text(improved, encoding="utf-8")
            if verbose:
                print(
                    f"\n[find] ✓ find prompt rewritten (score was {overall:.2f}, confirm_rate {confirm_rate:.0%})"
                )
    except Exception as exc:
        if verbose:
            print(f"\n[find] prompt improvement failed: {exc}")


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="pact find",
        description="Property-driven violation discovery via LLM + Hypothesis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              pact find src/click/utils.py --verbose
              pact find src/click/ --out find.json
              pact find src/click/ --out find.json | pact heal . --violations find.json --test-cmd "pytest tests/ -x -q"
        """),
    )
    p.add_argument("path", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--model", default=_DEFAULT_MODEL)
    p.add_argument("--api-key")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--improve",
        action="store_true",
        help="rewrite find.md if hypothesis confirmation rate < 30%% (self-improvement)",
    )
    p.add_argument(
        "--no-context",
        action="store_true",
        help="skip git/changelog context extraction",
    )

    args = p.parse_args(argv)
    find_violations(
        path=args.path.resolve(),
        model=args.model,
        api_key=args.api_key,
        output=args.out,
        verbose=args.verbose,
        use_context=not args.no_context,
        improve=args.improve,
    )


if __name__ == "__main__":
    main()
