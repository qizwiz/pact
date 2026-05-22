"""
Topological violation severity scoring.

For each pact violation, extract the k-hop call neighborhood from a graphify
call graph and compute β₁ (first Betti number = count of independent cycles in
the undirected neighborhood).

High β₁ means the vulnerable function sits inside a dense "diamond" of
redundant call paths — the bug is reachable by more independent routes, so
the blast radius is larger.

Topology score = β₁ × caller_count / neighborhood_size
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ast_utils import find_enclosing_function_chain
from graphify_graph import CallGraph


@dataclass
class TopoScore:
    func_name: str
    source_file: str
    n_nodes: int  # neighborhood size
    n_edges: int
    beta0: int  # connected components in neighborhood
    beta1: int  # independent cycles = redundant call paths
    n_callers: int  # direct callers in full graph

    @property
    def severity(self) -> float:
        """β₁ × callers / size.  Higher = more propagation risk."""
        if self.n_nodes == 0:
            return 0.0
        return (self.beta1 * self.n_callers) / self.n_nodes

    def __str__(self) -> str:
        return (
            f"{self.func_name}  ({Path(self.source_file).name})\n"
            f"  neighborhood: {self.n_nodes}v {self.n_edges}e  "
            f"β₀={self.beta0} β₁={self.beta1}  callers={self.n_callers}  "
            f"severity={self.severity:.2f}"
        )


def _find_node(cg: CallGraph, func_name: str, source_file: str = "") -> str | None:
    """Return node_id for func_name, or None if not in graph."""
    sf_basename = Path(source_file).name if source_file else ""
    if sf_basename:
        nid = cg._func_index.get((sf_basename, func_name))
        if nid:
            return nid
    matches = [nid for (sf, fn), nid in cg._func_index.items() if fn == func_name]
    return matches[0] if len(matches) == 1 else None


def neighborhood_edges(
    cg: CallGraph, node_id: str, hops: int = 2
) -> set[tuple[str, str]]:
    """
    BFS from node_id in both directions (callers + callees) up to hops steps.
    Returns the set of call edges (src_id, tgt_id) within the neighborhood.
    """
    visited: set[str] = {node_id}
    frontier: set[str] = {node_id}
    edges: set[tuple[str, str]] = set()

    for _ in range(hops):
        next_frontier: set[str] = set()
        for nid in frontier:
            for callee in cg._out_edges.get(nid, set()):
                edges.add((nid, callee))
                if callee not in visited:
                    visited.add(callee)
                    next_frontier.add(callee)
            for caller in cg._in_edges.get(nid, set()):
                edges.add((caller, nid))
                if caller not in visited:
                    visited.add(caller)
                    next_frontier.add(caller)
        frontier = next_frontier

    return edges


def _beta1(edges: set[tuple[str, str]]) -> tuple[int, int, int]:
    """Return (n_nodes, beta0, beta1) for the undirected graph defined by edges."""
    nodes: set[str] = set()
    for u, v in edges:
        nodes.add(u)
        nodes.add(v)
    V = len(nodes)
    E = len(edges)
    if V == 0:
        return 0, 0, 0

    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    components = V
    for u, v in edges:
        pu, pv = find(u), find(v)
        if pu != pv:
            parent[pu] = pv
            components -= 1

    beta0 = components
    beta1 = E - V + beta0  # Euler: χ = V - E,  β₁ = E - V + β₀
    return V, beta0, max(beta1, 0)


def score_function(
    cg: CallGraph,
    func_name: str,
    source_file: str = "",
    hops: int = 2,
) -> TopoScore | None:
    """
    Compute topology score for a single function.
    Returns None if the function is not in the call graph.
    """
    node_id = _find_node(cg, func_name, source_file)
    if node_id is None:
        return None

    edges = neighborhood_edges(cg, node_id, hops=hops)
    n_nodes, beta0, beta1 = _beta1(edges)
    n_callers = len(cg._in_edges.get(node_id, set()))

    return TopoScore(
        func_name=func_name,
        source_file=source_file,
        n_nodes=n_nodes,
        n_edges=len(edges),
        beta0=beta0,
        beta1=beta1,
        n_callers=n_callers,
    )


def score_at_line(
    cg: CallGraph,
    filepath: str,
    line: int,
    hops: int = 2,
) -> TopoScore | None:
    """
    Score the violation at (filepath, line) by walking the enclosing function
    chain (innermost → outermost) until a match is found in the call graph.

    Handles nested helpers (e.g. ``messages()`` inside ``anthropic_chat``) by
    falling back to the containing function when the inner one isn't indexed.
    """
    chain = find_enclosing_function_chain(filepath, line)
    # try innermost first, then walk outward
    for func in reversed(chain):
        # strip class prefix for graphify lookup (graphify stores bare names)
        bare = func.split(".")[-1]
        ts = score_function(cg, bare, filepath, hops=hops)
        if ts is not None:
            ts.func_name = func  # restore qualified name
            return ts
    return None


def score_violations(
    cg: CallGraph,
    violations: list,  # list of pact Violation objects
    hops: int = 2,
) -> list[tuple[object, TopoScore | None]]:
    """
    Score a list of pact Violation objects.
    Returns list of (violation, TopoScore | None), sorted by severity descending.
    """
    results = []
    for v in violations:
        func = getattr(v, "call", "") or ""
        sf = getattr(v, "file", "") or ""
        ts = score_function(cg, func, sf, hops=hops)
        results.append((v, ts))
    results.sort(key=lambda x: x[1].severity if x[1] else 0.0, reverse=True)
    return results


def score_corpus(
    graph_path: Path | str,
    corpus_path: Path | str,
    hops: int = 2,
    top_n: int = 30,
) -> list[dict]:
    """
    Score violations from a pact corpus JSONL against a graphify call graph.

    The corpus entries must have 'call' (function name) and 'file' fields.
    Only entries whose function appears in the call graph are scored.

    Returns a list of dicts sorted by severity descending.
    """
    try:
        g = json.loads(Path(graph_path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"graph_path {graph_path!r} does not contain valid JSON: {exc}"
        ) from exc
    cg = CallGraph(g.get("nodes", []), g.get("links", []))

    scored: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (func, file_basename) dedup

    for line in Path(corpus_path).read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            import warnings

            warnings.warn(
                f"Skipping invalid JSON line in corpus: {exc!s}",
                UserWarning,
                stacklevel=2,
            )
            continue
        func = entry.get("call", "")
        sf = entry.get("file", "")
        key = (func, Path(sf).name)
        if not func or key in seen:
            continue
        seen.add(key)

        ts = score_function(cg, func, sf, hops=hops)
        if ts is None:
            continue

        scored.append(
            {
                "func": func,
                "file": sf,
                "repo": entry.get("repo", ""),
                "mode": entry.get("mode", ""),
                "n_nodes": ts.n_nodes,
                "n_edges": ts.n_edges,
                "beta0": ts.beta0,
                "beta1": ts.beta1,
                "n_callers": ts.n_callers,
                "severity": ts.severity,
            }
        )

    scored.sort(key=lambda x: x["severity"], reverse=True)
    return scored[:top_n]


def main() -> None:
    """
    CLI: pact_tda <graph.json> <corpus.jsonl> [--hops N] [--top N]

    Scores violations in corpus.jsonl against the graphify call graph
    and prints a ranked severity table.
    """
    import argparse

    p = argparse.ArgumentParser(description="TDA violation severity scorer")
    p.add_argument("graph", help="Path to graphify-out/graph.json")
    p.add_argument("corpus", help="Path to pact corpus JSONL")
    p.add_argument(
        "--hops", type=int, default=2, help="Neighborhood radius (default 2)"
    )
    p.add_argument("--top", type=int, default=20, help="Show top N results")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of table")
    args = p.parse_args()

    results = score_corpus(args.graph, args.corpus, hops=args.hops, top_n=args.top)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No violations found in call graph.")
        return

    print(f"\n{'Rank':<5} {'β₁':>4} {'cal':>4} {'sev':>6}  {'function':<35} {'file'}")
    print("-" * 90)
    for i, r in enumerate(results, 1):
        fn = r["func"][:34]
        sf = Path(r["file"]).name[:30]
        print(
            f"{i:<5} {r['beta1']:>4} {r['n_callers']:>4} {r['severity']:>6.1f}"
            f"  {fn:<35} {sf}"
        )


if __name__ == "__main__":
    main()
