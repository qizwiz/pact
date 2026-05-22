"""
Optional call-graph enrichment from graphify-out/graph.json.

When graphify has been run on a project (generating graphify-out/graph.json),
pact can load the extracted call edges and annotate each violation with its
direct callers — functions that call a flagged function and may propagate the
error upward.  All 9 violation modes benefit: the enrichment is mode-agnostic.

If graph.json is absent or unreadable, every public function returns [] or None
and pact runs as if this module doesn't exist.
"""

from __future__ import annotations

import json
from pathlib import Path


class CallGraph:
    """
    In-memory index of graphify call edges.

    Nodes are keyed by (source_file_basename, func_label).  func_label is the
    raw label stored by graphify, e.g. ``_extract_llm_facts`` (no parens).
    """

    def __init__(self, nodes: list[dict], links: list[dict]) -> None:
        # node_id → {"label": str, "file": str, "loc": str}
        self._id_meta: dict[str, dict] = {}
        # (file_basename, func_label) → node_id  (for lookup by violation attrs)
        self._func_index: dict[tuple[str, str], str] = {}

        for n in nodes:
            nid = n.get("id", "")
            label = n.get("label", "")
            sf = n.get("source_file", "")
            loc = n.get("source_location", "")
            self._id_meta[nid] = {"label": label, "file": sf, "loc": loc}

            # Only index function nodes (graphify marks them with trailing "()")
            if label.endswith("()"):
                fname = label[:-2]
                sf_key = Path(sf).name if sf else sf
                self._func_index[(sf_key, fname)] = nid

        id_meta = self._id_meta  # alias for readability below

        # callee node_id → list of caller metadata dicts
        self._callers: dict[str, list[dict]] = {}
        # bidirectional edge indices for neighborhood BFS
        self._out_edges: dict[str, set[str]] = {}  # caller_id → callee_ids
        self._in_edges: dict[str, set[str]] = {}  # callee_id → caller_ids
        for link in links:
            if link.get("context") != "call":
                continue
            target = link["target"]
            src_id = link["source"]
            src_meta = id_meta.get(src_id, {})
            self._callers.setdefault(target, []).append(
                {
                    "label": src_meta.get("label", src_id).rstrip("()"),
                    "file": link.get("source_file", src_meta.get("file", "")),
                    "loc": link.get("source_location", src_meta.get("loc", "")),
                }
            )
            self._out_edges.setdefault(src_id, set()).add(target)
            self._in_edges.setdefault(target, set()).add(src_id)

    def callers_of(self, func_name: str, source_file: str = "") -> list[str]:
        """
        Return up to 5 direct callers of *func_name* as ``"file:loc  caller"`` strings.

        Parameters
        ----------
        func_name:
            Function name as it appears in a pact Violation (no parentheses).
        source_file:
            Absolute or relative path of the file where the function lives.
            Used to narrow the lookup when multiple functions share a name.
        """
        sf_basename = Path(source_file).name if source_file else ""
        node_id = None

        # Exact match on (file, func)
        if sf_basename:
            node_id = self._func_index.get((sf_basename, func_name))

        # Fallback: match by func name alone (unique across project)
        if not node_id:
            matches = [
                nid for (sf, fn), nid in self._func_index.items() if fn == func_name
            ]
            if len(matches) == 1:
                node_id = matches[0]

        if not node_id:
            return []

        callers = self._callers.get(node_id, [])
        results: list[str] = []
        for c in callers[:5]:
            label = c["label"] or "(unknown)"
            loc = c["loc"] or ""
            f = c["file"] or ""
            addr = f"{f}:{loc}" if f and loc else (f or loc or "")
            results.append(f"{addr}  {label}" if addr else label)
        return results

    @classmethod
    def load(cls, root: Path) -> "CallGraph | None":
        """
        Load ``graphify-out/graph.json`` from *root*.

        If the graphify file is absent, falls back to a minimal AST-derived
        call graph (function definitions + call sites). The fallback is less
        accurate than graphify (no cross-file resolution, no type inference)
        but enables topology scoring and priority ordering on any Python project.

        Returns None only if the target has no Python files.
        """
        graph_path = root / "graphify-out" / "graph.json"
        if graph_path.exists():
            try:
                g = json.loads(graph_path.read_text())
                return cls(g.get("nodes", []), g.get("links", []))
            except Exception:
                pass  # fall through to AST fallback

        return cls._from_ast(root)

    @classmethod
    def _from_ast(cls, root: Path) -> "CallGraph | None":
        """Build a minimal CallGraph by walking Python AST — no graphify needed.

        Creates one node per function definition and one 'call' link per
        function call that resolves to a known function name in the same scope.
        Only same-file calls are indexed (no cross-file resolution).
        """
        import ast as _ast

        nodes: list[dict] = []
        links: list[dict] = []
        node_counter = 0
        func_name_to_id: dict[str, str] = {}  # func_name → node_id (last seen)

        skip_dirs = frozenset(
            {
                "__pycache__",
                ".venv",
                "venv",
                "env",
                ".git",
                "node_modules",
                "dist",
                "build",
            }
        )
        py_files = [
            p
            for p in sorted(root.rglob("*.py"))
            if not any(
                part in skip_dirs or part.endswith(".egg-info") for part in p.parts
            )
        ][
            :200
        ]  # cap at 200 files to avoid huge graphs on big projects

        for fpath in py_files:
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
                tree = _ast.parse(source, filename=str(fpath))
            except (SyntaxError, OSError):
                continue

            # First pass: collect all function definitions in this file
            file_func_ids: dict[str, str] = {}
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    nid = f"fn_{node_counter}"
                    node_counter += 1
                    label = f"{node.name}()"
                    loc = str(node.lineno)
                    nodes.append(
                        {
                            "id": nid,
                            "label": label,
                            "source_file": str(fpath),
                            "source_location": loc,
                        }
                    )
                    file_func_ids[node.name] = nid
                    func_name_to_id[node.name] = nid

            # Second pass: emit call links for calls to known functions
            for node in _ast.walk(tree):
                if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    continue
                src_id = file_func_ids.get(node.name)
                if src_id is None:
                    continue
                for child in _ast.walk(node):
                    if isinstance(child, _ast.Call):
                        callee_name = ""
                        if isinstance(child.func, _ast.Name):
                            callee_name = child.func.id
                        elif isinstance(child.func, _ast.Attribute):
                            callee_name = child.func.attr
                        tgt_id = file_func_ids.get(callee_name)
                        if tgt_id and tgt_id != src_id:
                            links.append(
                                {
                                    "source": src_id,
                                    "target": tgt_id,
                                    "context": "call",
                                    "source_file": str(fpath),
                                    "source_location": str(
                                        getattr(child, "lineno", "")
                                    ),
                                }
                            )

        if not nodes:
            return None
        return cls(nodes, links)
