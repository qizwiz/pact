"""Constraint graph analysis: Z3 expression DAGs + NetworkX topology.

Key insight: Z3's expression trees are DAGs. expr.children() gives edges,
expr.get_id() gives node IDs. Graph analysis on those DAGs ranks constraints
by structural importance. A cut vertex with high betweenness is a load-bearing
invariant — remove it and the violation proof collapses.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import networkx as nx
import z3

# ---------------------------------------------------------------------------
# Crosshair runner
# ---------------------------------------------------------------------------


def _is_pure_function(source_file: str, fn_qualname: str) -> bool:
    """Heuristic: True if the function body contains no I/O or subprocess calls."""
    try:
        source = Path(source_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return False
    fn_node = _find_function_node(tree, fn_qualname)
    if fn_node is None:
        return False
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call):
            func = node.func
            # Flag any open/subprocess/os.path calls
            if isinstance(func, ast.Attribute) and func.attr in (
                "open",
                "run",
                "call",
                "popen",
                "read_text",
                "write_text",
                "read",
                "write",
                "listdir",
                "walk",
            ):
                return False
            if isinstance(func, ast.Name) and func.id in ("open", "print"):
                return False
    return True


def run_crosshair(
    fn_qualname: str, source_file: str, timeout: float = 10.0
) -> list[dict]:
    """Run Crosshair on *fn_qualname*, using cover mode for pure functions.

    - Pure functions (no I/O): ``crosshair cover`` — generates input corpus
      and detects exception-raising paths.
    - Impure functions: ``crosshair check`` — requires pre:/post: contracts;
      returns empty if none are present.

    Returns a list of violation dicts::

        {"file": str, "line": int, "message": str, "type": "crosshair_violation"}
        {"file": str, "line": int, "message": str, "type": "crosshair_cover",
         "inputs": [...], "raises": str}   # for exception-raising cover paths
    """
    pure = _is_pure_function(source_file, fn_qualname)

    # Derive module.qualname from file path for crosshair
    module = Path(source_file).stem
    # Try to find the package prefix
    parts = Path(source_file).parts
    try:
        pkg_idx = list(parts).index("pact-standalone") + 1
        pkg_parts = [p for p in parts[pkg_idx:] if p != "__pycache__"]
        module_path = ".".join(p.replace(".py", "") for p in pkg_parts)
        target = (
            f"{module_path}.{fn_qualname}"
            if "." not in module_path.split(".")[-1]
            else f"{module_path}"
        )
        target = f"{module_path}.{fn_qualname}"
    except (ValueError, IndexError):
        target = f"{module}.{fn_qualname}"

    if pure:
        cmd = [
            sys.executable,
            "-m",
            "crosshair",
            "cover",
            target,
            "--example_output_format",
            "pytest",
            "--per_condition_timeout",
            str(timeout),
            "--max_uninteresting_iterations",
            "10",
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "crosshair",
            "check",
            target,
            "--per_condition_timeout",
            str(timeout),
        ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
            check=False,
        )
        raw = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        warnings.warn(
            f"crosshair timed out on {fn_qualname}", RuntimeWarning, stacklevel=2
        )
        return []
    except FileNotFoundError:
        warnings.warn(
            "crosshair not found — install with: pip install crosshair-tool",
            RuntimeWarning,
            stacklevel=2,
        )
        return []

    violations: list[dict] = []

    if pure:
        # Parse pytest-format cover output; execute each case to find raisers
        import importlib

        fn = None
        try:
            mod_name, attr = target.rsplit(".", 1)
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, attr, None)
        except (ImportError, AttributeError):
            pass

        if fn is not None:
            # Extract assert lines: assert f(args) == value
            _assert_re = re.compile(r"assert \w+\((.+)\) == ")
            for line in raw.splitlines():
                m = _assert_re.match(line.strip())
                if not m:
                    continue
                args_src = m.group(1)
                try:
                    args = eval(f"({args_src},)", {})  # noqa: S307
                    fn(*args)
                except Exception as exc:
                    violations.append(
                        {
                            "file": source_file,
                            "line": 0,
                            "message": f"{type(exc).__name__}: {exc} — input: {args_src[:80]}",
                            "type": "crosshair_cover",
                        }
                    )
    else:
        # check mode: parse error lines
        err_re = re.compile(r"^(.+?):(\d+):\s*error:\s*(.+)$")
        for line in raw.splitlines():
            m = err_re.match(line.strip())
            if m:
                violations.append(
                    {
                        "file": m.group(1),
                        "line": int(m.group(2)),
                        "message": m.group(3),
                        "type": "crosshair_violation",
                    }
                )

    return violations


# ---------------------------------------------------------------------------
# Z3 DAG builder
# ---------------------------------------------------------------------------

_MAX_DEPTH = 30


def _walk_expr(expr: z3.ExprRef, G: nx.DiGraph, visited: set, depth: int = 0) -> None:
    """Recursively walk a Z3 expression, adding nodes and edges to *G*."""
    if depth > _MAX_DEPTH:
        return
    node_id = expr.get_id()
    if node_id in visited:
        return
    visited.add(node_id)

    # Determine label
    try:
        label = expr.decl().name()
    except Exception:
        try:
            label = str(expr.sort())
        except Exception:
            label = "?"

    G.add_node(node_id, label=label)

    for child in expr.children():
        child_id = child.get_id()
        if child_id not in visited:
            _walk_expr(child, G, visited, depth + 1)
        else:
            # Ensure node exists even if already visited
            if child_id not in G:
                try:
                    child_label = child.decl().name()
                except Exception:
                    child_label = "?"
                G.add_node(child_id, label=child_label)
        G.add_edge(node_id, child_id)


def build_constraint_dag(assertions: list) -> nx.DiGraph:
    """Build a NetworkX DiGraph from a list of Z3 ExprRef assertions.

    Nodes: ``expr.get_id()`` with attr ``label = expr.decl().name()``
    Edges: parent → child (expr → sub-expression)

    Args:
        assertions: list of z3.ExprRef objects.

    Returns:
        A directed graph encoding the constraint structure.
    """
    G: nx.DiGraph = nx.DiGraph()
    visited: set = set()
    for expr in assertions:
        if isinstance(expr, z3.ExprRef):
            _walk_expr(expr, G, visited, depth=0)
    return G


# ---------------------------------------------------------------------------
# DAG analysis
# ---------------------------------------------------------------------------


def analyze_constraint_dag(G: nx.DiGraph) -> dict:
    """Analyse the structural topology of a constraint DAG.

    Args:
        G: directed graph from :func:`build_constraint_dag`.

    Returns:
        dict with keys:
        - n_nodes, n_edges
        - cut_vertices: list of {label, betweenness} sorted by betweenness desc, top 10
        - beta1: int (edges - nodes + connected_components)  [first Betti number]
        - connected_components: int
        - top_betweenness: list of {label, betweenness} top 10 (all nodes, not just cut vertices)
    """
    if G.number_of_nodes() == 0:
        return {
            "n_nodes": 0,
            "n_edges": 0,
            "cut_vertices": [],
            "beta1": 0,
            "connected_components": 0,
            "top_betweenness": [],
        }

    G_u: nx.Graph = G.to_undirected()

    # Betweenness centrality over undirected graph
    btw: dict[Any, float] = nx.betweenness_centrality(G_u, normalized=True)

    # Articulation points (cut vertices)
    cv_set: set = set(nx.articulation_points(G_u))

    # Build node_id → label map
    id_to_label: dict[Any, str] = {
        nid: G.nodes[nid].get("label", str(nid)) for nid in G.nodes
    }

    cut_vertices = sorted(
        [
            {"label": id_to_label.get(nid, str(nid)), "betweenness": round(b, 4)}
            for nid, b in btw.items()
            if nid in cv_set
        ],
        key=lambda x: x["betweenness"],
        reverse=True,
    )[:10]

    top_betweenness = sorted(
        [
            {"label": id_to_label.get(nid, str(nid)), "betweenness": round(b, 4)}
            for nid, b in btw.items()
        ],
        key=lambda x: x["betweenness"],
        reverse=True,
    )[:10]

    n_nodes = G_u.number_of_nodes()
    n_edges = G_u.number_of_edges()
    cc = nx.number_connected_components(G_u)
    beta1 = max(0, n_edges - n_nodes + cc)

    return {
        "n_nodes": n_nodes,
        "n_edges": n_edges,
        "cut_vertices": cut_vertices,
        "beta1": beta1,
        "connected_components": cc,
        "top_betweenness": top_betweenness,
    }


# ---------------------------------------------------------------------------
# Known contract patterns
# ---------------------------------------------------------------------------


def contract_dag_for_pattern(
    pattern: str, **kwargs: Any
) -> tuple[nx.DiGraph, z3.Solver]:
    """Encode a known contract violation pattern as Z3 constraints and build its DAG.

    Supported patterns:

    ``json_loads_unguarded``
        Models the invariant: json.loads must be guarded; a raises=True,
        guarded=False state is a contract violation.

    ``subprocess_unchecked``
        Models the invariant: a subprocess with exit_code!=0 and checked=False
        is a violation.

    ``content_index_unguarded``
        Models accessing index 0 on a list of length 0.

    Args:
        pattern: one of the above strings.
        **kwargs: ignored (reserved for future parameterization).

    Returns:
        A (DiGraph, Solver) tuple. Caller can call ``s.check()`` to test
        satisfiability (sat = violation reachable).
    """
    s = z3.Solver()

    if pattern == "json_loads_unguarded":
        text = z3.String("text")
        valid = z3.Function("valid_json", z3.StringSort(), z3.BoolSort())
        guarded = z3.Bool("guarded")
        raises = z3.Bool("raises")
        s.add(z3.Implies(z3.Not(valid(text)), raises))
        s.add(z3.Implies(valid(text), z3.Not(raises)))
        s.add(z3.And(raises, z3.Not(guarded)))  # violation condition

    elif pattern == "subprocess_unchecked":
        exit_code = z3.Int("exit_code")
        checked = z3.Bool("checked")
        s.add(exit_code != 0)
        s.add(z3.Not(checked))

    elif pattern == "content_index_unguarded":
        length = z3.Int("length")
        index = z3.Int("index")
        s.add(length == 0)
        s.add(index >= 0)
        s.add(index < 1)  # accessing [0] on empty list

    else:
        raise ValueError(
            f"Unknown pattern: {pattern!r}. "
            "Valid patterns: json_loads_unguarded, subprocess_unchecked, "
            "content_index_unguarded"
        )

    G = build_constraint_dag(s.assertions())
    return G, s


# ---------------------------------------------------------------------------
# Function-specific AST extraction
# ---------------------------------------------------------------------------

_JSON_GUARD_NAMES = {"JSONDecodeError", "ValueError", "Exception"}


def _exc_names(exc_node: ast.expr) -> set[str]:
    """Collect exception names from a handler type node (Name, Attribute, Tuple)."""
    if isinstance(exc_node, ast.Name):
        return {exc_node.id}
    if isinstance(exc_node, ast.Attribute):
        return {exc_node.attr}
    if isinstance(exc_node, ast.Tuple):
        names: set[str] = set()
        for elt in exc_node.elts:
            names |= _exc_names(elt)
        return names
    return set()


def _find_function_node(
    tree: ast.AST, fn_qualname: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find the AST node for *fn_qualname* (supports 'Class.method' dotted names)."""
    parts = fn_qualname.split(".", 1)
    name = parts[0]
    rest = parts[1] if len(parts) > 1 else None

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                if rest is None:
                    return node
                return _find_function_node(node, rest)
        if isinstance(node, ast.ClassDef) and node.name == name and rest:
            return _find_function_node(node, rest)
    return None


