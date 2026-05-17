"""
pact fixer — automated patch generation for fixable violation modes.

Produces unified diffs (or applies in-place) for violations where the
correct fix is mechanically derivable from the AST. Modes supported:

  llm_response_unguarded  Insert `if not var.attr: return` guard
  missing_await           Prepend `await` to the unawaited call

save_without_update_fields is intentionally excluded: the correct
update_fields list requires tracking which fields were mutated before
the .save() call — a deeper analysis than line-level patching supports.

Usage
-----
    from pact.fixer import fix_file, apply_fixes, FIX_MODES

    # Get a patched source string + list of applied violations
    patched, applied = fix_file(path, violations)

    # unified diff
    print(diff_text(path, original, patched))
"""

from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path
from typing import NamedTuple

from .failure_mode import FailureEvidence

# Modes that this fixer can handle
FIX_MODES = frozenset({"llm_response_unguarded", "missing_await"})


def _mode(ev) -> str:
    """Return the violation mode name regardless of evidence type."""
    return getattr(ev, "mode_name", None) or getattr(ev, "context", "")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class FileResult(NamedTuple):
    path: str
    original: str
    patched: str
    applied: list[FailureEvidence]
    skipped: list[FailureEvidence]

    @property
    def changed(self) -> bool:
        return self.original != self.patched


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------


def diff_text(path: str, original: str, patched: str) -> str:
    orig_lines = original.splitlines(keepends=True)
    new_lines = patched.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


# ---------------------------------------------------------------------------
# llm_response_unguarded fix
# ---------------------------------------------------------------------------
# Violation: var.choices[0] (or var.content[0], etc.) without length guard.
# ev.call format: "response.choices[0]"
#
# Fix: insert `if not var.attr:\n    return\n` immediately before the
# statement that contains the unguarded subscript.  The user should review
# whether `return` is correct vs `continue`, `raise`, etc.
# ---------------------------------------------------------------------------


def _fix_llm_unguarded(
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Return (patched_lines, applied, skipped)."""
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    # Parse var/attr from ev.call = "response.choices[0]"
    _pat = re.compile(r"^(\w+)\.(\w+)\[0\]$")

    # Group violations by line; process in reverse so insertions don't shift
    # later line numbers.
    by_line: dict[int, list[FailureEvidence]] = {}
    for ev in violations:
        by_line.setdefault(ev.line, []).append(ev)

    result = list(lines)
    for line_no in sorted(by_line.keys(), reverse=True):
        evs = by_line[line_no]
        raw_line = result[line_no - 1]
        indent = " " * (len(raw_line) - len(raw_line.lstrip()))

        guard_lines: list[str] = []
        for ev in evs:
            m = _pat.match(ev.call)
            if not m:
                skipped.append(ev)
                continue
            var, attr = m.group(1), m.group(2)
            guard_lines.append(f"{indent}if not {var}.{attr}:\n")
            guard_lines.append(f"{indent}    return  # pact: guard empty {attr} list\n")
            applied.append(ev)

        if guard_lines:
            result[line_no - 1 : line_no - 1] = guard_lines

    return result, applied, skipped


# ---------------------------------------------------------------------------
# missing_await fix
# ---------------------------------------------------------------------------
# Violation: async function called without await.
# ev.call format: "trigger_evaluation"  (the callee name)
#
# Fix: prepend `await ` to the call on that line.  Only applied when the
# call appears at statement level (not inside a larger expression we can't
# safely rewrite without full AST unparsing).
# ---------------------------------------------------------------------------


def _fix_missing_await(
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    by_line: dict[int, list[FailureEvidence]] = {}
    for ev in violations:
        by_line.setdefault(ev.line, []).append(ev)

    result = list(lines)
    for line_no in sorted(by_line.keys(), reverse=True):
        evs = by_line[line_no]
        raw_line = result[line_no - 1]
        stripped = raw_line.lstrip()
        indent = raw_line[: len(raw_line) - len(stripped)]

        for ev in evs:
            callee = ev.call.strip()
            # Case 1: bare call statement — `callee(...)`
            if re.match(rf"^{re.escape(callee)}\s*\(", stripped):
                result[line_no - 1] = indent + "await " + stripped
                applied.append(ev)
            # Case 2: assignment — `x = callee(...)` → `x = await callee(...)`
            elif m := re.match(rf"^(\w+\s*=\s*)({re.escape(callee)}\s*\(.*)", stripped):
                result[line_no - 1] = indent + m.group(1) + "await " + m.group(2)
                applied.append(ev)
            else:
                skipped.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fix_file(
    path: str | Path,
    violations: list[FailureEvidence],
) -> FileResult:
    """
    Apply all fixable violations in `violations` to the file at `path`.

    Returns a FileResult with the patched source and lists of which
    violations were applied vs skipped (unfixable by this tool).
    """
    path = str(path)
    try:
        original = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return FileResult(
            path=path,
            original="",
            patched="",
            applied=[],
            skipped=violations,
        )

    lines = original.splitlines(keepends=True)
    all_applied: list[FailureEvidence] = []
    all_skipped: list[FailureEvidence] = []

    fixable = [v for v in violations if _mode(v) in FIX_MODES]
    unfixable = [v for v in violations if _mode(v) not in FIX_MODES]
    all_skipped.extend(unfixable)

    # Apply llm_response_unguarded fixes
    llm_evs = [v for v in fixable if _mode(v) == "llm_response_unguarded"]
    if llm_evs:
        lines, applied, skipped = _fix_llm_unguarded(lines, llm_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply missing_await fixes
    await_evs = [v for v in fixable if _mode(v) == "missing_await"]
    if await_evs:
        lines, applied, skipped = _fix_missing_await(lines, await_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    patched = "".join(lines)
    return FileResult(
        path=path,
        original=original,
        patched=patched,
        applied=all_applied,
        skipped=all_skipped,
    )


def apply_fixes(
    violations: list[FailureEvidence],
    *,
    dry_run: bool = True,
    mode_filter: frozenset[str] | None = None,
) -> list[FileResult]:
    """
    Apply fixes for all violations, grouped by file.

    Parameters
    ----------
    violations:
        All violations from a pact scan.
    dry_run:
        If True (default), do not write any files.
    mode_filter:
        If given, only fix violations whose mode_name is in this set.

    Returns
    -------
    List of FileResult — one per file that had at least one fixable violation.
    """
    if mode_filter:
        violations = [v for v in violations if _mode(v) in mode_filter]

    by_file: dict[str, list[FailureEvidence]] = {}
    for ev in violations:
        by_file.setdefault(ev.file, []).append(ev)

    results: list[FileResult] = []
    for file_path, evs in by_file.items():
        result = fix_file(file_path, evs)
        if result.changed and not dry_run:
            Path(file_path).write_text(result.patched, encoding="utf-8")
        results.append(result)
    return results
