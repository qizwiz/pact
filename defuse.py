"""
defuse -- variable-level taint analysis via beniget def-use chains.

Wraps beniget's intraprocedural DefUseChains to produce TaintFlow records:
each flow traces a named variable from its definition site to a use site,
classifying the use context and naming the sink.

Public API
----------
build_defuse(source, filename) -> DefUseGraph
class DefUseGraph:
    taint_flows(source_names) -> list[TaintFlow]

class TaintFlow:
    .render() -> str
    .to_dict() -> dict
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Optional

try:
    import beniget

    _BENIGET_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BENIGET_AVAILABLE = False

# Maximum hop depth when following def-use chains transitively.
_MAX_DEPTH = 5

# ---------------------------------------------------------------------------
# TaintFlow
# ---------------------------------------------------------------------------


@dataclass
class TaintFlow:
    variable: str  # variable name (the original source)
    defined_at: int  # line number of the Store/arg definition
    used_at: int  # line number of the use
    use_context: str  # "call", "assign", "return", "subscript", "other"
    sink_name: str  # name of function/attr being called (empty if not a call)
    confidence: float  # 1.0 direct, 0.8 through-assign
    # internal — not serialised as a top-level field
    _chain_depth: int = field(default=0, repr=False, compare=False)

    # ------------------------------------------------------------------
    def render(self) -> str:
        """Human-readable one-liner."""
        loc = f"line {self.defined_at} → line {self.used_at}"
        ctx = self.use_context
        if self.sink_name and ctx == "call":
            ctx = f"call to {self.sink_name!r}"
        conf_pct = int(self.confidence * 100)
        return (
            f"variable {self.variable!r} defined at line {self.defined_at} "
            f"flows to {ctx} at line {self.used_at} ({loc}) [{conf_pct}%]"
        )

    def to_dict(self) -> dict:
        return {
            "variable": self.variable,
            "defined_at": self.defined_at,
            "used_at": self.used_at,
            "use_context": self.use_context,
            "sink_name": self.sink_name,
            "confidence": self.confidence,
            "render": self.render(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_call_sink(call_node: ast.Call) -> str:
    """Return the name of the callable in a Call node (best-effort)."""
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# ---------------------------------------------------------------------------
# DefUseGraph
# ---------------------------------------------------------------------------


class DefUseGraph:
    """
    Wraps beniget DefUseChains for a single Python source string.

    If beniget is not installed, taint_flows() always returns [].
    If source has a SyntaxError, taint_flows() always returns [].
    """

    def __init__(self, source: str, filename: str = "<string>") -> None:
        self._source = source
        self._filename = filename
        self._tree: Optional[ast.AST] = None
        self._duc: object = None  # beniget.DefUseChains or None
        self._parents: dict[int, ast.AST] = {}
        self._error: Optional[Exception] = None
        self._ready = False
        self._build()

    def _build(self) -> None:
        if not _BENIGET_AVAILABLE:
            return  # pragma: no cover
        try:
            self._tree = ast.parse(self._source, filename=self._filename)
        except SyntaxError as e:
            self._error = e
            return
        # Build parent map (id(child) → parent) for context classification
        for node in ast.walk(self._tree):
            for child in ast.iter_child_nodes(node):
                self._parents[id(child)] = node
        # Run beniget, suppressing its "unbound identifier" warnings
        import warnings

        duc = beniget.DefUseChains()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            duc.visit(self._tree)
        self._duc = duc
        self._ready = True

    # ------------------------------------------------------------------

    def taint_flows(self, source_names: list[str]) -> list[TaintFlow]:
        """
        Trace all def-use chains from variable definitions whose names
        case-insensitively contain any of the source_names substrings.

        Returns one TaintFlow per (source-definition, use-site) pair that
        reaches a call, return, or subscript use, following assign edges
        transitively up to _MAX_DEPTH hops.
        """
        if not self._ready or self._duc is None:
            return []

        patterns = [p.lower() for p in source_names]

        def _matches(name: str) -> bool:
            low = name.lower()
            return any(p in low for p in patterns)

        results: list[TaintFlow] = []
        # key: (source_var_name, source_lineno, use_lineno) — dedup
        seen: set[tuple[str, int, int]] = set()

        duc = self._duc

        # Walk all chains.  Definitions are ast.Name nodes with Store context,
        # or ast.arg nodes (function parameters).
        for node, chain_def in duc.chains.items():
            var_name: Optional[str] = None
            defined_at: int = getattr(node, "lineno", 0)

            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                var_name = node.id
            elif isinstance(node, ast.arg):
                var_name = node.arg

            if var_name is None or not _matches(var_name):
                continue

            # BFS over def-use chains.
            # Queue entry: (def_object, hop_depth, confidence)
            # We start from the Store/arg Def and walk its .users().
            # When a use lands inside an Assign (as the RHS), we find the
            # Store targets of that Assign and continue from those.
            queue: list[tuple[object, int, float]] = [(chain_def, 0, 1.0)]
            visited_defs: set[int] = set()

            while queue:
                cur_def, depth, conf = queue.pop()
                def_id = id(cur_def)
                if def_id in visited_defs:
                    continue
                visited_defs.add(def_id)

                if depth > _MAX_DEPTH:
                    continue

                for user_def in cur_def.users():
                    user_node = user_def.node
                    use_line: int = getattr(user_node, "lineno", 0)
                    parent = self._parents.get(id(user_node))

                    use_context, sink_name = self._classify_use(user_node, parent)

                    # Record non-trivial flows
                    if use_context != "other":
                        key = (var_name, defined_at, use_line)
                        if key not in seen:
                            seen.add(key)
                            results.append(
                                TaintFlow(
                                    variable=var_name,
                                    defined_at=defined_at,
                                    used_at=use_line,
                                    use_context=use_context,
                                    sink_name=sink_name,
                                    confidence=conf,
                                    _chain_depth=depth,
                                )
                            )

                    # Follow assign-chains: when the use is inside an Assign
                    # as the value (RHS), find Store targets and trace through them.
                    if depth + 1 <= _MAX_DEPTH and isinstance(parent, ast.Assign):
                        # Check that user_node is the value, not one of the targets
                        if parent.value is user_node or self._is_inside(
                            user_node, parent.value
                        ):
                            for tgt in parent.targets:
                                tgt_chain = duc.chains.get(tgt)
                                if tgt_chain is not None:
                                    queue.append((tgt_chain, depth + 1, conf * 0.8))

                    # Follow the user_def itself transitively (for Load nodes
                    # that have their own downstream users — e.g. subscript chains)
                    if depth + 1 <= _MAX_DEPTH and use_context == "other":
                        queue.append((user_def, depth + 1, conf * 0.8))

        return results

    # ------------------------------------------------------------------

    @staticmethod
    def _classify_use(use_node: ast.AST, parent: Optional[ast.AST]) -> tuple[str, str]:
        """
        Given a use node and its immediate AST parent, return
        (use_context, sink_name).

        use_context: "call" | "assign" | "return" | "subscript" | "other"
        sink_name:   callee name when use_context == "call", else ""
        """
        if isinstance(parent, ast.Return):
            return "return", ""
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            return "assign", ""
        if isinstance(parent, ast.Subscript):
            return "subscript", ""
        if isinstance(parent, ast.Call):
            # user_node is passed as a positional or keyword argument
            if use_node in parent.args or any(
                kw.value is use_node for kw in parent.keywords
            ):
                return "call", _extract_call_sink(parent)
        return "other", ""

    @staticmethod
    def _is_inside(needle: ast.AST, haystack: ast.AST) -> bool:
        """True if needle is needle or a descendant of haystack."""
        for n in ast.walk(haystack):
            if n is needle:
                return True
        return False


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def build_defuse(source: str, filename: str = "<string>") -> DefUseGraph:
    """Build and return a DefUseGraph for *source*."""
    return DefUseGraph(source, filename)