def _is_guarded_in_fn(
    call_node: ast.Call, fn_node: ast.AST, guard_names: set[str]
) -> bool:
    """True if *call_node* is inside a try/except that catches any of *guard_names*."""
    for parent in ast.walk(fn_node):
        if not isinstance(parent, ast.Try):
            continue
        # Check if call_node is in the try body (not the handlers)
        in_body = any(call_node is n for n in ast.walk(ast.Module(body=parent.body, type_ignores=[])))  # type: ignore[arg-type]
        if not in_body:
            continue
        for handler in parent.handlers:
            if handler.type is None:
                return True  # bare except catches everything
            if _exc_names(handler.type) & guard_names:
                return True
    return False


def _returncode_checked_in_scope(call_node: ast.Call, scope: ast.AST) -> bool:
    """True if the result of *call_node* is assigned and .returncode is accessed."""
    # Look for: var = subprocess.run(...) then var.returncode
    # Walk the scope looking for assignments where the RHS is our call node.
    assigned_names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            if any(n is call_node for n in ast.walk(node.value)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if any(n is call_node for n in ast.walk(node.value)):
                if isinstance(node.target, ast.Name):
                    assigned_names.add(node.target.id)

    if not assigned_names:
        return False

    # Check if .returncode is accessed on any of those names
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "returncode"
            and isinstance(node.value, ast.Name)
            and node.value.id in assigned_names
        ):
            return True
    return False


