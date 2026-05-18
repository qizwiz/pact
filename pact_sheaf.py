"""
pact_sheaf.py — Sheaf-cohomological checker for LLM response access contracts.

Based on: Young (2026) "Sheaf-Cohomological Program Analysis: Unifying Bug
Finding, Equivalence, and Verification via Čech Cohomology" arXiv:2603.27015

THE KEY IMPROVEMENT OVER failure_mode.py
=========================================
failure_mode._scan_file_llm_response_unguarded tracks a flat _guarded set
per scope — it cannot see guards in called functions.  If a helper does:

    def safe_get(r):
        if not r.choices:
            raise ValueError(...)
        return r.choices[0].message.content

    result = safe_get(response)   ← current checker flags this

the current checker fires a false positive.  The sheaf interprocedural
transport follows the data-flow edge into safe_get, finds the BranchGuard
site there, and propagates the viability predicate back — Ȟ¹ = 0, no bug.

SITE GRAPH
==========
Objects (sites):  CallResult | BranchGuard | ErrorSite | ArgBoundary | OutBoundary
Morphisms:        data_flow edges (variable flows from assignment to use)
                  call edges (callee ArgBoundary ↔ caller variable)

COBOUNDARY MATRIX
=================
∂₀: C⁰(F₂) → C¹(F₂)   one row per morphism, one column per site
(∂₀ σ)[e] = σ[target(e)] ⊕ σ[source(e)]   (disagreement at overlap)

Ȟ¹ rank = dim C¹ − rk ∂₀  (Gaussian elimination over F₂)
       = minimum number of independent fixes needed

Ȟ¹ rank 0 → no violations
Ȟ¹ rank 1 → one guard fixes everything
Ȟ¹ rank k → k independent guards required
"""

from __future__ import annotations

import ast as _ast
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

try:
    import numpy as np

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

# ---------------------------------------------------------------------------
# Site types
# ---------------------------------------------------------------------------


class SiteKind(str, Enum):
    CALL_RESULT = "CallResult"  # r = client.chat.completions.create(...)
    BRANCH_GUARD = "BranchGuard"  # if not r.choices: raise/return
    ERROR_SITE = "ErrorSite"  # r.choices[0], r.content[0]
    ARG_BOUNDARY = "ArgBoundary"  # callee parameter receiving a response var
    OUT_BOUNDARY = "OutBoundary"  # callee return carrying a response var


@dataclass(frozen=True)
class Site:
    kind: SiteKind
    var_name: str  # response variable name at this site
    attr: str  # "choices" / "content" / "" (non-ErrorSites)
    file: str
    line: int
    func: str  # enclosing function name

    @property
    def id(self) -> str:
        return f"{self.kind.value}:{self.func}:{self.var_name}:{self.line}"


@dataclass
class Morphism:
    source_id: str
    target_id: str
    kind: str  # "data_flow" | "call" | "return"


@dataclass
class SiteGraph:
    sites: dict[str, Site] = field(default_factory=dict)
    morphisms: list[Morphism] = field(default_factory=list)

    def add(self, s: Site) -> str:
        self.sites[s.id] = s
        return s.id

    def link(self, src: str, tgt: str, kind: str = "data_flow") -> None:
        if src in self.sites and tgt in self.sites:
            self.morphisms.append(Morphism(src, tgt, kind))

    def error_sites(self) -> list[Site]:
        return [s for s in self.sites.values() if s.kind == SiteKind.ERROR_SITE]

    def guards_for(self, var_name: str) -> list[Site]:
        return [
            s
            for s in self.sites.values()
            if s.kind == SiteKind.BRANCH_GUARD and s.var_name == var_name
        ]

    def reachable_from(self, src_id: str) -> set[str]:
        """BFS over morphisms to find all reachable site ids from src_id."""
        visited: set[str] = set()
        frontier = {src_id}
        while frontier:
            visited |= frontier
            frontier = {
                m.target_id
                for m in self.morphisms
                if m.source_id in frontier and m.target_id not in visited
            }
        return visited


# ---------------------------------------------------------------------------
# Viability predicates (local sections)
# A site's local section is True if the viability predicate holds there.
# ---------------------------------------------------------------------------

_LLM_RESPONSE_SOURCES = frozenset(
    {
        "create",
        "complete",
        "generate",
        "invoke",
        "chat",
        "completions",
        "messages",
    }
)
_LLM_RESPONSE_ATTRS = frozenset({"choices", "content", "outputs", "candidates"})


