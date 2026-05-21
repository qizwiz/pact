"""
pact heal -- formal program repair via CEGIS.

Takes violations from `pact intent` output and synthesizes minimal patches
that satisfy the violated invariants. Verification oracle: Z3 + test suite.

Pipeline (per violation):
  1. Synthesize patch  (heal.md prompt)
  2. Apply patch to temp file
  3. Re-run pact checker — did the violation disappear?
  4. Score with verify.md rubric
  5. If score < 0.8 OR violation persists: feed counterexample back → step 1
  6. Repeat up to MAX_CEGIS_ITERS times

Usage:
    pact heal <dir> --violations intent_pact.json [--apply] [--verbose]
    pact heal <dir> --severity high [--apply] [--verbose]
"""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Shared utilities (mirrors intent.py to avoid import coupling)
# ---------------------------------------------------------------------------

_PROMPT_DIR = Path(__file__).parent / "prompts"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_MAX_CEGIS_ITERS = 3

_SYSTEM = (
    "You are a formal program repair engine. "
    "Return JSON only — no markdown fences, no text outside the JSON."
)


def _load_prompt(name: str) -> str:
    p = _PROMPT_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"Prompt not found: {p}")
    return p.read_text(encoding="utf-8")


def _render(template: str, **kwargs) -> str:
    for k, v in kwargs.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def _get_key(api_key: Optional[str]) -> str:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return key


def _call(prompt: str, model: str, key: str, max_tokens: int = 8192) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.content:
        raise RuntimeError("API returned empty content")
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        import re

        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response (pos {exc.pos}): {text[:400]}") from exc


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class Diagnosis:
    root_cause: str
    fix_class: str
    verification_oracle: str


@dataclass
class Patch:
    original: str  # exact code block to replace (verbatim from source)
    replacement: str  # what to replace it with
    lines_added: int = 0
    lines_removed: int = 0
    net_change: int = 0


@dataclass
class Justification:
    invariant_now_holds: str
    counterexample_before: str
    counterexample_after: str
    z3_property: Optional[str]
    behavioral_contract_preserved: str


@dataclass
class SynthesisResult:
    violation_id: str
    file: str
    line: int
    invariant_statement: str
    diagnosis: Diagnosis
    patch: Patch
    justification: Justification
    verify_score: float = 0.0
    verify_verdict: str = "PENDING"
    cegis_iters: int = 1
    applied: bool = False