def _optional_param_names(fn_node: ast.AST) -> set[str]:
    """Return parameter names annotated Optional[X] or X | None."""
    optional: set[str] = set()
    if not isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return optional
    for arg in fn_node.args.args + fn_node.args.posonlyargs + fn_node.args.kwonlyargs:
        if arg.annotation is None:
            continue
        ann = arg.annotation
        # Optional[X] → Subscript(Name("Optional"), ...)
        if (
            isinstance(ann, ast.Subscript)
            and isinstance(ann.value, ast.Name)
            and ann.value.id == "Optional"
        ):
            optional.add(arg.arg)
        # X | None → BinOp(X, BitOr, Constant(None))
        elif isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            if (isinstance(ann.right, ast.Constant) and ann.right.value is None) or (
                isinstance(ann.left, ast.Constant) and ann.left.value is None
            ):
                optional.add(arg.arg)
        # None | X same
    return optional


def _is_none_guarded(name: str, access_node: ast.AST, scope: ast.AST) -> bool:
    """True if *name* is guaranteed non-None at *access_node* in *scope*.

    Handles:
    - ``if x is not None: ... x.attr`` / ``if x: ... x.attr``
    - ``if x is None: return/raise`` before the access (early-exit guard)
    - ``x and x.attr`` short-circuit (the BoolOp itself)
    - ``if x is None: x = default`` reassignment-after-check
    """
    for parent in ast.walk(scope):
        # ── if-block guards ──────────────────────────────────────────────────
        if isinstance(parent, ast.If):
            test_src = ast.unparse(parent.test)
            guarded_positive = (
                f"{name} is not None" in test_src
                or test_src.strip() == name
                or test_src.startswith(f"{name} ")
                or f"isinstance({name}," in test_src
            )
            if guarded_positive:
                in_body = any(
                    access_node is n
                    for n in ast.walk(
                        ast.Module(body=parent.body, type_ignores=[])  # type: ignore[arg-type]
                    )
                )
                if in_body:
                    return True

            # if x is None → access in else branch
            if f"{name} is None" in test_src:
                in_else = any(
                    access_node is n
                    for n in ast.walk(
                        ast.Module(body=parent.orelse, type_ignores=[])  # type: ignore[arg-type]
                    )
                )
                if in_else:
                    return True

                # Early-exit: if x is None: return/raise — access is after the if
                body_exits = any(
                    isinstance(n, (ast.Return, ast.Raise))
                    for n in ast.walk(
                        ast.Module(body=parent.body, type_ignores=[])  # type: ignore[arg-type]
                    )
                )
                if body_exits and access_node is not None:
                    return (
                        True  # conservative: any access after an early-exit is guarded
                    )

        # ── reassignment: if x is None: x = default ──────────────────────────
        if isinstance(parent, ast.If):
            test_src = ast.unparse(parent.test)
            if f"{name} is None" in test_src:
                # Check if body assigns to name
                for stmt in parent.body:
                    if isinstance(stmt, ast.Assign):
                        if any(
                            isinstance(t, ast.Name) and t.id == name
                            for t in stmt.targets
                        ):
                            # After reassignment the name is non-None
                            return True
                    if isinstance(stmt, ast.AugAssign):
                        if isinstance(stmt.target, ast.Name) and stmt.target.id == name:
                            return True

        # ── short-circuit: x and x.attr ──────────────────────────────────────
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.And):
            values = parent.values
            # Find if `name` appears as a truth-test before access_node
            for i, val in enumerate(values):
                if isinstance(val, ast.Name) and val.id == name:
                    # Everything after this position is guarded by the short-circuit
                    for later in values[i + 1 :]:
                        if any(access_node is n for n in ast.walk(later)):
                            return True
                # Also handle `name is not None` as a left operand
                if (
                    isinstance(val, ast.Compare)
                    and isinstance(val.left, ast.Name)
                    and val.left.id == name
                ):
                    op_srcs = [type(o).__name__ for o in val.ops]
                    if "IsNot" in op_srcs:
                        for later in values[i + 1 :]:
                            if any(access_node is n for n in ast.walk(later)):
                                return True

    return False