# ---------------------------------------------------------------------------
# AST harvester — builds SiteGraph from one Python file
# ---------------------------------------------------------------------------


def _harvest_sites(path: str, source: str | None = None) -> SiteGraph:
    """
    Parse a Python file and extract the site graph for LLM response access.

    Handles:
    - CallResult: r = <llm_call>(...)
    - BranchGuard: if [not] r / if not r.choices / if r.choices is None
    - ErrorSite: r.choices[0], r.content[0]
    - ArgBoundary / OutBoundary: function parameters / returns carrying LLM vars
    - data_flow morphisms connecting the above
    """
    try:
        if source is None:
            source = Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return SiteGraph()

    sg = SiteGraph()
    # func_name → {param_name → ArgBoundary site id}
    _param_sites: dict[str, dict[str, str]] = {}
    # func_name → list of return site ids that carry LLM vars
    _return_sites: dict[str, list[str]] = {}

    class _Harvester(_ast.NodeVisitor):
        def __init__(self, func: str, llm_vars: dict[str, str] | None = None):
            self._func = func
            # var_name → CallResult/ArgBoundary site id
            self._llm_vars: dict[str, str] = llm_vars or {}
            # var_name → BranchGuard site id (most recent guard)
            self._guards: dict[str, str] = {}

        def _enter_func(self, func_name: str, params: list[str], lineno: int = 0):
            saved = (self._func, dict(self._llm_vars), dict(self._guards))
            self._func = func_name
            self._llm_vars = {}
            self._guards = {}
            # Register ALL parameters as potential LLM var carriers up front.
            # This ensures visit_If sees them in _llm_vars so BranchGuards fire.
            for p in params:
                site = Site(
                    kind=SiteKind.ARG_BOUNDARY,
                    var_name=p,
                    attr="",
                    file=path,
                    line=lineno,
                    func=func_name,
                )
                sid = sg.add(site)
                _param_sites.setdefault(func_name, {})[p] = sid
                self._llm_vars[p] = sid  # eagerly tracked
            return saved

        def _exit_func(self, saved):
            self._func, self._llm_vars, self._guards = saved

        def _param_names(self, node) -> list[str]:
            return [
                a.arg
                for a in node.args.args + node.args.posonlyargs + node.args.kwonlyargs
            ]

        def visit_FunctionDef(self, node):
            saved = self._enter_func(node.name, self._param_names(node))
            self.generic_visit(node)
            self._exit_func(saved)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], _ast.Name):
                val = node.value
                if isinstance(val, _ast.Call):
                    func = val.func
                    attr = (
                        func.attr
                        if isinstance(func, _ast.Attribute)
                        else (func.id if isinstance(func, _ast.Name) else None)
                    )
                    if attr and attr in _LLM_RESPONSE_SOURCES:
                        var = node.targets[0].id
                        site = Site(
                            kind=SiteKind.CALL_RESULT,
                            var_name=var,
                            attr="",
                            file=path,
                            line=node.lineno,
                            func=self._func,
                        )
                        self._llm_vars[var] = sg.add(site)
            self.generic_visit(node)

        def _note_guard(self, test_src: str, line: int):
            for var, cr_id in self._llm_vars.items():
                if var in test_src and var not in self._guards:
                    site = Site(
                        kind=SiteKind.BRANCH_GUARD,
                        var_name=var,
                        attr="",
                        file=path,
                        line=line,
                        func=self._func,
                    )
                    sid = sg.add(site)
                    sg.link(cr_id, sid, "data_flow")
                    self._guards[var] = sid

        def visit_If(self, node):
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            self._note_guard(src, node.lineno)
            self.generic_visit(node)

        def visit_IfExp(self, node):
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            self._note_guard(src, node.lineno)
            self.generic_visit(node)

        def visit_Subscript(self, node):
            if not isinstance(node.slice, _ast.Constant) or node.slice.value != 0:
                self.generic_visit(node)
                return
            obj = node.value
            if not (
                isinstance(obj, _ast.Attribute) and obj.attr in _LLM_RESPONSE_ATTRS
            ):
                self.generic_visit(node)
                return
            root = obj.value
            var_name = root.id if isinstance(root, _ast.Name) else None
            if var_name is None:
                self.generic_visit(node)
                return

            # Not tracked as LLM var — check if it's a parameter (ArgBoundary)
            if var_name not in self._llm_vars:
                param_sid = _param_sites.get(self._func, {}).get(var_name)
                if param_sid:
                    self._llm_vars[var_name] = param_sid

            if var_name in self._llm_vars:
                site = Site(
                    kind=SiteKind.ERROR_SITE,
                    var_name=var_name,
                    attr=obj.attr,
                    file=path,
                    line=node.lineno,
                    func=self._func,
                )
                sid = sg.add(site)
                # Connect from guard if one exists, else from CallResult/ArgBoundary
                src_id = self._guards.get(var_name, self._llm_vars[var_name])
                sg.link(src_id, sid, "data_flow")
            self.generic_visit(node)

        def visit_Return(self, node):
            if node.value is None:
                return
            src = _ast.unparse(node.value) if hasattr(_ast, "unparse") else ""
            for var, cr_id in self._llm_vars.items():
                if var in src:
                    site = Site(
                        kind=SiteKind.OUT_BOUNDARY,
                        var_name=var,
                        attr="",
                        file=path,
                        line=node.lineno,
                        func=self._func,
                    )
                    sid = sg.add(site)
                    guard_or_cr = self._guards.get(var, cr_id)
                    sg.link(guard_or_cr, sid, "return")
                    _return_sites.setdefault(self._func, []).append(sid)
            self.generic_visit(node)  # must visit children to catch Subscript nodes

    _Harvester("<module>").visit(tree)
    return sg


