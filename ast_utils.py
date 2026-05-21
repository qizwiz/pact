"""
AST utilities for pact.

find_enclosing_function(filepath, line) — given a file and line number,
return the name of the innermost function/method that contains that line.

For methods, returns "ClassName.method_name".
Returns None if the line is not inside any function (module-level code).

Supports Python (via stdlib ast) and TypeScript/TSX (via tree-sitter).
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

_TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts"}

# tree-sitter function/method node types for TypeScript
_TS_FN_TYPES = frozenset(
    {
        "function_declaration",
        "function",
        "method_definition",
        "arrow_function",
        "generator_function_declaration",
    }
)


def find_enclosing_function(filepath: str | Path, line: int) -> str | None:
    """
    Return the name of the innermost function or method enclosing *line*.

    Returns ``"ClassName.method_name"`` for methods, bare ``"func_name"``
    for module-level functions, and ``None`` if the line is at module scope.
    """
    chain = find_enclosing_function_chain(filepath, line)
    return chain[-1] if chain else None


def find_enclosing_function_chain(filepath: str | Path, line: int) -> list[str]:
    """
    Return all enclosing function names from outermost to innermost.

    Dispatches to Python (stdlib ast) or TypeScript (tree-sitter) based on
    file extension.  1-based line numbers throughout.
    """
    path = Path(filepath).resolve()
    if path.suffix in _TS_SUFFIXES:
        return _ts_chain(path, line)
    return _py_chain(path, line)


# ── Python ────────────────────────────────────────────────────────────────────


def _py_chain(path: Path, line: int) -> list[str]:
    tree = _parse_py_cached(path)
    if tree is None:
        return []

    enclosing: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None)
            if end is not None and node.lineno <= line <= end:
                enclosing.append(node)

    if not enclosing:
        return []

    enclosing.sort(key=lambda n: n.lineno)

    result = []
    for fn in enclosing:
        parent = getattr(fn, "_parent", None)
        if isinstance(parent, ast.ClassDef):
            result.append(f"{parent.name}.{fn.name}")
        else:
            result.append(fn.name)
    return result


@lru_cache(maxsize=256)
def _parse_py_cached(path: Path) -> ast.Module | None:
    """Parse and cache a Python AST, attaching _parent refs to every node."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError, ValueError):
        return None
    _attach_parents(tree)
    return tree


def _attach_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]


# ── TypeScript ─────────────────────────────────────────────────────────────────


def _ts_chain(path: Path, line: int) -> list[str]:
    """Return enclosing function chain for a TypeScript/TSX file (1-based line)."""
    tree = _parse_ts_cached(path)
    if tree is None:
        return []

    row = line - 1  # tree-sitter uses 0-based rows
    enclosing = []
    _ts_collect(tree.root_node, None, None, row, enclosing)
    enclosing.sort(key=lambda x: x[0])  # sort by start row (outermost first)
    return [name for _, name in enclosing]


def _ts_fn_name(node, parent, grandparent) -> str | None:
    """Extract a display name from a tree-sitter function/method node."""
    # method_definition / function_declaration: first identifier/property_identifier is the name
    for child in node.children:
        if child.type in ("identifier", "property_identifier"):
            raw = child.text.decode("utf-8", errors="replace")
            # Qualify with class name if parent chain is class_body → class_declaration
            if parent is not None and parent.type == "class_body":
                if grandparent is not None and grandparent.type in (
                    "class_declaration",
                    "class",
                ):
                    for c in grandparent.children:
                        if c.type == "type_identifier":
                            cls = c.text.decode("utf-8", errors="replace")
                            return f"{cls}.{raw}"
            return raw

    # arrow_function or anonymous function expression: variable declarator holds the name
    if parent is not None and parent.type == "variable_declarator":
        for c in parent.children:
            if c.type == "identifier":
                return c.text.decode("utf-8", errors="replace")

    return None


def _ts_collect(node, parent, grandparent, row: int, result: list) -> None:
    """Walk tree-sitter tree, collecting function nodes that span *row*."""
    if node.type in _TS_FN_TYPES:
        if node.start_point[0] <= row <= node.end_point[0]:
            name = _ts_fn_name(node, parent, grandparent)
            if name:
                result.append((node.start_point[0], name))

    for child in node.children:
        _ts_collect(child, node, parent, row, result)


@lru_cache(maxsize=256)
def _parse_ts_cached(path: Path):
    """Parse and cache a TypeScript/TSX file with tree-sitter."""
    try:
        import tree_sitter_typescript as tsts
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        source = path.read_bytes()
        if path.suffix == ".tsx":
            lang = Language(tsts.language_tsx())
        else:
            lang = Language(tsts.language_typescript())
        parser = Parser(lang)
        return parser.parse(source)
    except (OSError, Exception) as _ts_import_err:
    import warnings
    warnings.warn(
        f"tree-sitter or its TypeScript grammar is not available ({_ts_import_err!r}); "
        "TypeScript function-scope detection will be skipped silently.",
        ImportWarning,
        stacklevel=2,
    )
        return None