def _find_optional_deref_sites(fn_node: ast.AST, via: str) -> list[dict]:
    """Find Optional[T] parameter dereferences without a None guard."""
    optional_params = _optional_param_names(fn_node)
    if not optional_params:
        return []
    sites: list[dict] = []
    for node in ast.walk(fn_node):
        # attr access: x.field where x is Optional
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            name = node.value.id
            if name not in optional_params:
                continue
            if not _is_none_guarded(name, node, fn_node):
                sites.append(
                    {
                        "line": node.lineno,
                        "via": via,
                        "arg": f"{name}.{node.attr}",
                        "guarded": False,
                        "pattern": "optional_deref_unguarded",
                    }
                )
        # method call: x.method() where x is Optional
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            name = node.func.value.id
            if name not in optional_params:
                continue
            if not _is_none_guarded(name, node, fn_node):
                sites.append(
                    {
                        "line": node.lineno,
                        "via": via,
                        "arg": f"{name}.{node.func.attr}()",
                        "guarded": False,
                        "pattern": "optional_deref_unguarded",
                    }
                )
    # Deduplicate by line (attr and method call can both match the same line)
    seen: set[int] = set()
    deduped = []
    for s in sites:
        if s["line"] not in seen:
            seen.add(s["line"])
            deduped.append(s)
    return deduped


def _base_names(expr: ast.expr) -> set[str]:
    """All Name ids inside an expression — for matching guards on chained exprs."""
    return {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}