# ---------------------------------------------------------------------------
# Interprocedural transport
# ---------------------------------------------------------------------------


def _apply_interprocedural_transport(
    sg: SiteGraph,
    path: str,
    all_funcs: dict[str, str] | None = None,
) -> None:
    """
    For each CallResult site, look for call expressions in the same file
    where the LLM var is passed to a known function.  If that function has
    a BranchGuard for the corresponding parameter, add a synthetic guard
    edge in sg — the viability predicate propagates across the call boundary.

    all_funcs: func_name → source_file  (from extractor FunctionManifest index)
               Used to resolve cross-file callees.
    """
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return

    # Build a map: var_name → CallResult site id (for vars in this file)
    cr_map: dict[str, str] = {
        s.var_name: s.id for s in sg.sites.values() if s.kind == SiteKind.CALL_RESULT
    }

    class _CallWalker(_ast.NodeVisitor):
        """Find calls where an LLM var is passed as an argument."""

        def visit_Call(self, node):
            callee = (
                node.func.id
                if isinstance(node.func, _ast.Name)
                else node.func.attr if isinstance(node.func, _ast.Attribute) else None
            )
            if callee is None:
                self.generic_visit(node)
                return
            for arg_idx, arg in enumerate(node.args):
                var_name = arg.id if isinstance(arg, _ast.Name) else None
                if var_name and var_name in cr_map:
                    _check_callee_guards(callee, arg_idx, var_name, node.lineno)
            self.generic_visit(node)

    def _check_callee_guards(
        callee_name: str,
        arg_idx: int,
        caller_var: str,
        call_line: int,
    ) -> None:
        """
        If callee_name is defined in the same file and has a BranchGuard
        for the parameter at arg_idx, add a synthetic BranchGuard site in sg
        connected to the caller's CallResult.
        """
        # Find callee function def in the tree
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if node.name != callee_name:
                continue
            params = [a.arg for a in node.args.args + node.args.posonlyargs]
            if arg_idx >= len(params):
                continue
            param_name = params[arg_idx]

            # Harvest the callee's body to find guards for this parameter
            callee_sg = _harvest_sites(
                path, source=_ast.unparse(node) if hasattr(_ast, "unparse") else source
            )

            has_guard = any(
                s.kind == SiteKind.BRANCH_GUARD and s.var_name == param_name
                for s in callee_sg.sites.values()
            )

            if has_guard:
                # Transport: add a synthetic BranchGuard in the caller's scope
                synthetic = Site(
                    kind=SiteKind.BRANCH_GUARD,
                    var_name=caller_var,
                    attr="",
                    file=path,
                    line=call_line,
                    func=f"<via {callee_name}>",
                )
                sid = sg.add(synthetic)
                sg.link(cr_map[caller_var], sid, "call")
                # Connect all ErrorSites for this var to the synthetic guard
                for es in sg.error_sites():
                    if es.var_name == caller_var:
                        sg.link(sid, es.id, "call")

    _CallWalker().visit(tree)


def _enclosing_func(tree: "_ast.Module", lineno: int) -> str:
    """Return the innermost function name enclosing the given line."""
    best, best_line = "<module>", 0
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno + 9999)
            if node.lineno <= lineno <= end and node.lineno > best_line:
                best, best_line = node.name, node.lineno
    return best


