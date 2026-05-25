"""
TypeScript / TSX / JavaScript constraint checker (tree-sitter backed).

Failure modes:
  missing_await              — async call without await inside an async function
  optional_dereference       — .find()/.shift()/.pop() result used without null guard
  empty_catch                — catch (e) {} — silent error suppression
  llm_response_unguarded     — choices[0] accessed without optional chaining (?.[0])
  unvalidated_lookup_chain   — .get(key) result dereferenced without null guard
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

try:
    import tree_sitter as _ts
    import tree_sitter_typescript as _tstype

    _TS_LANGUAGE = _ts.Language(_tstype.language_typescript())
    _TSX_LANGUAGE = _ts.Language(_tstype.language_tsx())
    _HAS_TS = True
except Exception as _ts_exc:
    import warnings

    warnings.warn(
        f"tree-sitter TypeScript unavailable — TS/TSX checks are disabled: {_ts_exc}",
        RuntimeWarning,
        stacklevel=2,
    )
    _HAS_TS = False

_HAS_JS = False
try:
    import tree_sitter_javascript as _tsjs

    _JS_LANGUAGE = _ts.Language(_tsjs.language())
    _JSX_LANGUAGE = _JS_LANGUAGE  # tree-sitter-javascript handles JSX natively
    _HAS_JS = True
except Exception as _js_exc:
    import warnings

    warnings.warn(
        f"tree-sitter JavaScript unavailable — JS/JSX checks are disabled: {_js_exc}",
        RuntimeWarning,
        stacklevel=2,
    )

# Directories to skip (mirrors extractor._SKIP_DIRS)
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".github",
        ".venv",
        "venv",
        "node_modules",
        "migrations",
        ".mypy_cache",
        ".uv-cache",
        ".ruff_cache",
        "vendor",
        "_vendor",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "out",
    }
)

# Web / Node APIs that always return Promises
_KNOWN_ASYNC_APIS: frozenset[str] = frozenset(
    {
        # HTTP client libraries — very specific, rarely used as variable names
        "fetch",
        "axios",
        "got",
        "superagent",
        # Node.js fs functions (destructured import form: `const { readFile } = fs`)
        "readFile",
        "writeFile",
        "readdir",
        "stat",
        "mkdir",
        "unlink",
        "rename",
        "copyFile",
    }
)
# NOTE: removed from _KNOWN_ASYNC_APIS:
#   setTimeout/setInterval — return timer IDs, NOT Promises
#   request — too ambiguous (Express req object, common param name)
#   query/execute/transaction/save/find/create/update/delete/etc. — too generic;
#     these match as ORM *methods* via _KNOWN_ASYNC_METHODS when called on objects,
#     but as root names they produce false positives (e.g. query.trim())

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
_NULLABLE_ARRAY_METHODS: frozenset[str] = frozenset({"find", "shift", "pop", "at"})


def _iter_ts_files(root: Path) -> Iterator[Path]:
    """Yield TS/TSX/JS/JSX files under root, skipping vendor/generated dirs."""
    ts_exts = ("*.ts", "*.tsx") if _HAS_TS else ()
    js_exts = ("*.js", "*.jsx", "*.mjs", "*.cjs") if _HAS_JS else ()
    for ext in (*ts_exts, *js_exts):
        for path in root.rglob(ext):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            yield path


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
                    or (
                        callee_method in _KNOWN_ASYNC_METHODS
                        and callee_root != callee_method
                    )
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
        is_async_func = node.type in (
            "function_declaration",
            "arrow_function",
            "method_definition",
        ) and _is_async_node(node)
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


def _scan_unvalidated_map_get(root_node) -> list[tuple[int, str, str]]:
    """Detect .get(key) result immediately dereferenced without optional chaining.

    Pattern flagged:
        myMap.get(key).property     ← TypeError if key absent (returns undefined)
        myMap.get(key)[index]       ← same

    Safe patterns (skipped):
        myMap.get(key)?.property    ← optional chaining — explicitly guarded
    """
    results = []

    def walk(node):
        # member_expression: obj.prop (or obj?.prop with optional_chain child)
        if node.type in ("member_expression", "subscript_expression"):
            has_optional = any(c.type == "optional_chain" for c in node.children)
            if not has_optional:
                obj = node.child_by_field_name("object")
                if obj is not None and obj.type == "call_expression":
                    func = obj.child_by_field_name("function")
                    if func is not None and func.type == "member_expression":
                        prop_nodes = [
                            c for c in func.children if c.type == "property_identifier"
                        ]
                        if prop_nodes and prop_nodes[0].text == b"get":
                            call_text = obj.text.decode("utf-8", errors="replace")[:80]
                            results.append(
                                (
                                    node.start_point[0] + 1,
                                    call_text,
                                    f"'{call_text}' dereferenced without null guard — "
                                    ".get() returns undefined if key is absent; "
                                    "use optional chaining (?.) or check for undefined first",
                                )
                            )
                            return  # don't descend into flagged node
        for child in node.children:
            walk(child)

    walk(root_node)
    return results


_CHOICES_RE = re.compile(rb"\bchoices\b")


def _scan_llm_unguarded(src: bytes, root_node) -> list[tuple[int, str, str]]:
    """
    Detect response.choices[0] (non-optional subscript) in JS/TS files.

    Safe patterns skipped:
      response.choices?.[0]   — optional chaining
      response.choices?.length — guarded by caller
    Flagged patterns:
      response.choices[0]
      response.choices[0].message
      response.choices[0].message.content
    """
    if not _CHOICES_RE.search(src):
        return []

    results = []

    def walk(node):
        if node.type == "subscript_expression":
            children = node.children
            # Optional chaining: choices?.[0] has an optional_chain child token
            if any(c.type == "optional_chain" for c in children):
                for child in children:
                    walk(child)
                return

            # Index must be the literal number 0
            index_nodes = [c for c in children if c.type == "number"]
            if not index_nodes or index_nodes[0].text != b"0":
                for child in children:
                    walk(child)
                return

            # Object must be member_expression ending in .choices
            obj = children[0]
            if obj.type != "member_expression":
                for child in children:
                    walk(child)
                return
            prop_nodes = [c for c in obj.children if c.type == "property_identifier"]
            if not prop_nodes or prop_nodes[0].text != b"choices":
                for child in children:
                    walk(child)
                return

            call_text = src[node.start_byte : node.end_byte].decode(
                "utf-8", errors="replace"
            )[:120]
            results.append(
                (
                    node.start_point[0] + 1,
                    call_text,
                    (
                        f"'{call_text[:60]}' accessed without optional chaining — "
                        "use '?.[0]' to guard against empty choices arrays"
                    ),
                )
            )
            # Don't descend into the flagged subscript
            return

        for child in node.children:
            walk(child)

    walk(root_node)
    return results


def _get_lang(path: str):
    """Return (language, parser) for path, or (None, None) if unsupported."""
    ext = Path(path).suffix.lower()
    if _HAS_TS and ext in (".ts", ".mts"):
        return _TS_LANGUAGE, _ts.Parser(_TS_LANGUAGE)
    if _HAS_TS and ext in (".tsx",):
        return _TSX_LANGUAGE, _ts.Parser(_TSX_LANGUAGE)
    if _HAS_JS and ext in (".js", ".mjs", ".cjs"):
        return _JS_LANGUAGE, _ts.Parser(_JS_LANGUAGE)
    if _HAS_JS and ext in (".jsx",):
        return _JSX_LANGUAGE, _ts.Parser(_JSX_LANGUAGE)
    return None, None


def check_ts_file(path: str) -> list:
    """
    Run all JS/TS failure mode checks against one file.
    Returns list of Violation-compatible objects.
    """
    try:
        from .encoder import Violation
    except ImportError:
        from encoder import Violation  # type: ignore[no-redef]

    lang, parser = _get_lang(path)
    if parser is None:
        return []

    p = Path(path)
    try:
        src = p.read_bytes()
    except OSError as exc:
        import warnings

        warnings.warn(
            f"ts_checker: cannot read {p} ({exc}); file will not be scanned",
            RuntimeWarning,
            stacklevel=2,
        )
        return []

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

    for line, call, msg in _scan_llm_unguarded(src, root):
        violations.append(
            Violation(
                file=path,
                line=line,
                call=call,
                missing=[msg],
                context="llm_response_unguarded",
            )
        )

    for line, call, msg in _scan_unvalidated_map_get(root):
        violations.append(
            Violation(
                file=path,
                line=line,
                call=call,
                missing=[msg],
                context="unvalidated_lookup_chain",
            )
        )

    return violations


def check_ts_files(root: Path) -> list:
    """Scan all TS/TSX/JS/JSX files under root. Returns list of Violation objects."""
    if not _HAS_TS and not _HAS_JS:
        import warnings

        warnings.warn(
            "ts_checker: tree-sitter TypeScript/JavaScript wheels not installed; "
            "TS/JS structural checks are skipped — pip install tree-sitter-typescript "
            "tree-sitter-javascript to enable",
            RuntimeWarning,
            stacklevel=2,
        )
        return []
    violations = []
    for path in _iter_ts_files(root):
        violations.extend(check_ts_file(str(path)))
    return violations