def _is_index_guarded(subscript_node: ast.Subscript, scope: ast.AST) -> bool:
    """True if subscript_node is inside an if-block or after a len/bool check."""
    container_src = ast.unparse(subscript_node.value)
    # The bare name being indexed (for `records[0]` → "records")
    container_name = (
        subscript_node.value.id if isinstance(subscript_node.value, ast.Name) else None
    )
    # All variable names that appear anywhere in the container expression.
    # For `list(d.keys())[0]` this is {"list", "d"} — lets us match `if d and ...`
    container_base_names = _base_names(subscript_node.value)

    # Early-exit guard: `if not x: return/continue` before the access
    if container_name:
        for parent in ast.walk(scope):
            if not isinstance(parent, ast.If):
                continue
            test = parent.test
            # if not x / if len(x) == 0 / if x is None
            test_src = ast.unparse(test)
            is_empty_check = (
                (
                    isinstance(test, ast.UnaryOp)
                    and isinstance(test.op, ast.Not)
                    and isinstance(test.operand, ast.Name)
                    and test.operand.id == container_name
                )
                or f"not {container_name}" in test_src
                or f"{container_name} is None" in test_src
                or f"len({container_name}) == 0" in test_src
            )
            if not is_empty_check:
                continue
            # Body must be a short-circuit (return/continue/raise/pass)
            body_exits = any(
                isinstance(n, (ast.Return, ast.Continue, ast.Raise, ast.Break))
                for n in ast.walk(
                    ast.Module(body=parent.body, type_ignores=[])  # type: ignore[arg-type]
                )
            )
            if body_exits:
                return True

    # Ternary guard: `x[0] if x else default` or `x[0] if len(x) > 0 else ...`
    for parent in ast.walk(scope):
        if isinstance(parent, ast.IfExp):  # ternary
            # subscript is in the 'body' (true branch) of ANY ternary
            if any(subscript_node is n for n in ast.walk(parent.body)):
                return True  # any ternary wrapping the access is a guard

    # Skip subscripts in lambda/generator parameters — always structurally safe
    for parent in ast.walk(scope):
        if isinstance(
            parent,
            (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp),
        ):
            if any(subscript_node is n for n in ast.walk(parent)):
                return True

    for parent in ast.walk(scope):
        # if container or if len(container) or if container is not None
        if isinstance(parent, ast.If):
            test_src = ast.unparse(parent.test)
            if container_src in test_src:
                # subscript is in the body of this if
                in_body = any(
                    subscript_node is n
                    for n in ast.walk(
                        ast.Module(body=parent.body, type_ignores=[])  # type: ignore[arg-type]
                    )
                )
                if in_body:
                    return True
            # `if x and x[0]` or `if x and f(x[0])` — subscript is guarded by an
            # earlier operand in the same `and` condition that tests the container
            if isinstance(parent.test, ast.BoolOp) and isinstance(
                parent.test.op, ast.And
            ):
                for i, val in enumerate(parent.test.values):
                    val_names = _base_names(val)
                    # Left operand mentions one of the container's base names
                    if val_names & container_base_names:
                        for later in parent.test.values[i + 1 :]:
                            if any(subscript_node is n for n in ast.walk(later)):
                                return True
        # try/except IndexError or similar
        if isinstance(parent, ast.Try):
            for handler in parent.handlers:
                if handler.type is None:
                    in_body = any(
                        subscript_node is n
                        for n in ast.walk(
                            ast.Module(body=parent.body, type_ignores=[])  # type: ignore[arg-type]
                        )
                    )
                    if in_body:
                        return True
                elif _exc_names(handler.type) & {"IndexError", "KeyError", "Exception"}:
                    in_body = any(
                        subscript_node is n
                        for n in ast.walk(
                            ast.Module(body=parent.body, type_ignores=[])  # type: ignore[arg-type]
                        )
                    )
                    if in_body:
                        return True
    return False


def _collect_local_callees(fn_node: ast.AST, all_fns: dict[str, ast.AST]) -> set[str]:
    """Return names of same-file functions called directly from *fn_node* (one level)."""
    callees: set[str] = set()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call):
            func = node.func
            # Direct call: foo(...)
            if isinstance(func, ast.Name) and func.id in all_fns:
                callees.add(func.id)
            # Module-qualified: self.foo(...) or obj.foo(...) — skip (cross-file)
    return callees