def _apply_caller_to_callee_transport(
    sg: SiteGraph,
    path: str,
    source: str,
    tree: "_ast.Module",
) -> None:
    """
    Reverse direction of interprocedural transport.

    Phase 1 (existing): callee guards param → caller's CallResult is safe.
    Phase 2 (this):     caller guards var before call → callee's ArgBoundary is safe.

    For each function F that has unguarded ArgBoundary-sourced ErrorSites, find
    callers of F in this file.  If the caller guards the variable before passing
    it, inject a synthetic BranchGuard into F so the ErrorSites are no longer
    unguarded.
    """
    # Collect (callee_func, param_name) pairs with unguarded ArgBoundary ErrorSites
    needs: dict[tuple[str, str], list[str]] = {}
    for es in sg.error_sites():
        if any(es.id in sg.reachable_from(g.id) for g in sg.guards_for(es.var_name)):
            continue  # already guarded
        ab = next(
            (
                s
                for s in sg.sites.values()
                if s.kind == SiteKind.ARG_BOUNDARY
                and s.var_name == es.var_name
                and s.func == es.func
            ),
            None,
        )
        if ab:
            needs.setdefault((es.func, es.var_name), []).append(es.id)

    if not needs:
        return

    for (callee_func, param_name), es_ids in needs.items():
        # Resolve parameter index in callee
        callee_node = next(
            (
                n
                for n in _ast.walk(tree)
                if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and n.name == callee_func
            ),
            None,
        )
        if not callee_node:
            continue
        params = [a.arg for a in callee_node.args.args + callee_node.args.posonlyargs]
        try:
            param_idx = params.index(param_name)
        except ValueError:
            continue

        # Find call sites for callee_func and check caller guards
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            callee_name = (
                node.func.id
                if isinstance(node.func, _ast.Name)
                else node.func.attr if isinstance(node.func, _ast.Attribute) else None
            )
            if callee_name != callee_func or param_idx >= len(node.args):
                continue
            arg = node.args[param_idx]
            caller_var = arg.id if isinstance(arg, _ast.Name) else None
            if not caller_var:
                continue

            caller_func = _enclosing_func(tree, node.lineno)
            # Guard must be in the same caller function and before this call
            caller_guards = [
                g
                for g in sg.guards_for(caller_var)
                if g.func == caller_func and g.line < node.lineno
            ]
            if not caller_guards:
                continue

            ab_site = next(
                (
                    s
                    for s in sg.sites.values()
                    if s.kind == SiteKind.ARG_BOUNDARY
                    and s.var_name == param_name
                    and s.func == callee_func
                ),
                None,
            )
            if not ab_site:
                continue

            synthetic = Site(
                kind=SiteKind.BRANCH_GUARD,
                var_name=param_name,
                attr="",
                file=path,
                line=node.lineno,
                func=f"<caller:{caller_func}>",
            )
            sid = sg.add(synthetic)
            sg.link(ab_site.id, sid, "call")
            for es_id in es_ids:
                sg.link(sid, es_id, "call")


