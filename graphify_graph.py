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
import warnings
from pathlib import Path


class CallGraph:
    """
    In-memory index of graphify call edges.

    Nodes are keyed by (source_file_basename, func_label).  func_label is the
    raw label stored by graphify, e.g. ``_extract_llm_facts`` (no parens).
    """

    def __init__(self, nodes: list[dict], links: list[dict]) -> None:
        # node_id → {"label": str, "file": str, "loc": str, "community": int|None}
        self._id_meta: dict[str, dict] = {}
        # (file_basename, func_label) → node_id  (for lookup by violation attrs)
        self._func_index: dict[tuple[str, str], str] = {}
        # file_basename → node_id  (for file-level community lookup)
        self._file_index: dict[str, str] = {}
        # node_id → community_id
        self._community: dict[str, int] = {}
        # community_id → short plain-language description (from rationale nodes)
        self._community_label: dict[int, str] = {}

        for n in nodes:
            nid = n.get("id", "")
            label = n.get("label", "")
            sf = n.get("source_file", "")
            loc = n.get("source_location", "")
            community = n.get("community")
            self._id_meta[nid] = {"label": label, "file": sf, "loc": loc}

            if community is not None:
                self._community[nid] = community

            # Index function nodes (graphify marks them with trailing "()")
            if label.endswith("()"):
                fname = label[:-2]
                sf_key = Path(sf).name if sf else sf
                self._func_index[(sf_key, fname)] = nid

            # Index file-level nodes for community lookup by filename
            elif sf and not label.endswith("()") and n.get("file_type") == "code":
                sf_key = Path(sf).name if sf else sf
                if sf_key not in self._file_index:
                    self._file_index[sf_key] = nid

        # Build community labels from rationale nodes and their rationale_for links.
        # rationale nodes have file_type="rationale"; rationale_for links point to
        # the code node they describe.  We use the first rationale sentence as the
        # community label (truncated to 80 chars) for the community that code node
        # belongs to.
        rationale_labels: dict[str, str] = {
            n["id"]: n.get("label", "")
            for n in nodes
            if n.get("file_type") == "rationale"
        }
        for link in links:
            if link.get("relation") != "rationale_for":
                continue
            rat_id = link.get("source", "")
            tgt_id = link.get("target", "")
            rat_text = rationale_labels.get(rat_id, "")
            if not rat_text:
                continue
            community = self._community.get(tgt_id)
            if community is not None and community not in self._community_label:
                # Take first sentence of the rationale, strip trailing whitespace
                first_sentence = rat_text.split(".")[0].strip()[:80]
                if first_sentence:
                    self._community_label[community] = first_sentence

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

    def community_of(self, func_name: str, source_file: str = "") -> int | None:
        """Return the graphify Louvain community ID for *func_name*, or None.

        Falls back to a file-level lookup when the function is not found directly
        (useful for violations detected at the file level, not the call level).
        """
        sf_basename = Path(source_file).name if source_file else ""
        node_id = None

        if sf_basename:
            node_id = self._func_index.get((sf_basename, func_name))
        if not node_id:
            matches = [
                nid for (sf, fn), nid in self._func_index.items() if fn == func_name
            ]
            if len(matches) == 1:
                node_id = matches[0]
        if not node_id and sf_basename:
            # File-level fallback — return the community of the file node
            node_id = self._file_index.get(sf_basename)

        return self._community.get(node_id) if node_id else None

    def community_label_for(self, community_id: int) -> str:
        """Return a short plain-language label for *community_id*.

        The label is the first sentence of the graphify-generated rationale for
        one of the nodes in that community (at most 80 characters).  Returns an
        empty string if no rationale is available.
        """
        return self._community_label.get(community_id, "")

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
            except Exception as exc:
                warnings.warn(
                    f"JSON parse of graphify output failed: {exc}", RuntimeWarning
                )

        return cls._from_ast(root)

    @classmethod
    def _from_ast(cls, root: Path) -> "CallGraph | None":
        """Build a minimal CallGraph by walking Python AST — no graphify needed.

        Pass 1: index all function definitions across all files.
        Pass 2: build same-file call edges.
        Pass 3: follow `from module import name` and `import module` edges to
                cross-file calls (module-name prefix resolution).

        This is less accurate than graphify (no type inference, no dynamic calls)
        but gives a usable inter-module call graph for topology scoring.
        """
        import ast as _ast

        nodes: list[dict] = []
        links: list[dict] = []
        node_counter = 0

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
        ]  # cap to avoid huge graphs on large projects

        # Pass 1: collect all function definitions globally
        # global_func_index: func_name → node_id (first definition wins)
        # file_defs: fpath → {func_name → node_id}
        global_func_index: dict[str, str] = {}
        file_defs: dict[str, dict[str, str]] = {}
        file_trees: dict[str, "_ast.Module"] = {}

        for fpath in py_files:
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
                tree = _ast.parse(source, filename=str(fpath))
                file_trees[str(fpath)] = tree
            except (SyntaxError, OSError):
                continue

            local: dict[str, str] = {}
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    nid = f"fn_{node_counter}"
                    node_counter += 1
                    nodes.append(
                        {
                            "id": nid,
                            "label": f"{node.name}()",
                            "source_file": str(fpath),
                            "source_location": str(node.lineno),
                        }
                    )
                    local[node.name] = nid
                    global_func_index.setdefault(node.name, nid)
            file_defs[str(fpath)] = local

        # Pass 2 + 3: emit call edges (same-file + cross-file via imports)
        for fpath_str, tree in file_trees.items():
            local = file_defs.get(fpath_str, {})

            # Build import name → function_ids mapping for this file
            # `from foo import bar` → bar resolves to global_func_index["bar"]
            # `import foo; foo.bar()` → bar resolves to global_func_index["bar"]
            import_aliases: dict[str, str] = {}  # local_name → canonical func_name
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ImportFrom):
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        import_aliases[local_name] = alias.name
                elif isinstance(node, _ast.Import):
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        import_aliases[local_name] = alias.name

            for node in _ast.walk(tree):
                if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    continue
                src_id = local.get(node.name)
                if src_id is None:
                    continue

                for child in _ast.walk(node):
                    if not isinstance(child, _ast.Call):
                        continue

                    callee_name = ""
                    if isinstance(child.func, _ast.Name):
                        callee_name = child.func.id
                    elif isinstance(child.func, _ast.Attribute):
                        # `module.func()` — try to resolve module import
                        attr = child.func.attr
                        callee_name = attr

                    if not callee_name:
                        continue

                    # Resolve: same-file first, then imported, then global
                    tgt_id = local.get(callee_name)
                    if tgt_id is None:
                        canon = import_aliases.get(callee_name, callee_name)
                        tgt_id = global_func_index.get(canon)
                        if tgt_id is None:
                            tgt_id = global_func_index.get(callee_name)

                    if tgt_id and tgt_id != src_id:
                        links.append(
                            {
                                "source": src_id,
                                "target": tgt_id,
                                "context": "call",
                                "source_file": fpath_str,
                                "source_location": str(getattr(child, "lineno", "")),
                            }
                        )

        if not nodes:
            return None
        return cls(nodes, links)