def extract_call_sites(source_file: str, fn_qualname: str) -> list[dict]:
    """Walk *fn_qualname*'s AST and find contract-relevant call sites.

    Returns a list of dicts::

        {"line": int, "arg": str, "guarded": bool, "pattern": str}

    Patterns detected:
    - ``json_loads_unguarded``: ``json.loads(...)`` not inside try/except JSONDecodeError
    - ``subprocess_unchecked``: ``subprocess.run(...)`` / ``subprocess.call(...)`` without ``check=True``
    - ``content_index_unguarded``: ``expr[0]`` / ``expr.content[0]`` without length guard
    """
    try:
        source = Path(source_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError) as exc:
        warnings.warn(
            f"extract_call_sites: cannot parse {source_file}: {exc}", RuntimeWarning
        )
        return []

    fn_node = _find_function_node(tree, fn_qualname)
    if fn_node is None:
        return []

    # Build index of all top-level functions in the file for interprocedural walk
    all_fns: dict[str, ast.AST] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_fns[node.name] = node

    # Scan the target + its direct same-file callees (one interprocedural hop)
    to_scan: dict[str, ast.AST] = {fn_qualname: fn_node}
    for callee_name in _collect_local_callees(fn_node, all_fns):
        if callee_name not in to_scan:
            to_scan[callee_name] = all_fns[callee_name]

    sites: list[dict] = []

    for scan_name, scan_node in to_scan.items():
        via = "" if scan_name == fn_qualname else f"{scan_name}→"
        for node in ast.walk(scan_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # json.loads(...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "loads"
                and isinstance(func.value, ast.Name)
                and func.value.id == "json"
            ):
                guarded = _is_guarded_in_fn(node, scan_node, _JSON_GUARD_NAMES)
                arg_str = ast.unparse(node.args[0]) if node.args else "?"
                sites.append(
                    {
                        "line": node.lineno,
                        "via": via,
                        "arg": arg_str,
                        "guarded": guarded,
                        "pattern": "json_loads_unguarded",
                    }
                )

            # subprocess.run / subprocess.call / subprocess.popen
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in ("run", "call", "popen")
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                check_val = None
                for kw in node.keywords:
                    if kw.arg == "check":
                        check_val = kw.value
                # Guarded if:
                #   check=True  → raises CalledProcessError on failure
                #   check=False → developer explicitly acknowledged non-zero exit
                #   .returncode accessed → developer inspects exit code manually
                check_true = (
                    isinstance(check_val, ast.Constant) and check_val.value is True
                )
                check_false_explicit = (
                    isinstance(check_val, ast.Constant) and check_val.value is False
                )
                returncode_used = _returncode_checked_in_scope(node, scan_node)
                if not check_true and not check_false_explicit and not returncode_used:
                    sites.append(
                        {
                            "line": node.lineno,
                            "via": via,
                            "arg": "...",
                            "guarded": False,
                            "pattern": "subprocess_unchecked",
                        }
                    )

        # Index [0] access without prior length/emptiness check
        for node in ast.walk(scan_node):
            if not isinstance(node, ast.Subscript):
                continue
            idx = node.slice
            if not (isinstance(idx, ast.Constant) and idx.value in (0, -1)):
                continue
            container_node = node.value
            container = ast.unparse(container_node)
            # Skip literals that can never be empty
            if isinstance(container_node, (ast.Constant, ast.Dict, ast.Set)):
                continue
            # Skip str.split() — always returns ≥1 element
            if (
                isinstance(container_node, ast.Call)
                and isinstance(container_node.func, ast.Attribute)
                and container_node.func.attr
                in ("split", "splitlines", "partition", "rpartition")
            ):
                continue
            # Skip AST node attributes guaranteed non-empty by grammar
            # (ast.Assign.targets, ast.FunctionDef.args, etc.)
            if isinstance(container_node, ast.Attribute) and container_node.attr in (
                "targets",
                "ops",
                "comparators",
            ):
                continue
            guarded = _is_index_guarded(node, scan_node)
            if not guarded:
                sites.append(
                    {
                        "line": node.lineno,
                        "via": via,
                        "arg": container,
                        "guarded": False,
                        "pattern": "content_index_unguarded",
                    }
                )

        # Optional parameter dereference without None guard
        for site in _find_optional_deref_sites(scan_node, via):
            sites.append(site)

    return sites


def contract_dag_for_function(
    source_file: str, fn_qualname: str
) -> tuple[nx.DiGraph, z3.Solver, list[dict]]:
    """Build a function-specific Z3 constraint DAG from actual AST call sites.

    Unlike :func:`contract_dag_for_pattern` which uses generic templates, this
    function extracts the real unguarded call sites from *fn_qualname*'s AST
    and encodes each one as a distinct Z3 sub-problem keyed by line number.

    Returns:
        (DAG, solver, sites) — sites is the list of extracted call sites.
        The solver's assertions() encodes only the unguarded sites found.
        If no unguarded sites exist, returns an empty graph and empty sites.
    """
    sites = extract_call_sites(source_file, fn_qualname)
    unguarded = [s for s in sites if not s["guarded"]]

    s = z3.Solver()

    for site in unguarded:
        line = site["line"]
        p = site["pattern"]

        if p == "json_loads_unguarded":
            text = z3.String(f"text_L{line}")
            valid = z3.Function(f"valid_json_L{line}", z3.StringSort(), z3.BoolSort())
            raises = z3.Bool(f"raises_L{line}")
            guarded = z3.Bool(f"guarded_L{line}")
            s.add(z3.Implies(z3.Not(valid(text)), raises))
            s.add(z3.Implies(valid(text), z3.Not(raises)))
            s.add(z3.And(raises, z3.Not(guarded)))

        elif p == "subprocess_unchecked":
            exit_code = z3.Int(f"exit_code_L{line}")
            checked = z3.Bool(f"checked_L{line}")
            s.add(exit_code != 0)
            s.add(z3.Not(checked))

        elif p == "optional_deref_unguarded":
            is_none = z3.Bool(f"is_none_L{line}")
            guarded_n = z3.Bool(f"guarded_L{line}")
            s.add(is_none)
            s.add(z3.Not(guarded_n))

    G = build_constraint_dag(list(s.assertions()))
    return G, s, sites


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def analyze_function(
    source_file: str,
    fn_qualname: str,
    pattern: str | None = None,
    timeout: float = 10.0,
) -> dict:
    """Full constraint graph analysis for a function.

    1. Runs Crosshair on the function to find contract violations.
    2. If *pattern* is provided, builds the Z3 constraint DAG, checks SAT,
       extracts model variables.
    3. Runs :func:`analyze_constraint_dag` on the DAG.

    Args:
        source_file: Absolute path to the Python source file.
        fn_qualname: Qualified function name (e.g. ``"MyClass.method"`` or
            ``"my_function"``).
        pattern: Optional contract pattern name — see
            :func:`contract_dag_for_pattern`.
        timeout: Crosshair per-condition timeout in seconds.

    Returns:
        dict with keys::

            {
                "function": fn_qualname,
                "file": source_file,
                "crosshair_violations": [...],
                "z3_sat": "sat" | "unsat" | "unknown" | "not_run",
                "z3_model": {...},
                "constraint_dag": {
                    "n_nodes": ...,
                    "n_edges": ...,
                    "cut_vertices": [...],
                    "beta1": ...,
                    "connected_components": ...,
                },
                "load_bearing_constraints": [...]
            }
    """
    crosshair_violations = run_crosshair(fn_qualname, source_file, timeout=timeout)

    z3_sat: str = "not_run"
    z3_model: dict = {}
    dag_stats: dict = {
        "n_nodes": 0,
        "n_edges": 0,
        "cut_vertices": [],
        "beta1": 0,
        "connected_components": 0,
    }
    G: nx.DiGraph = nx.DiGraph()
    extracted_sites: list[dict] = []

    if pattern is not None:
        # Generic template mode (caller-specified pattern)
        try:
            G, solver = contract_dag_for_pattern(pattern)
            result = solver.check()
            z3_sat = str(result)
            if result == z3.sat:
                m = solver.model()
                z3_model = {str(d): str(m[d]) for d in m.decls() if m[d] is not None}
        except ValueError as exc:
            warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
    else:
        # Function-specific mode: extract actual call sites from AST
        G, solver, extracted_sites = contract_dag_for_function(source_file, fn_qualname)
        if G.number_of_nodes() > 0:
            result = solver.check()
            z3_sat = str(result)
            if result == z3.sat:
                m = solver.model()
                z3_model = {str(d): str(m[d]) for d in m.decls() if m[d] is not None}
        else:
            z3_sat = "unsat"  # no unguarded sites found

    full_stats = analyze_constraint_dag(G)
    dag_stats = {
        k: full_stats[k]
        for k in ("n_nodes", "n_edges", "cut_vertices", "beta1", "connected_components")
    }
    load_bearing = full_stats.get("cut_vertices", [])

    return {
        "function": fn_qualname,
        "file": source_file,
        "crosshair_violations": crosshair_violations,
        "extracted_sites": extracted_sites,
        "z3_sat": z3_sat,
        "z3_model": z3_model,
        "constraint_dag": dag_stats,
        "load_bearing_constraints": load_bearing,
    }


