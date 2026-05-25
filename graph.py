"""
Call graph construction.

Builds a directed graph where nodes are qualified function names and
edges are call relationships. Used to trace data flow across boundaries —
e.g., to know whether a value that may be None can reach a NOT NULL field.
"""

import warnings
from dataclasses import dataclass

from .extractor import CallSite, FunctionManifest

try:
    import networkx as nx

    _HAS_NX = True
except ImportError:
    _HAS_NX = False


@dataclass
class CallGraph:
    _g: object  # networkx.DiGraph or None

    def _require_graph(self) -> bool:
        """Warn and return False when the graph layer is unavailable."""
        if not _HAS_NX:
            warnings.warn(
                "CallGraph: networkx is not installed — graph queries return empty results; "
                "install networkx to enable call-graph analysis",
                RuntimeWarning,
                stacklevel=3,
            )
            return False
        if self._g is None:
            warnings.warn(
                "CallGraph: graph was never built — call build_call_graph() first; "
                "returning empty results",
                RuntimeWarning,
                stacklevel=3,
            )
            return False
        return True

    def reachable_from(self, func_name: str) -> list[str]:
        if not self._require_graph():
            return []
        if func_name not in self._g:
            return []
        return list(nx.descendants(self._g, func_name))

    def callers_of(self, func_name: str) -> list[str]:
        if not self._require_graph():
            return []
        if func_name not in self._g:
            return []
        return list(self._g.predecessors(func_name))

    def call_sites_to(self, func_name: str) -> list[CallSite]:
        if not self._require_graph():
            return []
        if func_name not in self._g:
            return []
        sites = []
        for _, _, data in self._g.in_edges(func_name, data=True):
            site = data.get("call_site")
            if site:
                sites.append(site)
        return sites


def build_call_graph(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
) -> CallGraph:
    if not _HAS_NX:
        return CallGraph(_g=None)

    G = nx.DiGraph()

    func_names = {f.name for f in functions}
    # Also index short names (for unqualified calls)
    short_to_qual: dict[str, str] = {}
    for f in functions:
        G.add_node(f.name, manifest=f)
        short = f.name.split(".")[-1]
        short_to_qual.setdefault(short, f.name)

    for call in call_sites:
        source = call.caller_name or "__root__"
        target = call.callee_name
        if target not in func_names:
            short = target.split(".")[-1]
            target = short_to_qual.get(short, target)
        G.add_edge(source, target, call_site=call)

    return CallGraph(_g=G)
