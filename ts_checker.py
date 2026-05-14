"""
TypeScript / TSX constraint checker (tree-sitter backed).

Failure modes:
  missing_await        — async call without await inside an async function
  optional_dereference — .find()/.shift()/.pop() result used without null guard
  empty_catch          — catch (e) {} — silent error suppression
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

try:
    import tree_sitter as _ts
    import tree_sitter_typescript as _tstype

    _TS_LANGUAGE = _ts.Language(_tstype.language_typescript())
    _TSX_LANGUAGE = _ts.Language(_tstype.language_tsx())
    _HAS_TS = True
except Exception:
    _HAS_TS = False

# Web / Node APIs that always return Promises
_KNOWN_ASYNC_APIS: frozenset[str] = frozenset(
    {
        "fetch",
        "axios",
        "got",
        "superagent",
        "request",
        "readFile",
        "writeFile",
        "readdir",
        "stat",
        "mkdir",
        "unlink",
        "rename",
        "copyFile",
        "setTimeout",
        "setInterval",
        "connect",
        "disconnect",
        "query",
        "execute",
        "transaction",
        "save",
        "find",
        "findOne",
        "findAll",
        "create",
        "update",
        "delete",
        "destroy",
        "upsert",
        "count",
        "bulkCreate",
        "bulkUpdate",
        "bulkDelete",
    }
)

# Method names (on any object) that always return Promises
_KNOWN_ASYNC_METHODS: frozenset[str] = frozenset(
    {
        "json",
        "text",
        "blob",
        "arrayBuffer",
        "formData",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "request",
        "send",
        "end",
        "exec",
        "query",
        "connect",
        "disconnect",
        "close",
        "open",
        "read",
        "write",
        "flush",
        "commit",
        "rollback",
        "save",
        "load",
        "fetch",
        "refresh",
        "sync",
        "upload",
        "download",
        "emit",
        "publish",
        "subscribe",
    }
)

# Array methods whose return type is T | undefined
_NULLABLE_ARRAY_METHODS: frozenset[str] = frozenset(
    {"find", "shift", "pop", "at"}
)


def _iter_ts_files(root: Path) -> Iterator[Path]:
    for ext in ("*.ts", "*.tsx"):
        yield from root.rglob(ext)


def _find_all(node, ntype: str) -> list:
    results = []
    if node.type == ntype:
        results.append(node)
    for child in node.children:
        results.extend(_find_all(child, ntype))
    return results


def _is_async_node(node) -> bool:
    """Return True if node (function/arrow/method) has an `async` keyword child."""
    return any(c.type == "async" for c in node.children)


def _callee_name(call_node) -> str | None:
    """Extract the bare function/method name from a call_expression."""
    callee = call_node.child_by_field_name("function")
    if callee is None:
        return None
    if callee.type == "identifier":
        return callee.text.decode()
    if callee.type == "member_expression":
        prop = callee.child_by_field_name("property")
        if prop is not None:
            return prop.text.decode()
    return None


def _callee_root_name(call_node) -> str | None:
    """
    For `axios.get(...)` returns 'axios'; for `fetch(...)` returns 'fetch'.
    Used to match against _KNOWN_ASYNC_APIS.
    """
    callee = call_node.child_by_field_name("function")
    if callee is None:
        return None
    if callee.type == "identifier":
        return callee.text.decode()
    if callee.type == "member_expression":
        obj = callee.child_by_field_name("object")
        if obj is not None:
            return obj.text.decode()
    return None


def _is_awaited(node) -> bool:
    """Walk up the parent chain to check if node is inside an await_expression or return statement."""
    p = node.parent
    while p is not None:
        if p.type == "await_expression":
            return True
        # `return asyncFn()` — propagates the Promise to the caller; not a bug
        if p.type == "return_statement":
            return True
        # Stop at function/arrow/method boundaries
        if p.type in (
            "function_declaration",
            "function",
            "arrow_function",
            "method_definition",
        ):
            break
        p = p.parent
    return False


def _collect_async_func_names(root_node) -> set[str]:
    """
    First-pass: collect names of all async functions declared in this file.
    Covers:
      async function foo() { ... }
      const foo = async () => { ... }
      class X { async foo() { ... } }
    """
    names: set[str] = set()

    def walk(node):
        if node.type == "function_declaration" and _is_async_node(node):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                names.add(name_node.text.decode())

        elif node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            if val is not None and val.type == "arrow_function" and _is_async_node(val):
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    names.add(name_node.text.decode())

        elif node.type == "method_definition" and _is_async_node(node):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                names.add(name_node.text.decode())

        for child in node.children:
            walk(child)

    walk(root_node)
    return names


def _scan_missing_await(
    path: str,
    root_node,
    async_names: set[str],
) -> list[tuple[int, str, str]]:
    """
    Return (line, call_text, message) for every unawaited async call inside
    an async function body.
    """
    results = []

    def walk_async_body(node):
        """Walk inside an async function, flagging unawaited calls."""
        if node.type == "call_expression":
            if not _is_awaited(node):
                callee_root = _callee_root_name(node)
                callee_method = _callee_name(node)
                is_async = (
                    (callee_root in _KNOWN_ASYNC_APIS)
                    or (callee_method in _KNOWN_ASYNC_METHODS and callee_root != callee_method)
                    or (callee_root in async_names)
                    or (callee_method in async_names)
                )
                if is_async:
                    results.append(
                        (
                            node.start_point[0] + 1,
                            node.text.decode()[:60],
                            "async call without await — Promise never resolved",
                        )
                    )
            # Don't descend into nested function definitions (they reset async context)
            for child in node.children:
                walk_async_body(child)
            return

        # Stop descending into nested function definitions
        if node.type in (
            "function_declaration",
            "function",
            "arrow_function",
            "method_definition",
        ):
            # Only continue if this nested function is also async
            if _is_async_node(node):
                for child in node.children:
                    walk_async_body(child)
            return

        for child in node.children:
            walk_async_body(child)

    def walk(node):
        is_async_func = (
            node.type in ("function_declaration", "arrow_function", "method_definition")
            and _is_async_node(node)
        )
        if is_async_func:
            body = node.child_by_field_name("body")
            if body is not None:
                walk_async_body(body)
        else:
            for child in node.children:
                walk(child)

    walk(root_node)
    return results


def _scan_optional_dereference(root_node) -> list[tuple[int, str, str]]:
    """
    Find `.find()`, `.shift()`, `.pop()` results used in direct member access
    without a null guard or optional chaining.

    Pattern:
      const x = arr.find(...);
      x.prop   ← flagged (x may be undefined)
    """
    results = []

    # Collect variable names assigned from nullable array methods
    nullable_vars: set[str] = set()

    def collect_nullable(node):
        if node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            if val is not None and val.type == "call_expression":
                method = _callee_name(val)
                if method in _NULLABLE_ARRAY_METHODS:
                    name_node = node.child_by_field_name("name")
                    if name_node is not None and name_node.type == "identifier":
                        nullable_vars.add(name_node.text.decode())
        for child in node.children:
            collect_nullable(child)

    collect_nullable(root_node)
    if not nullable_vars:
        return []

    def find_dereferences(node):
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            # optional chaining (?.) is safe — skip
            optional = any(c.type == "optional_chain" for c in node.children)
            if (
                not optional
                and obj is not None
                and obj.type == "identifier"
                and obj.text.decode() in nullable_vars
            ):
                # Check parent — if it's a null check (if/ternary/&&), skip
                p = node.parent
                if p is not None and p.type not in (
                    "if_statement",
                    "ternary_expression",
                    "binary_expression",
                ):
                    prop = node.child_by_field_name("property")
                    prop_name = prop.text.decode() if prop else "?"
                    var_name = obj.text.decode()
                    results.append(
                        (
                            node.start_point[0] + 1,
                            f"{var_name}.{prop_name}",
                            f"'{var_name}' assigned from array method returning T|undefined — dereference without guard",
                        )
                    )
        for child in node.children:
            find_dereferences(child)

    find_dereferences(root_node)
    return results


def _scan_empty_catch(root_node) -> list[tuple[int, str, str]]:
    """Find catch clauses whose body is empty or contains only comments."""
    results = []

    def walk(node):
        if node.type == "catch_clause":
            body = node.child_by_field_name("body")
            if body is not None:
                # Empty or comment-only
                named_stmts = [
                    c for c in body.children if c.is_named and c.type != "comment"
                ]
                if not named_stmts:
                    results.append(
                        (
                            node.start_point[0] + 1,
                            "catch",
                            "empty catch block — exception silently swallowed",
                        )
                    )
        for child in node.children:
            walk(child)

    walk(root_node)
    return results


def check_ts_file(path: str) -> list:
    """
    Run all TypeScript failure mode checks against one file.
    Returns list of Violation-compatible dicts (avoids circular import).
    """
    try:
        from .encoder import Violation
    except ImportError:
        from encoder import Violation  # type: ignore[no-redef]

    p = Path(path)
    try:
        src = p.read_bytes()
    except OSError:
        return []

    lang = _TSX_LANGUAGE if path.endswith(".tsx") else _TS_LANGUAGE
    parser = _ts.Parser(lang)
    tree = parser.parse(src)
    root = tree.root_node

    async_names = _collect_async_func_names(root)

    violations: list[Violation] = []

    for line, call, msg in _scan_missing_await(path, root, async_names):
        violations.append(
            Violation(
                file=path,
                line=line,
                call=call,
                missing=[msg],
                context="missing_await",
            )
        )

    for line, call, msg in _scan_optional_dereference(root):
        violations.append(
            Violation(
                file=path,
                line=line,
                call=call,
                missing=[msg],
                context="optional_dereference",
            )
        )

    for line, call, msg in _scan_empty_catch(root):
        violations.append(
            Violation(
                file=path,
                line=line,
                call=call,
                missing=[msg],
                context="empty_catch",
            )
        )

    return violations


def check_ts_files(root: Path) -> list:
    """Scan all .ts/.tsx files under root. Returns list of Violation objects."""
    if not _HAS_TS:
        return []
    violations = []
    for path in _iter_ts_files(root):
        violations.extend(check_ts_file(str(path)))
    return violations
