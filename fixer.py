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
FIX_MODES = frozenset(
    {"llm_response_unguarded", "missing_await", "optional_dereference", "bare_except"}
)


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
# AST helpers
# ---------------------------------------------------------------------------


def _build_stmt_index(source: str) -> dict[int, int]:
    """
    Return a mapping from every source line number to the start line of the
    innermost statement that contains it.

    Used to find the correct insertion point for guards: when a violation
    falls inside a multi-line expression (e.g. a function-call argument list),
    we insert the guard before the enclosing statement, not at the violation
    line itself.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    # Collect (start, end) for every statement node in the tree
    stmts: list[tuple[int, int]] = []

    def _collect(nodes: list) -> None:
        for node in nodes:
            if not isinstance(node, ast.stmt) or not hasattr(node, "lineno"):
                continue
            stmts.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
            # Recurse into all child statement lists
            for field, value in ast.iter_fields(node):
                if isinstance(value, list):
                    _collect(value)

    _collect(tree.body)

    # For each line, find the innermost (tightest range) enclosing statement
    index: dict[int, int] = {}
    for start, end in stmts:
        for line in range(start, end + 1):
            cur = index.get(line)
            if cur is None or (end - start) < (
                # tighter range wins
                next(
                    (e - s for s, e in stmts if s == cur),
                    end - start + 1,
                )
            ):
                index[line] = start
    return index


# ---------------------------------------------------------------------------
# llm_response_unguarded fix
# ---------------------------------------------------------------------------
# Violation: var.choices[0] (or var.content[0], etc.) without length guard.
# ev.call format: "response.choices[0]"
#
# Fix: insert `if not var.attr:\n    return\n` immediately before the
# statement that contains the unguarded subscript.  The user should review
# whether `return` is correct vs `continue`, `raise`, etc.
#
# Uses _build_stmt_index to handle violations inside multi-line expressions:
# the guard is always inserted before the enclosing statement, not at the
# violation line itself.
# ---------------------------------------------------------------------------


def _fix_llm_unguarded(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Return (patched_lines, applied, skipped)."""
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    _pat = re.compile(r"^(\w+)\.(\w+)\[0\]$")
    stmt_index = _build_stmt_index(source)

    # Map each violation to its enclosing statement start line
    # Multiple violations may map to the same statement — deduplicate guards.
    by_insert_line: dict[int, list[tuple[str, str, FailureEvidence]]] = {}
    for ev in violations:
        m = _pat.match(ev.call)
        if not m:
            skipped.append(ev)
            continue
        var, attr = m.group(1), m.group(2)
        insert_line = stmt_index.get(ev.line, ev.line)
        by_insert_line.setdefault(insert_line, []).append((var, attr, ev))

    result = list(lines)
    for insert_line in sorted(by_insert_line.keys(), reverse=True):
        entries = by_insert_line[insert_line]
        raw_line = result[insert_line - 1]
        indent = " " * (len(raw_line) - len(raw_line.lstrip()))

        guard_lines: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        for var, attr, ev in entries:
            pair = (var, attr)
            if pair in seen_pairs:
                # Same guard already being inserted for this statement
                applied.append(ev)
                continue
            seen_pairs.add(pair)
            guard_lines.append(f"{indent}if not {var}.{attr}:\n")
            guard_lines.append(
                f'{indent}    raise ValueError("LLM returned empty response")  # pact: guard empty {attr} list\n'
            )
            applied.append(ev)

        if guard_lines:
            result[insert_line - 1 : insert_line - 1] = guard_lines

    return result, applied, skipped


# ---------------------------------------------------------------------------
# missing_await fix
# ---------------------------------------------------------------------------
# Violation: async function called without await.
# ev.call format: "trigger_evaluation"  (the callee name)
#
# Fix: prepend `await ` to the call on that line.  Only applied when the
# AST confirms the call is a direct statement (Expr) or the RHS of an
# assignment — never when it's nested inside a larger expression.
# ---------------------------------------------------------------------------

# Coroutine consumers: callers that schedule the coroutine themselves.
# Patterns here mean the coroutine is being *consumed* by an external
# runner — adding await would be wrong.  Includes project-local sync
# wrappers (_run_sync, run_sync, sync_run) found in the corpus.
_CORO_CONSUMERS_RE = re.compile(
    r"\b(asyncio\.run|asyncio\.create_task|asyncio\.ensure_future"
    r"|loop\.run_until_complete|executor\.submit|ThreadPoolExecutor"
    r"|ensure_future|create_task"
    r"|_run_sync|run_sync|sync_run|gevent\.spawn|eventlet\.spawn)\s*\("
)