def _apply_cross_file_transport(
    sg: SiteGraph,
    path: str,
    call_graph: object,  # graphify_graph.CallGraph
) -> None:
    """
    Cross-file interprocedural transport using the graphify call graph.

    For each unguarded ArgBoundary-sourced ErrorSite in function F, look up
    callers of F via graphify.  For each cross-file caller G (file Y), harvest
    Y's site graph and check if G guards the argument before passing it to F.
    If yes, inject a synthetic BranchGuard in F.

    This extends _apply_caller_to_callee_transport to cross-file call edges.
    """
    try:
        from pathlib import Path as _Path

        cg = call_graph  # type: ignore[assignment]
    except Exception:
        return

    # Collect unguarded ArgBoundary ErrorSites grouped by (func, param)
    needs: dict[tuple[str, str], list[str]] = {}
    for es in sg.error_sites():
        if any(es.id in sg.reachable_from(g.id) for g in sg.guards_for(es.var_name)):
            continue
        ab = next(
            (
                s
                for s in sg.sites.values()
                if s.kind == SiteKind.ARG_BOUNDARY
                and s.var_name == es.var_name
                and s.func == es.func
            ),
            None,
        )
        if ab:
            needs.setdefault((es.func, es.var_name), []).append(es.id)

    if not needs:
        return

    for (callee_func, param_name), es_ids in needs.items():
        # Get callers from graphify
        caller_annotations = cg.callers_of(callee_func, path)
        for annotation in caller_annotations:
            # annotation: "file:loc  caller_name"
            parts = annotation.split()
            if not parts:
                continue
            caller_name = parts[-1]
            addr = parts[0] if len(parts) > 1 else ""
            caller_file = addr.split(":")[0] if ":" in addr else ""
            if not caller_file or caller_file == path:
                continue  # same-file handled by _apply_caller_to_callee_transport

            try:
                caller_sg = _harvest_sites(caller_file)
            except Exception:
                continue

            # Find guards for any variable passed to callee_func at a call site
            # in caller_file that corresponds to param_name
            try:
                caller_source = _Path(caller_file).read_text(
                    encoding="utf-8", errors="replace"
                )
                caller_tree = _ast.parse(caller_source)
            except (SyntaxError, OSError):
                continue

            # Find the param index in callee
            callee_node = None
            try:
                callee_source = _Path(path).read_text(
                    encoding="utf-8", errors="replace"
                )
                callee_tree = _ast.parse(callee_source)
                callee_node = next(
                    (
                        n
                        for n in _ast.walk(callee_tree)
                        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                        and n.name == callee_func
                    ),
                    None,
                )
            except (SyntaxError, OSError):
                pass

            if not callee_node:
                continue
            params = [
                a.arg for a in callee_node.args.args + callee_node.args.posonlyargs
            ]
            try:
                param_idx = params.index(param_name)
            except ValueError:
                continue

            for node in _ast.walk(caller_tree):
                if not isinstance(node, _ast.Call):
                    continue
                cn = (
                    node.func.id
                    if isinstance(node.func, _ast.Name)
                    else (
                        node.func.attr
                        if isinstance(node.func, _ast.Attribute)
                        else None
                    )
                )
                if cn != callee_func or param_idx >= len(node.args):
                    continue
                arg = node.args[param_idx]
                caller_var = arg.id if isinstance(arg, _ast.Name) else None
                if not caller_var:
                    continue

                enclosing = _enclosing_func(caller_tree, node.lineno)
                cross_guards = [
                    g
                    for g in caller_sg.guards_for(caller_var)
                    if g.func == enclosing and g.line < node.lineno
                ]
                if not cross_guards:
                    continue

                ab_site = next(
                    (
                        s
                        for s in sg.sites.values()
                        if s.kind == SiteKind.ARG_BOUNDARY
                        and s.var_name == param_name
                        and s.func == callee_func
                    ),
                    None,
                )
                if not ab_site:
                    continue

                synthetic = Site(
                    kind=SiteKind.BRANCH_GUARD,
                    var_name=param_name,
                    attr="",
                    file=caller_file,
                    line=node.lineno,
                    func=f"<cross-file:{caller_name}>",
                )
                sid = sg.add(synthetic)
                sg.link(ab_site.id, sid, "call")
                for es_id in es_ids:
                    sg.link(sid, es_id, "call")


# ---------------------------------------------------------------------------
# Coboundary matrix and Ȟ¹ rank
# ---------------------------------------------------------------------------


def _coboundary_matrix_f2(sg: SiteGraph) -> Optional["np.ndarray"]:
    """
    ∂₀: C⁰(F₂) → C¹(F₂)

    Row per morphism, column per site.
    ∂₀[e, v] = 1 if v is source or target of e (XOR boundary over F₂).
    """
    if not _HAS_NUMPY or not sg.sites or not sg.morphisms:
        return None
    site_ids = list(sg.sites)
    idx = {sid: i for i, sid in enumerate(site_ids)}
    mat = np.zeros((len(sg.morphisms), len(site_ids)), dtype=np.uint8)
    for row, m in enumerate(sg.morphisms):
        if m.source_id in idx:
            mat[row, idx[m.source_id]] = 1
        if m.target_id in idx:
            mat[row, idx[m.target_id]] = 1
    return mat


def _h1_rank_f2(mat: "np.ndarray") -> int:
    """
    rk Ȟ¹ = dim C¹ − rk ∂₀  over F₂.
    Gaussian elimination over GF(2).
    (Used for topology visualisation — see _h1_rank_semantic for the
    semantically meaningful violation count.)
    """
    m, n = mat.shape
    a = mat.copy()
    pivot_row = 0
    for col in range(n):
        pivot = next((r for r in range(pivot_row, m) if a[r, col]), None)
        if pivot is None:
            continue
        a[[pivot_row, pivot]] = a[[pivot, pivot_row]]
        for r in range(m):
            if r != pivot_row and a[r, col]:
                a[r] = (a[r] + a[pivot_row]) % 2
        pivot_row += 1
    return m - pivot_row  # dim C¹ − rk ∂₀


