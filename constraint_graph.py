"""Constraint graph analysis: Z3 expression DAGs + NetworkX topology.

Key insight: Z3's expression trees are DAGs. expr.children() gives edges,
expr.get_id() gives node IDs. Graph analysis on those DAGs ranks constraints
by structural importance. A cut vertex with high betweenness is a load-bearing
invariant — remove it and the violation proof collapses.
"""

from __future__ import annotations

import re
import subprocess
import sys
import warnings
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

    if pattern is not None:
        try:
            G, solver = contract_dag_for_pattern(pattern)
            result = solver.check()
            z3_sat = str(result)
            if result == z3.sat:
                m = solver.model()
                z3_model = {str(d): str(m[d]) for d in m.decls() if m[d] is not None}
        except ValueError as exc:
            warnings.warn(str(exc), RuntimeWarning, stacklevel=2)

        full_stats = analyze_constraint_dag(G)
        dag_stats = {
            k: full_stats[k]
            for k in (
                "n_nodes",
                "n_edges",
                "cut_vertices",
                "beta1",
                "connected_components",
            )
        }
        load_bearing = full_stats.get("cut_vertices", [])
    else:
        load_bearing = []

    return {
        "function": fn_qualname,
        "file": source_file,
        "crosshair_violations": crosshair_violations,
        "z3_sat": z3_sat,
        "z3_model": z3_model,
        "constraint_dag": dag_stats,
        "load_bearing_constraints": load_bearing,
    }