# ---------------------------------------------------------------------------
# Structural risk report — topology + constraint analysis in one pass
# ---------------------------------------------------------------------------


def structural_risk_report(root: str, top_n: int = 20) -> dict:
    """Chain topology analysis → constraint analysis for the top cut vertices.

    For each of the top *top_n* call-graph cut vertices (by betweenness),
    runs :func:`contract_dag_for_function` to extract function-specific
    violations and their formal Z3 encoding.

    Rankings:
    - Primary: call-graph betweenness × (1 + unguarded_site_count)
      so a central function with real violations ranks above a central
      function with none.
    - Secondary: betweenness alone (for functions with no violations).

    Args:
        root: Absolute path to project root.
        top_n: How many cut vertices to analyse (default 20).

    Returns:
        dict with keys:
        - ``project``: project name
        - ``n_cut_vertices``: total cut vertices found
        - ``n_analysed``: how many were run through constraint analysis
        - ``n_violated``: how many have at least one formally-provable violation
        - ``n_clean``: formally clean (no unguarded sites found)
        - ``risk_findings``: list of finding dicts, highest-risk first
    """
    import networkx as nx
    from pathlib import Path as _Path

    from .extractor import extract_from_codebase
    from .reduce import _build_digraph

    root_path = _Path(root)

    _, functions, call_sites = extract_from_codebase(root_path)

    def _is_noise(file: str) -> bool:
        stem = _Path(file).stem
        return stem.startswith("test_") or stem in ("seed_corpus", "__main__")

    prod_fns = [f for f in functions if not _is_noise(f.file)]
    prod_cs = [c for c in call_sites if not _is_noise(c.file)]

    G, func_by_name = _build_digraph(prod_fns, prod_cs)
    if G is None:
        return {
            "project": root_path.name,
            "n_cut_vertices": 0,
            "n_analysed": 0,
            "n_violated": 0,
            "n_clean": 0,
            "risk_findings": [],
        }

    G_u = G.to_undirected()
    btw: dict = nx.betweenness_centrality(G_u, normalized=True)
    cv_set: set = set(nx.articulation_points(G_u))

    # Top cut vertices by betweenness, excluding noise
    top_cvs = sorted(
        [
            (name, btw.get(name, 0.0), func_by_name[name])
            for name in cv_set
            if name in func_by_name and not _is_noise(func_by_name[name].file)
        ],
        key=lambda x: x[1],
        reverse=True,
    )[:top_n]

    findings: list[dict] = []

    for fn_name, btw_score, fn_obj in top_cvs:
        source_file = fn_obj.file

        G_dag, solver, sites = contract_dag_for_function(source_file, fn_name)
        unguarded = [s for s in sites if not s["guarded"]]

        z3_sat = "not_run"
        z3_model: dict = {}
        if G_dag.number_of_nodes() > 0:
            result = solver.check()
            z3_sat = str(result)
            if result == z3.sat:
                m = solver.model()
                z3_model = {str(d): str(m[d]) for d in m.decls() if m[d] is not None}
        elif sites:
            z3_sat = "unsat"  # sites found but all guarded

        dag_stats = analyze_constraint_dag(G_dag)

        # Risk score: betweenness amplified by number of formal violations
        risk_score = btw_score * (1 + len(unguarded))

        findings.append(
            {
                "function": fn_name,
                "file": source_file,
                "betweenness": round(btw_score, 4),
                "risk_score": round(risk_score, 4),
                "unguarded_sites": unguarded,
                "guarded_sites": [s for s in sites if s["guarded"]],
                "z3_sat": z3_sat,
                "z3_model": z3_model,
                "load_bearing_constraints": dag_stats.get("cut_vertices", [])[:5],
                "beta1": dag_stats.get("beta1", 0),
                "constraint_dag_nodes": dag_stats.get("n_nodes", 0),
            }
        )

    # Sort by risk score descending
    findings.sort(key=lambda x: x["risk_score"], reverse=True)

    n_violated = sum(1 for f in findings if f["unguarded_sites"])
    n_clean = sum(1 for f in findings if not f["unguarded_sites"])

    return {
        "project": root_path.name,
        "n_cut_vertices": len(cv_set),
        "n_analysed": len(findings),
        "n_violated": n_violated,
        "n_clean": n_clean,
        "risk_findings": findings,
    }