def _h1_rank_semantic(sg: SiteGraph) -> int:
    """
    Semantic Ȟ¹ rank: minimum number of independent fixes needed.

    Two ErrorSites are in the same Ȟ¹ class when they share a CallResult —
    a single guard added just after that call fixes both simultaneously.
    The rank counts how many independent CallResult (or ArgBoundary) sources
    have at least one unguarded ErrorSite downstream.

    Concretely: rank 0 = clean; rank k = k independent guards needed.
    """
    unguarded_sources: set[str] = set()
    for es in sg.error_sites():
        guards = sg.guards_for(es.var_name)
        guarded = any(es.id in sg.reachable_from(g.id) for g in guards)
        if not guarded:
            # Find the upstream source (CallResult or ArgBoundary)
            source = next(
                (
                    s
                    for s in sg.sites.values()
                    if s.kind in (SiteKind.CALL_RESULT, SiteKind.ARG_BOUNDARY)
                    and s.var_name == es.var_name
                    and s.func == es.func
                ),
                None,
            )
            key = source.id if source else f"unknown:{es.func}:{es.var_name}"
            unguarded_sources.add(key)
    return len(unguarded_sources)


# ---------------------------------------------------------------------------
# Violation type
# ---------------------------------------------------------------------------


@dataclass
class SheafViolation:
    file: str
    line: int
    func: str
    var_name: str
    attr: str
    h1_rank: int  # minimum independent fixes needed
    guarded: bool  # True = sheaf is clean (no violation)
    interprocedural: bool  # guard was found across a call boundary
    spec_id: str | None = None

    def __str__(self) -> str:
        spec = f"  [spec: {self.spec_id}]" if self.spec_id else ""
        rank = f"  [Ȟ¹={self.h1_rank}]"
        xp = "  [interprocedural guard]" if self.interprocedural else ""
        return (
            f"{self.file}:{self.line}  {self.var_name}.{self.attr}[0]"
            f"{rank}{xp}{spec}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_file(
    path: str,
    *,
    interprocedural: bool = True,
) -> list[SheafViolation]:
    """
    Run the sheaf-cohomological check on a Python file.

    Returns a SheafViolation per ErrorSite.  Violations with guarded=True
    are clean (Ȟ¹=0 at that site); guarded=False are bugs.

    Parameters
    ----------
    path:
        Python source file to analyse.
    interprocedural:
        If True, follow call edges for guard transport (default).
        Set False to get intra-procedural-only results (for comparison).
    """
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    sg = _harvest_sites(path, source=source)

    if interprocedural:
        _apply_interprocedural_transport(sg, path)
        _apply_caller_to_callee_transport(sg, path, source, tree)

    global_h1 = _h1_rank_semantic(sg)

    violations: list[SheafViolation] = []
    for es in sg.error_sites():
        # Is this ErrorSite reachable from a BranchGuard?
        guards = sg.guards_for(es.var_name)
        guarded = any(es.id in sg.reachable_from(g.id) for g in guards)

        interprocedural_guard = guarded and any(
            g.func.startswith("<via ")
            for g in guards
            if es.id in sg.reachable_from(g.id)
        )

        spec_id = (
            "openai-chat#choices-nonempty"
            if es.attr == "choices"
            else "anthropic-messages#content-nonempty" if es.attr == "content" else None
        )

        if not guarded:
            violations.append(
                SheafViolation(
                    file=es.file,
                    line=es.line,
                    func=es.func,
                    var_name=es.var_name,
                    attr=es.attr,
                    h1_rank=global_h1,
                    guarded=False,
                    interprocedural=interprocedural_guard,
                    spec_id=spec_id,
                )
            )
        # guarded=True sites are not violations — but we could report them
        # as "confirmed safe" for --verbose mode

    return violations


def h1_rank_for_file(path: str) -> int:
    """
    Return the semantic Ȟ¹ rank for a file's LLM response site graph.

    0 → all accesses are guarded (or no LLM accesses exist)
    k → k independent guards are needed (one per independent CallResult source)
    """
    sg = _harvest_sites(path)
    _apply_interprocedural_transport(sg, path)
    return _h1_rank_semantic(sg)
