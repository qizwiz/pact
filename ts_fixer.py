"""
pact ts_fixer — automated patch generation for JavaScript/TypeScript violations.

Produces unified diffs (or applies in-place) for JS/TS violations where the
correct fix is mechanically derivable from the source. Modes supported:

  empty_catch    Insert `console.error(e);` into empty catch blocks
                 (truly empty only; comment-only bodies are skipped)
  missing_await  Prepend `await` to unawaited async calls inside async functions

Usage
-----
    from pact.ts_fixer import fix_ts_file, TS_FIX_MODES

    patched_source, applied = fix_ts_file(path, violations)
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import NamedTuple

# Modes this fixer can handle
TS_FIX_MODES = frozenset({"empty_catch", "missing_await"})

# Matches: catch (varName)
_CATCH_WITH_PARAM_RE = re.compile(r"\bcatch\s*\(\s*(\w+)\s*\)")
# Matches: catch { (no binding — TypeScript optional catch binding)
_CATCH_NO_PARAM_RE = re.compile(r"\bcatch\s*\{")


def _mode(v: object) -> str:
    return getattr(v, "mode_name", getattr(v, "context", ""))


class TsFileResult(NamedTuple):
    path: str
    original: str
    patched: str
    applied: list
    skipped: list

    @property
    def changed(self) -> bool:
        return self.original != self.patched


def diff_text(path: str, original: str, patched: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


# ---------------------------------------------------------------------------
# empty_catch fix
# ---------------------------------------------------------------------------
# Violation: catch (e) {} or catch {} — exception silently swallowed.
# ev.line: 1-indexed line of the `catch` keyword.
#
# Fix (only truly empty bodies — no comments):
#   catch (e) {    →   catch (e) {
#   }                      console.error(e);
#                      }
#
#   catch {        →   catch (e) {
#   }                      console.error(e);
#                      }
#
# Comment-only bodies are skipped — those represent intentional suppression.
# ---------------------------------------------------------------------------


def _find_matching_brace(
    lines: list[str], open_line_idx: int, brace_col: int
) -> int | None:
    """Return 0-indexed line of the '}' that closes the '{' at brace_col on open_line_idx.

    Starts depth counting at the exact '{' position so that preceding '}' characters
    on the same line (e.g., `} catch (e) {`) are ignored.
    Returns None if no match found within 100 lines.
    """
    depth = 0
    for idx in range(open_line_idx, min(len(lines), open_line_idx + 100)):
        start = brace_col if idx == open_line_idx else 0
        for ch in lines[idx][start:]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return idx
    return None


def _body_is_truly_empty(
    lines: list[str], open_idx: int, close_idx: int, open_col: int
) -> bool:
    """Return True only if the catch body has no non-whitespace content.

    open_col: column index of the opening '{' on open_idx.
    """
    if open_idx == close_idx:
        raw = lines[open_idx]
        # Find the first '}' after the opening '{'
        close_pos = raw.find("}", open_col + 1)
        if close_pos < 0:
            return False
        inner = raw[open_col + 1 : close_pos]
        return not inner.strip()

    for idx in range(open_idx + 1, close_idx):
        if lines[idx].strip():
            return False
    return True


def _fix_empty_catch(
    source: str,
    lines: list[str],
    violations: list,
) -> tuple[list[str], list, list]:
    """Insert console.error into truly empty catch blocks.

    Skips:
    - Comment-only bodies (intentional suppression)
    - Multi-line asyncio.run-style inline calls (can't reconstruct safely)
    - Any catch we can't parse unambiguously

    Processes violations in reverse line order to keep line numbers stable
    after insertions.
    """
    result = list(lines)
    applied: list = []
    skipped: list = []

    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        line_idx = ev.line - 1
        if line_idx < 0 or line_idx >= len(result):
            skipped.append(ev)
            continue

        # --- Step 1: find catch parameter, opening brace line, and brace column ---
        var: str | None = None
        catch_header_idx: int | None = None
        open_brace_idx: int | None = None
        open_brace_col: int | None = None  # column of the catch body's opening {

        for offset in range(3):
            idx = line_idx + offset
            if idx >= len(result):
                break
            raw = result[idx]

            m_param = _CATCH_WITH_PARAM_RE.search(raw)
            if m_param:
                var = m_param.group(1)
                catch_header_idx = idx
                # Opening brace may be on same line or next
                brace_pos = raw.find("{", m_param.end())
                if brace_pos >= 0:
                    open_brace_idx = idx
                    open_brace_col = brace_pos
                elif idx + 1 < len(result):
                    brace_pos2 = result[idx + 1].find("{")
                    if brace_pos2 >= 0:
                        open_brace_idx = idx + 1
                        open_brace_col = brace_pos2
                break

            m_no_param = _CATCH_NO_PARAM_RE.search(raw)
            if m_no_param:
                var = None
                catch_header_idx = idx
                # `catch {` — brace is last char of the match
                open_brace_idx = idx
                open_brace_col = m_no_param.end() - 1  # position of {
                break

        if catch_header_idx is None or open_brace_idx is None or open_brace_col is None:
            skipped.append(ev)
            continue

        # --- Step 2: find matching closing brace ---
        close_brace_idx = _find_matching_brace(result, open_brace_idx, open_brace_col)
        if close_brace_idx is None:
            skipped.append(ev)
            continue

        # --- Step 3: skip comment-only bodies ---
        if not _body_is_truly_empty(
            result, open_brace_idx, close_brace_idx, open_brace_col
        ):
            skipped.append(ev)
            continue

        # --- Step 4: determine indentation ---
        close_line = result[close_brace_idx]
        close_indent_len = len(close_line) - len(close_line.lstrip())
        close_indent = close_line[:close_indent_len]
        body_indent = close_indent + "  "

        # --- Step 5: build fix ---
        error_var = var if var else "e"

        if open_brace_idx == close_brace_idx:
            # Single-line: `catch (e) {}` or `catch {}` — expand it
            raw = result[open_brace_idx]
            open_pos = open_brace_col
            close_pos = raw.find("}", open_pos + 1)
            # Everything before {, then expanded body, then closing }
            before_brace = raw[: open_pos + 1]
            after_brace = raw[close_pos:]

            if var is None:
                # `catch {` → `catch (e) {`
                # Replace the catch { with catch (e) {
                before_brace = re.sub(
                    r"\bcatch\s*\{", "catch (e) {", before_brace, count=1
                )

            new_lines = [
                before_brace.rstrip("\n") + "\n",
                f"{body_indent}console.error({error_var});\n",
                after_brace.rstrip("\n") + "\n",
            ]
            result[open_brace_idx : close_brace_idx + 1] = new_lines
        else:
            # Multi-line empty body — just insert before the closing brace
            if var is None:
                # Also update the catch header: `catch {` → `catch (e) {`
                header_raw = result[catch_header_idx]
                result[catch_header_idx] = re.sub(
                    r"\bcatch\s*\{", "catch (e) {", header_raw, count=1
                )

            result.insert(
                close_brace_idx, f"{body_indent}console.error({error_var});\n"
            )

        applied.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# missing_await fix
# ---------------------------------------------------------------------------
# Violation: async call inside an async function with no `await`.
# ev.line: 1-indexed line of the call_expression.
# ev.call: text of the call_expression node, truncated at 60 chars.
#
# Fix:
#   const result = axios.get(url);   →  const result = await axios.get(url);
#   fetch("/api");                   →  await fetch("/api");
#
# The call text is used to locate the exact position in the source line.
# For truncated call text (>60 chars), a 20-char prefix is used.
# Already-awaited calls are skipped (shouldn't appear but checked for safety).
# ---------------------------------------------------------------------------


def _fix_missing_await(
    source: str,  # noqa: ARG001 — unused but kept for API consistency with _fix_empty_catch
    lines: list[str],
    violations: list,
) -> tuple[list[str], list, list]:
    """Prepend `await` to unawaited async call expressions."""
    result = list(lines)
    applied: list = []
    skipped: list = []

    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        line_idx = ev.line - 1
        if line_idx < 0 or line_idx >= len(result):
            skipped.append(ev)
            continue

        raw = result[line_idx]
        call_text: str = getattr(ev, "call", "")
        if not call_text:
            skipped.append(ev)
            continue

        # Find the call's start position in the line
        pos = raw.find(call_text)
        if pos < 0 and len(call_text) >= 20:
            # Truncated call — match on the first 20 chars as a prefix
            pos = raw.find(call_text[:20])
        if pos < 0:
            skipped.append(ev)
            continue

        # Guard: don't double-insert if `await` already precedes this position
        before = raw[:pos]
        if before.rstrip().endswith("await"):
            skipped.append(ev)
            continue

        result[line_idx] = before + "await " + raw[pos:]
        applied.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fix_ts_file(
    path: str | Path,
    violations: list,
) -> TsFileResult:
    """Apply all fixable TS violations to the file at `path`.

    Returns a TsFileResult with patched source and applied/skipped lists.
    """
    path = str(path)
    try:
        original = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return TsFileResult(
            path=path,
            original="",
            patched="",
            applied=[],
            skipped=violations,
        )

    lines = original.splitlines(keepends=True)
    all_applied: list = []
    all_skipped: list = []

    fixable = [v for v in violations if _mode(v) in TS_FIX_MODES]
    unfixable = [v for v in violations if _mode(v) not in TS_FIX_MODES]
    all_skipped.extend(unfixable)

    catch_evs = [v for v in fixable if _mode(v) == "empty_catch"]
    if catch_evs:
        lines, applied, skipped = _fix_empty_catch(original, lines, catch_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    await_evs = [v for v in fixable if _mode(v) == "missing_await"]
    if await_evs:
        lines, applied, skipped = _fix_missing_await(original, lines, await_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    patched = "".join(lines)
    return TsFileResult(
        path=path,
        original=original,
        patched=patched,
        applied=all_applied,
        skipped=all_skipped,
    )
