"""
pact fixer — automated patch generation for fixable violation modes.

Produces unified diffs (or applies in-place) for violations where the
correct fix is mechanically derivable from the AST. Modes supported:

  llm_response_unguarded       Insert `if not var.attr: raise ValueError(...)` guard
  sheaf_llm_unguarded          Same guard — interprocedural case found by sheaf checker
  json_loads_unguarded         Wrap `json.loads(expr)` in try/except json.JSONDecodeError
  missing_await                Prepend `await` to the unawaited call
  optional_dereference         Insert None guard before nullable dereference
  bare_except                  Replace bare `except:` with `except Exception:`
  mutable_default_arg          Replace mutable default with `None` + if-None guard
  save_without_update_fields   Add `update_fields=[...]` to bare `.save()` calls
  unvalidated_lookup_chain     Replace `collection[x]` with `collection.get(x)`
  asyncio_run_in_async         Replace `asyncio.run(expr)` with `await expr`

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
    {
        "llm_response_unguarded",
        "sheaf_llm_unguarded",
        "missing_await",
        "optional_dereference",
        "bare_except",
        "mutable_default_arg",
        "save_without_update_fields",
        "unvalidated_lookup_chain",
        "asyncio_run_in_async",
        "subprocess_exit_code_unchecked",
        "falsy_or_zero_elision",
        "prompt_injection_risk",
        "json_loads_unguarded",
    }
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

    # Matches: var.attr[0]  OR  var.attr[0].subattr  OR  var.attr[0].sub.sub ...
    _pat = re.compile(r"^(\w+)\.(\w+)\[0\](\.\w+)*$")
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
# mutable_default_arg fix
# ---------------------------------------------------------------------------
# Violation: def fn(x=[], y={}):  — mutable default shared across calls.
# ev.call format: "def function_name"
# ev.line: line of the mutable default expression
#
# Fix:
#   1. Replace the mutable default with `None` (using exact column offsets).
#   2. Insert `if param is None:\n    param = <original>` at the top of the
#      function body (after any docstring).
#
# Processes violations in reverse line order so insertions below don't shift
# the line numbers of defaults above.
# ---------------------------------------------------------------------------


def _fix_mutable_default_arg(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Return (patched_lines, applied, skipped)."""
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        skipped.extend(violations)
        return list(lines), applied, skipped

    # Build lookup: (call_str, default_line) → (func_node, param_name, default_node)
    func_map: dict[tuple[str, int], tuple[ast.AST, str, ast.expr]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        call_str = f"def {node.name}"
        all_args = node.args.args + node.args.posonlyargs
        n_defaults = len(node.args.defaults)
        n_args = len(all_args)
        for i, default in enumerate(node.args.defaults):
            param = all_args[n_args - n_defaults + i].arg
            func_map[(call_str, default.lineno)] = (node, param, default)
        for kwarg, kw_default in zip(node.args.kwonlyargs, node.args.kw_defaults or []):
            if kw_default is not None:
                func_map[(call_str, kw_default.lineno)] = (node, kwarg.arg, kw_default)

    result = list(lines)
    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        key = (ev.call, ev.line)
        if key not in func_map:
            skipped.append(ev)
            continue
        func_node, param_name, default_node = func_map[key]

        # Get the original mutable default text via source segment
        try:
            default_text = ast.get_source_segment(source, default_node)
        except Exception:
            default_text = None
        if not default_text:
            skipped.append(ev)
            continue

        # Step 1: Replace the default value with None using exact column offsets.
        # ev.line is 1-indexed; default may be on the same line as `def`.
        def_line_idx = ev.line - 1
        raw = result[def_line_idx]
        col_s = default_node.col_offset
        col_e = default_node.end_col_offset
        if len(raw.rstrip("\n")) < col_e:
            skipped.append(ev)
            continue
        result[def_line_idx] = raw[:col_s] + "None" + raw[col_e:]

        # Step 2: Find insertion point at start of function body (skip docstring).
        first_stmt = func_node.body[0]
        if (
            isinstance(first_stmt, ast.Expr)
            and isinstance(first_stmt.value, ast.Constant)
            and isinstance(first_stmt.value.value, str)
        ):
            insert_idx = (
                first_stmt.end_lineno
            )  # after docstring (1-indexed end → 0-indexed next)
        else:
            insert_idx = first_stmt.lineno - 1  # before first real statement

        # Determine body indentation from the first statement line.
        first_line = result[first_stmt.lineno - 1]
        body_indent = " " * (len(first_line) - len(first_line.lstrip()))

        # Insert the if-None guard (two lines) at insert_idx.
        none_check = [
            f"{body_indent}if {param_name} is None:\n",
            f"{body_indent}    {param_name} = {default_text}\n",
        ]
        result[insert_idx:insert_idx] = none_check
        applied.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# save_without_update_fields fix
# ---------------------------------------------------------------------------

_AST_NAME_ID = re.compile(r"^Name\(id='([^']+)'")


def _obj_name(node: ast.expr) -> str | None:
    """Return the simple name of an AST expression, or None for complex exprs."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _obj_name(node.value)
    return None


def _collect_non_field_class_attrs(tree: ast.Module) -> frozenset[str]:
    """
    Collect attribute names that are assigned simple Python values (not Django
    Field instances) at class-body level in any class in the module.

    These are Python sentinels / class constants (e.g. `no_changes = False`,
    `objects = Manager()`, `Meta = ...`) — not real database columns.  When
    pact infers `update_fields` from preceding attribute assignments it must
    exclude these, or Django raises FieldDoesNotExist at runtime.

    Pattern detected:
        class SomeModel(models.Model):
            real_field = models.CharField(...)   ← NOT collected (is a Field)
            no_changes = False                   ← collected (simple value)
            _state  = ...                        ← collected (non-Field)
    """
    non_fields: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name):
                continue
            val = stmt.value
            # If the value is a call whose function name ends in "Field", it's a
            # real Django field definition — skip it.
            if isinstance(val, ast.Call):
                func = val.func
                is_field = (
                    isinstance(func, ast.Attribute) and func.attr.endswith("Field")
                ) or (isinstance(func, ast.Name) and func.id.endswith("Field"))
                if is_field:
                    continue
            # Not a Field call — this is a Python class attribute / sentinel.
            non_fields.add(target.id)
    return frozenset(non_fields)


def _collect_preceding_assignments(
    func_body: list[ast.stmt],
    save_lineno: int,
    obj_name: str,
) -> list[str] | None:
    """
    Walk func_body statements BEFORE save_lineno.
    Return the list of attrs set on obj_name (e.g. ["name", "email"]),
    or None if any assignment is conditional/complex (unsafe to infer update_fields).

    Conservative rules:
    - Only count simple `obj.attr = value` at top level (not inside if/for/with).
    - Stop collecting once we pass the most recent binding of obj_name
      (i.e., `obj = SomeModel(...)` or `obj = get_object_or_404(...)` resets the window).
    - Return None (skip) if zero assignments found or if any are in a branch.
    """
    attrs: list[str] = []
    for stmt in reversed(func_body):
        if stmt.lineno >= save_lineno:
            continue

        # Top-level obj.attr = value
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Attribute)
            and isinstance(stmt.targets[0].ctx, ast.Store)
            and _obj_name(stmt.targets[0].value) == obj_name
        ):
            attr = stmt.targets[0].attr
            if attr not in attrs:
                attrs.append(attr)
            continue

        # Top-level augmented assign (obj.attr += 1)
        if (
            isinstance(stmt, ast.AugAssign)
            and isinstance(stmt.target, ast.Attribute)
            and isinstance(stmt.target.ctx, ast.Store)
            and _obj_name(stmt.target.value) == obj_name
        ):
            attr = stmt.target.attr
            if attr not in attrs:
                attrs.append(attr)
            continue

        # Rebinding of obj (e.g. obj = Model(...)) — stop collecting here
        if isinstance(stmt, (ast.Assign, ast.AugAssign)):
            tgts = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for tgt in tgts:
                if isinstance(tgt, ast.Name) and tgt.id == obj_name:
                    # Everything above this rebinding is irrelevant — stop.
                    return attrs or None  # type: ignore[return-value]

        # Any branching control flow (if/for/while/with/try) — bail for safety
        if isinstance(stmt, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            return None

    return attrs or None


def _fix_save_without_update_fields(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """
    obj.save() → obj.save(update_fields=["field1", "field2"])

    Only fixes when we can statically determine the complete set of modified
    fields from unconditional attribute assignments that precede the save() call.
    Skips violations where assignments are inside branches or where no preceding
    attribute assignment is found.
    """
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        skipped.extend(violations)
        return list(lines), applied, skipped

    # Attrs that are Python class constants / sentinels (not Django Fields).
    # These must never appear in update_fields — Django would raise FieldDoesNotExist.
    sentinel_attrs = _collect_non_field_class_attrs(tree)

    # Build map: save_lineno → enclosing function body
    save_line_to_func: dict[int, list[ast.stmt]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Expr):
                continue
            call = stmt.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute) and func.attr == "save"):
                continue
            has_uf = any(
                (isinstance(kw.arg, str) and kw.arg == "update_fields")
                for kw in call.keywords
            )
            if not has_uf:
                save_line_to_func[stmt.lineno] = node.body

    result = list(lines)
    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        if ev.line not in save_line_to_func:
            skipped.append(ev)
            continue

        func_body = save_line_to_func[ev.line]

        # ev.call is like "obj.save" or "self.task.save"
        # The object name is everything before the last ".save"
        call_str = ev.call  # e.g. "user.save" or "self.obj.save"
        if not call_str.endswith(".save"):
            skipped.append(ev)
            continue
        obj_expr = call_str[: -len(".save")]
        # Only handle simple names (e.g. "user", "task") — not chained attrs
        if "." in obj_expr:
            # Use the last segment for attr tracking (e.g. "self.user" → "user")
            obj_name = obj_expr.split(".")[-1]
        else:
            obj_name = obj_expr

        attrs = _collect_preceding_assignments(func_body, ev.line, obj_name)
        if not attrs:
            skipped.append(ev)
            continue

        # Filter out Python sentinels — keep only real DB columns.
        attrs = [a for a in attrs if a not in sentinel_attrs]
        if not attrs:
            skipped.append(ev)
            continue

        # Build `update_fields=[...]` argument string
        fields_list = "[" + ", ".join(f'"{a}"' for a in reversed(attrs)) + "]"

        # Patch the save() line
        line_idx = ev.line - 1
        raw = result[line_idx]
        # Find `.save()` in the line and insert `update_fields=` before the `)`
        # Handle both `obj.save()` and `obj.save(  )` with whitespace
        save_call_re = re.compile(r"(\.save\()\s*(\))")
        new_raw, n = save_call_re.subn(
            rf"\1update_fields={fields_list}\2", raw, count=1
        )
        if n == 0:
            skipped.append(ev)
            continue

        result[line_idx] = new_raw
        applied.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# unvalidated_lookup_chain fix
# ---------------------------------------------------------------------------


def _fix_unvalidated_lookup_chain(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Replace collection[x] with collection.get(x) for unvalidated lookup chains.

    The detector only flags Load-context subscripts (never Store), so every
    violation is safe to convert: KeyError on miss → None.  The caller already
    handles the None case because x itself came from dict.get().
    """
    result = list(lines)
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        call = ev.call  # "collection[var]"
        bracket_pos = call.index("[")
        collection = call[:bracket_pos]
        var = call[bracket_pos + 1 :].rstrip("]")

        line_idx = ev.line - 1
        if line_idx < 0 or line_idx >= len(result):
            skipped.append(ev)
            continue

        raw = result[line_idx]
        old = f"{collection}[{var}]"
        new = f"{collection}.get({var})"

        if old not in raw:
            skipped.append(ev)
            continue

        result[line_idx] = raw.replace(old, new, 1)
        applied.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# asyncio_run_in_async fix
# ---------------------------------------------------------------------------

_ASYNCIO_RUN_RE = re.compile(r"\basyncio\.run\(")


def _find_matching_close(line: str, open_pos: int) -> int | None:
    """Return the index just past the ')' that closes the '(' at open_pos-1.

    open_pos is the position after the opening '('.  Returns None if the
    closing paren is not found on this line (multi-line call — skip).
    """
    depth = 1
    pos = open_pos
    while pos < len(line) and depth > 0:
        ch = line[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        pos += 1
    return pos if depth == 0 else None


def _fix_asyncio_run_in_async(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Replace asyncio.run(expr) with await expr inside async functions.

    Only handles single-line calls — multi-line asyncio.run(...) is skipped
    to avoid mangling indented argument lists.
    """
    result = list(lines)
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        line_idx = ev.line - 1
        if line_idx < 0 or line_idx >= len(result):
            skipped.append(ev)
            continue

        raw = result[line_idx]
        m = _ASYNCIO_RUN_RE.search(raw)
        if not m:
            skipped.append(ev)
            continue

        open_pos = m.end()  # position after the opening '('
        close_pos = _find_matching_close(raw, open_pos)
        if close_pos is None:
            skipped.append(ev)
            continue

        inner = raw[open_pos : close_pos - 1]  # content between ( and )
        # Build: everything before "asyncio.run(" + "await " + inner + rest after ")"
        result[line_idx] = raw[: m.start()] + "await " + inner + raw[close_pos:]
        applied.append(ev)

    return result, applied, skipped


_FALSY_OR_ZERO_RE = re.compile(
    r"(?<![=!<>])(?<!\bif\b)\b([A-Za-z_][A-Za-z0-9_]*)\s+or\s+(0(?:\.0)?)\b"
)


def _fix_falsy_or_zero_elision(
    source: str,  # noqa: ARG001
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Rewrite `var or 0` → `var if var is not None else 0`.

    Only fixes simple single-token left-hand sides (variable names). Complex
    expressions like `total / count or 0` are skipped to avoid double-evaluation.
    The ev.call field carries `<expr> or <zero>` — we parse the variable name
    from there and match it on the line.
    """
    result = list(lines)
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        line_idx = ev.line - 1
        if line_idx < 0 or line_idx >= len(result):
            skipped.append(ev)
            continue

        raw = result[line_idx]

        # Parse call: "<left> or <zero>"
        call = getattr(ev, "call", "") or ""
        if " or " not in call:
            skipped.append(ev)
            continue
        left_expr, _, zero_str = call.partition(" or ")
        left_expr = left_expr.strip()
        zero_str = zero_str.strip()

        # Only handle simple variable names on the left
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", left_expr):
            skipped.append(ev)
            continue

        target = f"{left_expr} or {zero_str}"
        replacement = f"{left_expr} if {left_expr} is not None else {zero_str}"
        if target not in raw:
            skipped.append(ev)
            continue

        result[line_idx] = raw.replace(target, replacement, 1)
        applied.append(ev)

    return result, applied, skipped


# prompt_injection_risk fix
# ---------------------------------------------------------------------------

_PROMPT_INJECTION_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _fix_prompt_injection_risk(
    source: str,  # noqa: ARG001
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Inline newline sanitization into f-string vars passed to LLM content.

    Rewrites `{varname}` → `{varname.replace(chr(10), chr(32))}` for simple
    bare variable names extracted from ev.call.  Uses chr() for both args to
    avoid introducing string literals that would conflict with the outer
    f-string quotes on Python <3.12.  Skips attribute/subscript expressions.
    """
    result = list(lines)
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        line_idx = ev.line - 1
        if line_idx < 0 or line_idx >= len(result):
            skipped.append(ev)
            continue

        call = getattr(ev, "call", "") or ""
        m = _PROMPT_INJECTION_VAR_RE.search(call)
        if not m:
            skipped.append(ev)
            continue

        varname = m.group(1)
        # Match bare {varname} not followed by '.' or '[' (skip attribute/subscript)
        bare_re = re.compile(r"\{" + re.escape(varname) + r"\}(?![.\[])")
        raw = result[line_idx]
        if not bare_re.search(raw):
            skipped.append(ev)
            continue

        replacement = "{" + varname + ".replace(chr(10), chr(32))}"
        result[line_idx] = bare_re.sub(replacement, raw)
        applied.append(ev)

    return result, applied, skipped


# ---------------------------------------------------------------------------
# json_loads_unguarded fix
# ---------------------------------------------------------------------------
# Violation: json.loads(expr) outside a try/except that catches JSONDecodeError.
# ev.call format: "json.loads(expr)"
#
# Fix: wrap the enclosing statement in try/except json.JSONDecodeError.
# Only applied to single-line statements where the violation line equals the
# statement start (multi-line expressions are skipped to avoid corruption).
# ---------------------------------------------------------------------------

_JSON_LOADS_RE = re.compile(r"\bjson\.loads\s*\(")


def _fix_json_loads_unguarded(
    source: str,
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Return (patched_lines, applied, skipped)."""
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    stmt_index = _build_stmt_index(source)

    # Group violations by statement start line (descending to avoid index shifts)
    by_stmt: dict[int, list[FailureEvidence]] = {}
    for ev in violations:
        if not _JSON_LOADS_RE.search(ev.call):
            skipped.append(ev)
            continue
        stmt_line = stmt_index.get(ev.line, ev.line)
        by_stmt.setdefault(stmt_line, []).append(ev)

    result = list(lines)
    for stmt_line in sorted(by_stmt.keys(), reverse=True):
        evs = by_stmt[stmt_line]
        raw = result[stmt_line - 1]
        indent = " " * (len(raw) - len(raw.lstrip()))
        inner = indent + "    "

        # Skip multi-line statements: if next line is more indented, it's a continuation
        if stmt_line < len(result):
            nxt = result[stmt_line]
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) > len(indent):
                skipped.extend(evs)
                continue

        # Build try/except wrapper
        wrapped = [
            f"{indent}try:\n",
            f"{inner}{raw.lstrip()}",
            f"{indent}except json.JSONDecodeError as exc:\n",
            f'{inner}raise ValueError(f"Invalid JSON: {{exc}}") from exc\n',
        ]
        result[stmt_line - 1 : stmt_line] = wrapped
        applied.extend(evs)

    return result, applied, skipped


# ---------------------------------------------------------------------------
_SUBPROCESS_RUN_RE = re.compile(r"\bsubprocess\.(run|call|Popen)\s*\(")


def _fix_subprocess_exit_code(
    source: str,  # noqa: ARG001
    lines: list[str],
    violations: list[FailureEvidence],
) -> tuple[list[str], list[FailureEvidence], list[FailureEvidence]]:
    """Add check=True to subprocess.run/call() calls missing an exit-code check.

    Only fixes single-line calls where the closing ) is on the same line.
    Skips calls that already contain 'check=' (shouldn't appear per detector,
    but defensive) and multi-line calls.
    """
    result = list(lines)
    applied: list[FailureEvidence] = []
    skipped: list[FailureEvidence] = []

    for ev in sorted(violations, key=lambda e: e.line, reverse=True):
        line_idx = ev.line - 1
        if line_idx < 0 or line_idx >= len(result):
            skipped.append(ev)
            continue

        raw = result[line_idx]
        m = _SUBPROCESS_RUN_RE.search(raw)
        if not m:
            skipped.append(ev)
            continue

        if "check=" in raw:
            skipped.append(ev)
            continue

        open_pos = m.end()  # position after the opening '('
        close_pos = _find_matching_close(raw, open_pos)
        if close_pos is None:
            skipped.append(ev)
            continue

        # close_pos points one past the ')' character
        inner = raw[open_pos : close_pos - 1].rstrip()
        if inner:
            new_inner = inner + ", check=True"
        else:
            new_inner = "check=True"
        result[line_idx] = raw[:open_pos] + new_inner + raw[close_pos - 1 :]
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

    # Apply llm_response_unguarded + sheaf_llm_unguarded fixes (same guard pattern)
    llm_evs = [
        v
        for v in fixable
        if _mode(v) in {"llm_response_unguarded", "sheaf_llm_unguarded"}
    ]
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

    # Apply mutable_default_arg fixes (mutable defaults → None + if-None guard)
    mut_evs = [v for v in fixable if _mode(v) == "mutable_default_arg"]
    if mut_evs:
        lines, applied, skipped = _fix_mutable_default_arg(original, lines, mut_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply save_without_update_fields fixes (obj.save() → obj.save(update_fields=[...]))
    save_evs = [v for v in fixable if _mode(v) == "save_without_update_fields"]
    if save_evs:
        lines, applied, skipped = _fix_save_without_update_fields(
            original, lines, save_evs
        )
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply unvalidated_lookup_chain fixes (collection[x] → collection.get(x))
    lookup_evs = [v for v in fixable if _mode(v) == "unvalidated_lookup_chain"]
    if lookup_evs:
        lines, applied, skipped = _fix_unvalidated_lookup_chain(
            original, lines, lookup_evs
        )
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply asyncio_run_in_async fixes (asyncio.run(expr) → await expr)
    async_run_evs = [v for v in fixable if _mode(v) == "asyncio_run_in_async"]
    if async_run_evs:
        lines, applied, skipped = _fix_asyncio_run_in_async(
            original, lines, async_run_evs
        )
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply falsy_or_zero_elision fixes (var or 0 → var if var is not None else 0)
    falsy_evs = [v for v in fixable if _mode(v) == "falsy_or_zero_elision"]
    if falsy_evs:
        lines, applied, skipped = _fix_falsy_or_zero_elision(original, lines, falsy_evs)
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply subprocess_exit_code_unchecked fixes (add check=True)
    subproc_evs = [v for v in fixable if _mode(v) == "subprocess_exit_code_unchecked"]
    if subproc_evs:
        lines, applied, skipped = _fix_subprocess_exit_code(
            original, lines, subproc_evs
        )
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply prompt_injection_risk fixes ({var} → {var.replace(chr(10), " ")})
    inject_evs = [v for v in fixable if _mode(v) == "prompt_injection_risk"]
    if inject_evs:
        lines, applied, skipped = _fix_prompt_injection_risk(
            original, lines, inject_evs
        )
        all_applied.extend(applied)
        all_skipped.extend(skipped)

    # Apply json_loads_unguarded fixes (wrap in try/except json.JSONDecodeError)
    json_evs = [v for v in fixable if _mode(v) == "json_loads_unguarded"]
    if json_evs:
        lines, applied, skipped = _fix_json_loads_unguarded(original, lines, json_evs)
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
