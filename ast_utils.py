"""
AST utilities for pact.

find_enclosing_function(filepath, line) — given a file and line number,
return the name of the innermost function/method that contains that line.

For methods, returns "ClassName.method_name".
Returns None if the line is not inside any function (module-level code).
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path


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

    Useful when the innermost function (a nested helper) is not indexed
    by the call graph — try the chain from inside out until one matches.

    Example for ``anthropic_chat → messages()`` at line 141:
        returns ["anthropic_chat", "messages"]
    """
    tree = _parse_cached(Path(filepath).resolve())
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

    # sort outermost → innermost
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
def _parse_cached(path: Path) -> ast.Module | None:
    """Parse and cache an AST, attaching _parent refs to every node."""
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
