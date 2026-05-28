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


def run_crosshair(
    fn_qualname: str, source_file: str, timeout: float = 10.0
) -> list[dict]:
    """Run crosshair on *fn_qualname* in *source_file*.

    Returns a list of violation dicts::

        {"file": str, "line": int, "message": str, "type": "crosshair_violation"}

    The output format is ``<filename>:<line>: error: <message>``.
    """
    # crosshair check accepts TARGET as path::fn or module.fn.
    # The safest form for an arbitrary file is ``<file>::<fn>``.
    target = f"{source_file}::{fn_qualname}"
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
            timeout=timeout + 5,
        )
        raw = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        warnings.warn(
            f"crosshair timed out checking {fn_qualname}", RuntimeWarning, stacklevel=2
        )
        return []
    except FileNotFoundError:
        warnings.warn(
            "crosshair not found — install it with: pip install crosshair-tool",
            RuntimeWarning,
            stacklevel=2,
        )
        return []

    violations: list[dict] = []
    # Parse lines of the form: filename:line: error: message
    pattern = re.compile(r"^(.+?):(\d+):\s*error:\s*(.+)$")
    for line in raw.splitlines():
        m = pattern.match(line.strip())
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


def _is_index_guarded(subscript_node: ast.Subscript, scope: ast.AST) -> bool:
    """True if subscript_node is inside an if-block or after a len/bool check."""
    container_src = ast.unparse(subscript_node.value)

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
            # Only flag [0] / [-1] literal index — most dangerous
            idx = node.slice
            if not (isinstance(idx, ast.Constant) and idx.value in (0, -1)):
                continue
            # The accessed expression (the container)
            container = ast.unparse(node.value)
            # Skip simple string/dict literals — those don't index-error at 0
            if isinstance(node.value, (ast.Constant, ast.Dict, ast.Set)):
                continue
            # Check if there's a guard: len(...) > 0, if ..., or similar
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