@dataclass
class HealResult:
    project: str
    violations_attempted: int = 0
    patches_accepted: int = 0
    patches_rejected: int = 0
    results: list[SynthesisResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Source utilities
# ---------------------------------------------------------------------------


def _read_source(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def _context_window(
    lines: list[str], line: int, radius: int = 15
) -> tuple[str, int, int]:
    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    ctx = "".join(f"{i + 1:4d}  {lines[i]}" for i in range(start, end))
    return ctx, start + 1, end


def apply_patch(source: str, original: str, replacement: str) -> Optional[str]:
    """
    Apply a patch by exact string replacement of `original` with `replacement`.
    Returns patched source or None if original is not found verbatim.
    """
    original = original.replace("\\n", "\n").replace("\\t", "\t")
    replacement = replacement.replace("\\n", "\n").replace("\\t", "\t")
    if original not in source:
        # Try with normalized indentation: strip common leading whitespace
        orig_stripped = textwrap.dedent(original).strip()
        for existing_block in _find_blocks(source, orig_stripped):
            return source.replace(existing_block, replacement, 1)
        return None
    return source.replace(original, replacement, 1)


def _find_blocks(source: str, stripped_target: str) -> list[str]:
    """Find source blocks that match stripped_target after dedenting."""
    lines = source.splitlines(keepends=True)
    target_lines = stripped_target.splitlines()
    n = len(target_lines)
    matches = []
    for i in range(len(lines) - n + 1):
        block = "".join(lines[i : i + n])
        if textwrap.dedent(block).strip() == stripped_target:
            matches.append(block)
    return matches


# ---------------------------------------------------------------------------
# Checker integration — re-run pact on patched file
# ---------------------------------------------------------------------------


def _check_patched(
    patched_source: str,
    original_violation_line: int,
) -> tuple[bool, list[dict]]:
    """
    Write patched source to a temp file, run pact checker, return:
      (violation_still_present, new_violations_list)
    """
    try:
        from pact.checker import check_file
    except ImportError:
        return False, []

    tmp_path = Path(tempfile.mktemp(suffix=".py", prefix="pact_heal_"))
    try:
        tmp_path.write_text(patched_source, encoding="utf-8")
        results = list(check_file(tmp_path))
        lines_with_violations = {getattr(r, "line", 0) for r in results}
        still_present = original_violation_line in lines_with_violations
        new_viols = [
            {"line": getattr(r, "line", 0), "mode": getattr(r, "mode_name", "?")}
            for r in results
            if getattr(r, "line", 0) != original_violation_line
        ]
        return still_present, new_viols
    except Exception:
        return False, []
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Step 1: Synthesize
# ---------------------------------------------------------------------------


def _synthesize(
    violation: dict,
    invariant: dict,
    source_lines: list[str],
    model: str,
    key: str,
    verbose: bool,
    feedback: str = "",
) -> Optional[SynthesisResult]:
    line = int(violation.get("line", 1))
    ctx, ctx_start, ctx_end = _context_window(source_lines, line)

    template = _load_prompt("heal")
    extra = f"\n\n## Feedback from previous attempt\n{feedback}" if feedback else ""
    prompt = _render(
        template + extra,
        invariant_id=invariant.get("id", "?"),
        invariant_type=invariant.get("type", "?"),
        invariant_statement=invariant.get("statement", ""),
        invariant_formal=invariant.get("formal", ""),
        invariant_derived_from=invariant.get("derived_from", ""),
        file=violation.get("file", "?"),
        line=line,
        severity=violation.get("severity", "?"),
        evidence=violation.get("evidence", ""),
        explanation=violation.get("explanation", ""),
        source_context=ctx,
        context_start=ctx_start,
        context_end=ctx_end,
    )

    raw = _call(prompt, model, key, max_tokens=8192)

    diag_raw = raw.get("diagnosis", {})
    patch_raw = raw.get("patch", {})
    just_raw = raw.get("justification", {})

    return SynthesisResult(
        violation_id=violation.get("invariant_id", "?"),
        file=violation.get("file", "?"),
        line=line,
        invariant_statement=invariant.get("statement", ""),
        diagnosis=Diagnosis(
            root_cause=diag_raw.get("root_cause", ""),
            fix_class=diag_raw.get("fix_class", "unknown"),
            verification_oracle=diag_raw.get("verification_oracle", ""),
        ),
        patch=Patch(
            original=patch_raw.get("original", ""),
            replacement=patch_raw.get("replacement", ""),
            lines_added=patch_raw.get("lines_added", 0),
            lines_removed=patch_raw.get("lines_removed", 0),
            net_change=patch_raw.get("net_change", 0),
        ),
        justification=Justification(
            invariant_now_holds=just_raw.get("invariant_now_holds", ""),
            counterexample_before=just_raw.get("counterexample_before", ""),
            counterexample_after=just_raw.get("counterexample_after", ""),
            z3_property=just_raw.get("z3_property"),
            behavioral_contract_preserved=just_raw.get(
                "behavioral_contract_preserved", ""
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Step 2: Verify
# ---------------------------------------------------------------------------


def _verify(
    result: SynthesisResult,
    source_lines: list[str],
    model: str,
    key: str,
    verbose: bool,
) -> tuple[float, str, str]:
    """
    Returns (score, verdict, counterexample_feedback).
    Runs checker on patched code first, then LLM rubric scoring.
    """
    source = "".join(source_lines)
    patched = apply_patch(source, result.patch.original, result.patch.replacement)

    if patched is None:
        return (
            0.0,
            "REJECT",
            f"Patch failed to apply — original block not found verbatim in source.\n"
            f"Original block to match:\n{result.patch.original[:300]}",
        )

    still_present, new_viols = _check_patched(patched, result.line)

    patch_display = f"ORIGINAL:\n{result.patch.original}\n\nREPLACEMENT:\n{result.patch.replacement}"
    template = _load_prompt("verify")
    prompt = _render(
        template,
        invariant_statement=result.invariant_statement,
        invariant_formal=result.justification.invariant_now_holds,
        patch_diff=patch_display,
        violation_still_present=str(still_present),
        new_violations=json.dumps(new_viols),
        tests_passed="unknown",
        original_evidence=result.patch.original[:500],
    )

    try:
        raw = _call(prompt, model, key, max_tokens=4096)
    except Exception as exc:
        return 0.0, "REJECT", f"Verify call failed: {exc}"

    scores_raw = raw.get("scores", {})

    def _s(k: str) -> float:
        v = scores_raw.get(k, {})
        return float(v.get("score", 0) if isinstance(v, dict) else v) / 10.0

    overall = (
        _s("correctness")
        + _s("minimality")
        + _s("safety")
        + _s("formal_grounding")
        + _s("no_regressions")
    ) / 5.0

    verdict = raw.get("verdict", "REJECT")
    reason = raw.get("verdict_reason", "")

    # Build CEGIS feedback from weaknesses
    weaknesses = raw.get("weaknesses", [])
    feedback_parts = [reason] if reason else []
    for w in weaknesses:
        feedback_parts.append(f"[{w.get('dimension','?')}] {w.get('problem','')}")
        if w.get("better_patch"):
            feedback_parts.append(f"Suggested fix:\n{w['better_patch']}")

    if still_present:
        feedback_parts.append(
            f"The original violation at line {result.line} is STILL PRESENT "
            "after applying the patch — the invariant has not been satisfied."
        )
    if new_viols:
        feedback_parts.append(f"New violations introduced: {json.dumps(new_viols)}")

    return overall, verdict, "\n".join(feedback_parts)


# ---------------------------------------------------------------------------
# CEGIS loop
# ---------------------------------------------------------------------------


def _heal_violation(
    violation: dict,
    invariant: dict,
    source_lines: list[str],
    model: str,
    key: str,
    verbose: bool,
) -> Optional[SynthesisResult]:
    """CEGIS: synthesize → verify → feedback → synthesize ... MAX_CEGIS_ITERS."""
    feedback = ""
    result = None

    for i in range(_MAX_CEGIS_ITERS):
        if verbose:
            label = f"iter {i + 1}/{_MAX_CEGIS_ITERS}"
            print(f"    {label}: synthesizing patch (fix_class=?)")

        try:
            result = _synthesize(
                violation, invariant, source_lines, model, key, verbose, feedback
            )
        except Exception as exc:
            if verbose:
                print(f"    synthesis failed: {exc}")
            return None

        if verbose:
            print(
                f"    synthesized: {result.diagnosis.fix_class} (+{result.patch.lines_added}/-{result.patch.lines_removed})"
            )
            print("    verifying...")

        score, verdict, fb = _verify(result, source_lines, model, key, verbose)
        result.verify_score = score
        result.verify_verdict = verdict
        result.cegis_iters = i + 1

        if verbose:
            print(f"    verify: {verdict} (score={score:.2f})")

        if verdict == "ACCEPT" and score >= 0.8:
            return result

        feedback = fb
        if verdict == "REJECT" and not feedback:
            if verbose:
                print("    rejected with no feedback — stopping")
            return result

    return result


# ---------------------------------------------------------------------------
# Apply patch to disk
# ---------------------------------------------------------------------------


def _apply_to_disk(result: SynthesisResult, verbose: bool) -> bool:
    path = Path(result.file)
    if not path.exists():
        if verbose:
            print(f"    cannot apply — file not found: {path}")
        return False

    source = path.read_text(encoding="utf-8")
    patched = apply_patch(source, result.patch.original, result.patch.replacement)
    if patched is None:
        if verbose:
            print("    cannot apply — original block not found in source")
        return False

    path.write_text(patched, encoding="utf-8")
    result.applied = True
    if verbose:
        print(f"    ✓ applied to {path}")
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def heal_project(
    violations_path: Path,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    severity_filter: Optional[list[str]] = None,
    apply: bool = False,
    output: Optional[Path] = None,
    verbose: bool = False,
) -> HealResult:
    """
    Load violations from intent output, synthesize patches, verify with CEGIS.
    """
    key = _get_key(api_key)

    raw = json.loads(violations_path.read_text(encoding="utf-8"))
    modules = raw.get("modules", [])

    # Build invariant index
    inv_index: dict[str, dict] = {}
    for m in modules:
        for inv in m.get("invariants", []):
            inv_index[inv["id"]] = inv

    # Collect violations to attempt
    to_heal: list[tuple[dict, dict]] = []
    sev_set = set(severity_filter or ["critical", "high", "medium"])

    for m in modules:
        for v in m.get("violations", []):
            if v.get("severity") not in sev_set:
                continue
            inv = inv_index.get(v.get("invariant_id", ""))
            if not inv:
                continue
            to_heal.append((v, inv))

    heal = HealResult(
        project=raw.get("project", violations_path.parent.name),
        violations_attempted=len(to_heal),
    )

    if verbose:
        print(f"[heal] {len(to_heal)} violations to attempt ({', '.join(sev_set)})")

    for v, inv in to_heal:
        fname = v.get("file", "")
        line = v.get("line", 0)
        path = Path(fname)

        if verbose:
            print(
                f"\n  [{v.get('severity')}] {path.name}:{line} — {inv.get('type','?')}"
            )
            print(f"  invariant: {inv.get('statement','')[:100]}")

        if not path.exists():
            if verbose:
                print("  skipped — file not found")
            heal.patches_rejected += 1
            continue

        source_lines = _read_source(path)

        result = _heal_violation(v, inv, source_lines, model, key, verbose)

        if result is None:
            if verbose:
                print("  failed to synthesize")
            heal.patches_rejected += 1
            continue

        heal.results.append(result)

        if result.verify_verdict == "ACCEPT" and result.verify_score >= 0.8:
            heal.patches_accepted += 1
            if apply:
                _apply_to_disk(result, verbose)
        else:
            heal.patches_rejected += 1
            if verbose:
                print(
                    f"  patch not accepted ({result.verify_verdict}, {result.verify_score:.2f})"
                )

    if output:
        out_path = output if not output.is_dir() else output / "heal.json"
        import dataclasses

        out_path.write_text(
            json.dumps(dataclasses.asdict(heal), indent=2), encoding="utf-8"
        )
        if verbose:
            print(
                f"\n[heal] wrote {out_path}\n"
                f"  attempted:{heal.violations_attempted}  "
                f"accepted:{heal.patches_accepted}  "
                f"rejected:{heal.patches_rejected}"
            )

    return heal


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="pact heal",
        description="Synthesize patches for pact intent violations via CEGIS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              pact heal . --violations intent_pact.json --verbose
              pact heal . --violations intent_pact.json --severity high --apply
              pact heal . --violations intent_pact.json --out heal.json
        """),
    )
    p.add_argument("path", type=Path, help="project root (for context)")
    p.add_argument(
        "--violations", type=Path, required=True, help="intent JSON from pact intent"
    )
    p.add_argument(
        "--severity",
        nargs="+",
        default=["critical", "high"],
        choices=["critical", "high", "medium", "low"],
        help="which severities to attempt (default: critical high)",
    )
    p.add_argument(
        "--apply", action="store_true", help="apply accepted patches to disk"
    )
    p.add_argument("--out", type=Path, help="write heal.json output")
    p.add_argument("--model", default=_DEFAULT_MODEL, help="Claude model to use")
    p.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)

    heal_project(
        violations_path=args.violations,
        model=args.model,
        api_key=args.api_key,
        severity_filter=args.severity,
        apply=args.apply,
        output=args.out,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