def _fix_missing_await(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    # Build AST to verify each violation is at statement level
    try:
        tree = ast.parse(source)
    except SyntaxError:
        skipped.extend(violations)
        return list(lines), applied, skipped

    # Collect lines that ARE bare Expr or Assign statements
    stmt_call_lines: dict[int, str] = {}  # line → "expr" | "assign"
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            stmt_call_lines[node.lineno] = "expr"
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            stmt_call_lines[node.lineno] = "assign"
        elif isinstance(node, ast.AugAssign) and isinstance(node.value, ast.Call):
            stmt_call_lines[node.lineno] = "assign"

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
            kind = stmt_call_lines.get(line_no)

            # Skip if inside a coroutine consumer call (asyncio.run etc.)
            # Check surrounding context lines for consumer patterns
            context = "".join(lines[max(0, line_no - 3) : line_no + 1])
            if _CORO_CONSUMERS_RE.search(context):
                skipped.append(ev)
                continue

            if kind == "expr" and re.match(rf"^{re.escape(callee)}\s*\(", stripped):
                result[line_no - 1] = indent + "await " + stripped
                applied.append(ev)
            elif kind == "assign":
                # `x = callee(...)` → `x = await callee(...)`
                m = re.match(rf"^(\w+\s*=\s*)({re.escape(callee)}\s*\(.*)", stripped)
                if m:
                    result[line_no - 1] = indent + m.group(1) + "await " + m.group(2)
                    applied.append(ev)
                else:
                    skipped.append(ev)
            else:
                skipped.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# optional_dereference fix
# ---------------------------------------------------------------------------
# Violation: var used without None check where var was assigned from an
# Optional source (dict.get(), nullable DB field, optional return type).
# ev.call format: "var.attr"  (the dereference that triggered the flag)
#
# Fix: insert `if var is None:\n    raise ValueError("'var' is None")\n`
# immediately before the enclosing statement.  Multiple dereferences of the
# same var in the same statement produce only one guard.
# ---------------------------------------------------------------------------

_OPT_PAT = re.compile(r"^(\w+)\.\w+")


def _fix_optional_dereference(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    stmt_index = _build_stmt_index(source)

    by_insert_line: dict[int, list[tuple[str, FailureEvidence]]] = {}
    for ev in violations:
        m = _OPT_PAT.match(ev.call)
        if not m:
            skipped.append(ev)
            continue
        var = m.group(1)
        insert_line = stmt_index.get(ev.line, ev.line)
        by_insert_line.setdefault(insert_line, []).append((var, ev))

    result = list(lines)
    for insert_line in sorted(by_insert_line.keys(), reverse=True):
        entries = by_insert_line[insert_line]
        raw_line = result[insert_line - 1]
        indent = " " * (len(raw_line) - len(raw_line.lstrip()))

        guard_lines: list[str] = []
        seen_vars: set[str] = set()
        for var, ev in entries:
            if var in seen_vars:
                applied.append(ev)
                continue
            seen_vars.add(var)
            guard_lines.append(f"{indent}if {var} is None:\n")
            guard_lines.append(
                f"{indent}    raise ValueError(f\"'{var}' is None\")  # pact: guard optional dereference\n"
            )
            applied.append(ev)

        if guard_lines:
            result[insert_line - 1 : insert_line - 1] = guard_lines

    return result, applied, skipped


# ---------------------------------------------------------------------------
# bare_except fix
# ---------------------------------------------------------------------------
# Violation: bare `except:` catches KeyboardInterrupt and SystemExit.
# ev.call format: "except:"  (only this variant is fixable; "except Exception: pass"
#                 requires deciding on logging/re-raise — left to the developer)
#
# Fix: replace `except:` with `except Exception:` on the same line, preserving
# indentation and any trailing comment.
# ---------------------------------------------------------------------------

_BARE_EXCEPT_PAT = re.compile(r"^(\s*)except(\s*)(:.*)$")


def _fix_bare_except(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Return (patched_lines, applied, skipped)."""
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    result = list(lines)
    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        # Only handle bare `except:` — the silent-swallow variant needs human judgment
        if ev.call != "except:":
            skipped.append(ev)
            continue
        raw = result[ev.line - 1]
        m = _BARE_EXCEPT_PAT.match(raw.rstrip("\n"))
        if not m:
            skipped.append(ev)
            continue
        indent, _space, rest = m.groups()
        result[ev.line - 1] = f"{indent}except Exception{rest}\n"
        applied.append(ev)

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
    except OSError:
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
        lines, applied, skipped = _fix_llm_unguarded(original, lines, llm_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply missing_await fixes
    await_evs = [v for v in fixable if _mode(v) == "missing_await"]
    if await_evs:
        lines, applied, skipped = _fix_missing_await(original, lines, await_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply optional_dereference fixes
    opt_evs = [v for v in fixable if _mode(v) == "optional_dereference"]
    if opt_evs:
        lines, applied, skipped = _fix_optional_dereference(original, lines, opt_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply bare_except fixes (bare `except:` → `except Exception:`)
    bare_evs = [v for v in fixable if _mode(v) == "bare_except"]
    if bare_evs:
        lines, applied, skipped = _fix_bare_except(original, lines, bare_evs)
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