# ---------------------------------------------------------------------------
# Adapter: pact_risk output → pact_heal violations JSON
# ---------------------------------------------------------------------------

_PATTERN_STATEMENTS = {
    "json_loads_unguarded": (
        "json.loads() calls must be wrapped in try/except that catches "
        "json.JSONDecodeError or ValueError"
    ),
    "subprocess_unchecked": (
        "subprocess invocations must use check=True, check=False with explicit "
        "returncode inspection, or handle CalledProcessError"
    ),
    "content_index_unguarded": (
        "List index access [0] requires a prior length or emptiness check"
    ),
    "optional_deref_unguarded": (
        "Optional[T] parameters must be checked for None before attribute or "
        "method access"
    ),
}


def risk_to_violations(risk_report: dict) -> dict:
    """Convert pact_risk output to pact_heal-compatible violations JSON.

    pact_risk produces findings keyed by function and betweenness.
    pact_heal expects modules with invariants and violations lists.
    This adapter bridges them so the pipeline is:

        pact_risk → risk_to_violations → pact_heal → oracle → patches

    Args:
        risk_report: dict returned by :func:`structural_risk_report`.

    Returns:
        dict in pact_heal violations format::

            {
                "project": str,
                "generated_by": "pact.constraint_graph",
                "modules": [
                    {
                        "path": str,
                        "invariants": [...],
                        "violations": [...]
                    }
                ]
            }
    """
    # Group unguarded sites by file
    by_file: dict[str, list[dict]] = {}
    for finding in risk_report.get("risk_findings", []):
        for site in finding.get("unguarded_sites", []):
            fpath = finding["file"]
            if fpath not in by_file:
                by_file[fpath] = []
            by_file[fpath].append(
                {
                    "function": finding["function"],
                    "betweenness": finding["betweenness"],
                    "risk_score": finding["risk_score"],
                    **site,
                }
            )

    modules = []
    for fpath, sites in sorted(by_file.items()):
        # One invariant per pattern per file
        seen_patterns: dict[str, dict] = {}
        invariants = []
        violations = []

        for site in sites:
            pattern = site["pattern"]
            inv_id = f"{pattern}_{Path(fpath).stem}"

            if pattern not in seen_patterns:
                inv = {
                    "id": inv_id,
                    "type": pattern,
                    "statement": _PATTERN_STATEMENTS.get(
                        pattern, f"Violation: {pattern}"
                    ),
                    "severity": "high",
                    "confidence": 0.9,
                }
                invariants.append(inv)
                seen_patterns[pattern] = inv

            via_note = f" (via {site['via']})" if site.get("via") else ""
            violations.append(
                {
                    "invariant_id": inv_id,
                    "file": fpath,
                    "line": site["line"],
                    "severity": "high",
                    "evidence": (
                        "subprocess.run(...)"
                        if "subprocess" in pattern
                        else f"json.loads({site.get('arg', '...')})"
                    ),
                    "explanation": (
                        f"{pattern} at line {site['line']}{via_note} "
                        f"in {site['function']} (btw={site['betweenness']:.4f})"
                    ),
                }
            )

        modules.append(
            {"path": fpath, "invariants": invariants, "violations": violations}
        )

    return {
        "project": risk_report.get("project", "?"),
        "generated_by": "pact.constraint_graph",
        "modules": modules,
    }
