"""
Z3 Fixedpoint engine for pact.

Program analysis as Datalog: Python extracts facts, Z3 derives violations.

Schema — model_constraint
-------------------------
EDB:
  site_creates(site, model)       call site is Model.objects.create()
  site_provides(site, field)      call site passes this kwarg
  model_req(model, field)         model requires this field

IDB (derived by Z3 in two strata):
  site_req(site, field)           = join: site creates a model that requires this field
  violation(site, field)          = site_req but not site_provides

Two rules, no existential variables in negated atoms:
  site_req(S, F) :- site_creates(S, M), model_req(M, F)
  violation(S, F) :- site_req(S, F), not site_provides(S, F)

Schema — llm_response_unguarded
--------------------------------
EDB:
  llm_var(scope, var)             scope has var assigned from LLM-returning call
  unguarded_access(scope, var)    var.choices[0]/etc accessed without guard in scope

IDB (one rule, no negation):
  llm_violation(S, V) :- llm_var(S, V), unguarded_access(S, V)

UNSAT → proved safe (no violations exist in any scope).
SAT   → get_answer() contains (scope, var) witness pairs → concrete crash sites.

Python adds facts. Z3 reasons. One query. All violations.

Implementation notes
--------------------
- Z3 datalog engine requires FINITE sorts — use BitVecSort, not IntSort.
- Rules must be in ForAll(vars, Implies(body, head)) form.
- Query with Exists([qs, qf], violation(qs, qf)).
- get_answer() returns a formula in de Bruijn Var notation:
    Var(0) = qs = first arg (site), Var(1) = qf = second arg (field)
  (Z3 binds Exists vars right-to-left: last in list → Var(0))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import z3 as _z3
from z3 import (
    And,
    BitVecSort,
    BitVecVal,
    BitVecs,
    BoolSort,
    Exists,
    Fixedpoint,
    ForAll,
    Function,
    Implies,
    Not,
    is_and,
    is_eq,
    is_false,
    is_or,
    sat,
    unknown,
    unsat,
)

from .extractor import extract_from_codebase, iter_python_files

_ctx = _z3.main_ctx()


def _is_var(expr: _z3.ExprRef) -> bool:
    return _z3.Z3_get_ast_kind(_ctx.ref(), expr.as_ast()) == _z3.Z3_VAR_AST


def _var_idx(expr: _z3.ExprRef) -> int:
    return _z3.Z3_get_index_value(_ctx.ref(), expr.as_ast())


def _extract_tuples(formula: _z3.BoolRef) -> list[tuple[int, int]]:
    """Parse Or(And(Var(0)==si, Var(1)==fi), ...) → [(si, fi), ...].

    Z3's datalog get_answer() returns a DNF formula where each clause
    is one violation tuple. This walks the formula in O(|violations|).
    """
    if is_false(formula):
        return []
    clauses = formula.children() if is_or(formula) else [formula]
    tuples: list[tuple[int, int]] = []
    for clause in clauses:
        if not is_and(clause):
            continue
        vals: dict[int, int] = {}
        for eq in clause.children():
            if not is_eq(eq):
                continue
            lhs, rhs = eq.children()
            if _is_var(lhs):
                vals[_var_idx(lhs)] = rhs.as_long()
            elif _is_var(rhs):
                vals[_var_idx(rhs)] = lhs.as_long()
        if 0 in vals and 1 in vals:
            tuples.append((vals[0], vals[1]))  # (site, field)
    return tuples


# Domain size: supports up to 2^BITS distinct IDs per intern table.
# 16 bits = 65536 — more than enough for any codebase.
_BITS = 16
_BVS = BitVecSort(_BITS)


def _bv(n: int):
    return BitVecVal(n, _BITS)


# ── EDB ───────────────────────────────────────────────────────────────────────
site_creates = Function("site_creates", _BVS, _BVS, BoolSort())
site_provides = Function("site_provides", _BVS, _BVS, BoolSort())
model_req = Function("model_req", _BVS, _BVS, BoolSort())

# ── IDB ───────────────────────────────────────────────────────────────────────
site_req = Function("site_req", _BVS, _BVS, BoolSort())  # derived join
violation = Function("violation", _BVS, _BVS, BoolSort())  # the violations


@dataclass
class Z3Violation:
    file: str
    line: int
    call: str
    missing: list[str] = field(default_factory=list)
    context: str = "model_constraint"

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line}  {self.call}  missing: {', '.join(self.missing)}"
        )


class PactEngine:
    """
    Loads a codebase as a Datalog database.
    Z3's Fixedpoint derives all violations — Python just reads the results.
    """

    def __init__(self) -> None:
        self._fp = Fixedpoint()
        self._fp.set(engine="datalog")
        for rel in (site_creates, site_provides, model_req, site_req, violation):
            self._fp.register_relation(rel)

        # Stratum 1: materialize the join (no negation, existential _m is fine here)
        # site_req(S, F) :- site_creates(S, M), model_req(M, F)
        _s, _m, _f = BitVecs("_s _m _f", _BITS)
        self._fp.add_rule(
            ForAll(
                [_s, _m, _f],
                Implies(And(site_creates(_s, _m), model_req(_m, _f)), site_req(_s, _f)),
            )
        )

        # Stratum 2: negate site_provides — now no existential in the negated atom
        # violation(S, F) :- site_req(S, F), not site_provides(S, F)
        _s2, _f2 = BitVecs("_s2 _f2", _BITS)
        self._fp.add_rule(
            ForAll(
                [_s2, _f2],
                Implies(
                    And(site_req(_s2, _f2), Not(site_provides(_s2, _f2))),
                    violation(_s2, _f2),
                ),
            )
        )

        # Intern tables
        self._site_id: dict[tuple, int] = {}
        self._field_id: dict[str, int] = {}
        self._model_id: dict[str, int] = {}
        self._site_meta: dict[int, dict] = {}

    def _sid(self, file: str, line: int, call: str) -> int:
        key = (file, line)
        if key not in self._site_id:
            n = len(self._site_id)
            self._site_id[key] = n
            self._site_meta[n] = {"file": file, "line": line, "call": call}
        return self._site_id[key]

    def _fid(self, name: str) -> int:
        if name not in self._field_id:
            self._field_id[name] = len(self._field_id)
        return self._field_id[name]

    def _mid(self, name: str) -> int:
        if name not in self._model_id:
            self._model_id[name] = len(self._model_id)
        return self._model_id[name]

    def load(self, root: Path) -> None:
        """Extract AST facts → Z3 Fixedpoint database."""
        models, _funcs, calls = extract_from_codebase(root)

        for model in models:
            mi = self._mid(model.name)
            for fc in model.required_fields:
                fi = self._fid(fc.name)
                self._fp.add_rule(model_req(_bv(mi), _bv(fi)))

        for call in calls:
            if not call.is_create_call or not call.model_name:
                continue
            si = self._sid(call.file, call.line, call.callee_name)
            mi = self._mid(call.model_name)
            self._fp.add_rule(site_creates(_bv(si), _bv(mi)))
            for kwarg in call.provided_kwargs:
                fi = self._fid(kwarg)
                self._fp.add_rule(site_provides(_bv(si), _bv(fi)))

    def violations(self) -> list[Z3Violation]:
        """One query. Z3 derives all violations."""
        import warnings as _warnings

        qs, qf = BitVecs("qs qf", _BITS)
        result = self._fp.query(Exists([qs, qf], violation(qs, qf)))
        if result == unsat:
            return []
        if result == unknown:
            _warnings.warn(
                "z3_engine: fixedpoint query returned UNKNOWN — engine gave up; "
                "violations list is empty but the codebase may not be safe",
                RuntimeWarning,
                stacklevel=2,
            )
            return []
        if result != sat:
            return []

        # get_answer() returns a DNF formula — one clause per violation tuple.
        # _extract_tuples() reads it in O(|violations|) instead of O(|sites|×|fields|).
        tuples = _extract_tuples(self._fp.get_answer())
        field_names = {v: k for k, v in self._field_id.items()}

        by_site: dict[int, list[str]] = {}
        for si, fi in tuples:
            fname = field_names.get(fi)
            if fname is not None and si in self._site_meta:
                by_site.setdefault(si, []).append(fname)

        return [
            Z3Violation(
                file=self._site_meta[si]["file"],
                line=self._site_meta[si]["line"],
                call=self._site_meta[si]["call"],
                missing=missing,
            )
            for si, missing in by_site.items()
        ]


def run(root: Path) -> list[Z3Violation]:
    engine = PactEngine()
    engine.load(root)
    return engine.violations()


# ── LLM Response Guard engine ─────────────────────────────────────────────────
# Separate Fixedpoint instance: function names shadow module-level relations
# if reused, so each engine class owns its own Z3 relations.

_llm_var_rel = Function("llm_var", _BVS, _BVS, BoolSort())
_unguarded_access_rel = Function("unguarded_access", _BVS, _BVS, BoolSort())
_llm_violation_rel = Function("llm_violation", _BVS, _BVS, BoolSort())

# These must match failure_mode._LLM_RESPONSE_SOURCES / _LLM_RESPONSE_ATTRS
_LLM_CALL_ATTRS = frozenset(
    {
        # Synchronous methods (original set)
        "create",
        "complete",
        "generate",
        "invoke",
        "chat",
        "completions",
        "messages",
        # Async variants (OpenAI SDK, Anthropic SDK, etc.)
        "acreate",
        "agenerate",
        "ainvoke",
        "astream",
        "astream_events",
        # Streaming (synchronous)
        "stream",
        "stream_complete",
        # Generic callable / provider-agnostic
        "run",
        "__call__",
        # Google Vertex / Gemini
        "predict",
        "count_tokens",
    }
)
_LLM_LIST_ATTRS = frozenset({"choices", "content", "outputs", "candidates"})


@dataclass
class LLMProofResult:
    """Result of a Z3 llm_response_unguarded proof attempt."""

    proved_safe: bool
    violations: list[Z3Violation] = field(default_factory=list)
    scopes_analyzed: int = 0

    def __str__(self) -> str:
        if self.proved_safe:
            return (
                f"SAFE: llm_response_unguarded — Z3 proved no violations "
                f"across {self.scopes_analyzed} scope(s)"
            )
        return (
            f"UNSAFE: llm_response_unguarded — {len(self.violations)} violation(s) "
            f"(Z3 witness)"
        )


def _extract_llm_facts(
    path: str,
    scope_intern: dict[str, int],
    var_intern: dict[str, int],
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Extract (llm_var_facts, unguarded_access_facts) from one Python file.

    Runs the same sequential guard-tracking logic as the AST scanner so
    Z3 reasons over facts that are in 1-to-1 correspondence with scanner
    violations — UNSAT is a formal certificate, not a guess.
    """
    import ast as _ast
    from pathlib import Path as _Path

    try:
        source = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError) as exc:
        import warnings as _warnings

        _warnings.warn(
            f"z3_engine: skipping {path} ({type(exc).__name__}: {exc}); "
            "UNSAT result will not cover this file",
            RuntimeWarning,
            stacklevel=2,
        )
        return [], []

    llm_var_facts: list[tuple[int, int]] = []
    unguarded_access_facts: list[tuple[int, int]] = []

    def _sid(func_name: str) -> int:
        key = f"{path}::{func_name}"
        if key not in scope_intern:
            scope_intern[key] = len(scope_intern)
        return scope_intern[key]

    def _vid(name: str) -> int:
        if name not in var_intern:
            var_intern[name] = len(var_intern)
        return var_intern[name]

    class _Visitor(_ast.NodeVisitor):
        def __init__(self):
            self._scope = "<module>"
            self._llm_vars: dict[str, int] = {}
            self._guarded: set[str] = set()
            self._flagged: set[tuple[str, str]] = set()

        def _enter(self, name: str):
            saved = (
                self._scope,
                dict(self._llm_vars),
                set(self._guarded),
                set(self._flagged),
            )
            self._scope = name
            self._llm_vars = {}
            self._guarded = set()
            self._flagged = set()
            return saved

        def _exit(self, saved):
            self._scope, self._llm_vars, self._guarded, self._flagged = saved

        def visit_FunctionDef(self, node):
            saved = self._enter(node.name)
            self.generic_visit(node)
            self._exit(saved)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], _ast.Name):
                # Unwrap `await expr` so async assignments are treated the same
                # as synchronous ones: `response = await client.acreate(...)`
                rhs = node.value
                if isinstance(rhs, _ast.Await):
                    rhs = rhs.value
                if isinstance(rhs, _ast.Call):
                    func = rhs.func
                    attr = (
                        func.attr
                        if isinstance(func, _ast.Attribute)
                        else func.id if isinstance(func, _ast.Name) else None
                    )
                    if attr and attr in _LLM_CALL_ATTRS:
                        var = node.targets[0].id
                        self._llm_vars[var] = node.lineno
                        llm_var_facts.append((_sid(self._scope), _vid(var)))
            self.generic_visit(node)

        def visit_If(self, node):
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            for var in list(self._llm_vars):
                if var in src:
                    self._guarded.add(var)
            self.generic_visit(node)

        def visit_IfExp(self, node):
            self.visit(node.test)
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            newly: set[str] = set()
            for var in list(self._llm_vars):
                if var in src and var not in self._guarded:
                    self._guarded.add(var)
                    newly.add(var)
            self.visit(node.body)
            for var in newly:
                self._guarded.discard(var)
            self.visit(node.orelse)

        def visit_Subscript(self, node):
            if (
                isinstance(node.slice, _ast.Constant)
                and node.slice.value == 0
                and isinstance(node.value, _ast.Attribute)
                and node.value.attr in _LLM_LIST_ATTRS
                and isinstance(node.value.value, _ast.Name)
            ):
                var = node.value.value.id
                pair = (var, node.value.attr)
                if (
                    var in self._llm_vars
                    and var not in self._guarded
                    and pair not in self._flagged
                ):
                    self._flagged.add(pair)
                    unguarded_access_facts.append((_sid(self._scope), _vid(var)))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return llm_var_facts, unguarded_access_facts


class LLMResponseEngine:
    """
    Z3 Fixedpoint verifier for llm_response_unguarded.

    Schema:
      EDB: llm_var(scope, var), unguarded_access(scope, var)
      IDB: llm_violation(S, V) :- llm_var(S, V), unguarded_access(S, V)

    SAT  → violations exist; get_answer() yields (scope, var) witnesses.
    UNSAT → proved safe — no scope contains both an LLM assignment and an
            unguarded access.  This is a mathematical certificate, not heuristic.
    """

    def __init__(self) -> None:
        self._fp = Fixedpoint()
        self._fp.set(engine="datalog")
        for rel in (_llm_var_rel, _unguarded_access_rel, _llm_violation_rel):
            self._fp.register_relation(rel)

        _sv, _vv = BitVecs("_sv _vv", _BITS)
        self._fp.add_rule(
            ForAll(
                [_sv, _vv],
                Implies(
                    And(_llm_var_rel(_sv, _vv), _unguarded_access_rel(_sv, _vv)),
                    _llm_violation_rel(_sv, _vv),
                ),
            )
        )

        self._scope_intern: dict[str, int] = {}
        self._var_intern: dict[str, int] = {}
        self._scope_meta: dict[int, str] = {}
        self._scopes_with_llm: int = 0

    def load(self, root: Path) -> None:
        """Extract facts from all Python files under root and populate Z3."""
        all_lv: list[tuple[int, int]] = []
        all_ua: list[tuple[int, int]] = []

        for path in iter_python_files(root):
            lv, ua = _extract_llm_facts(str(path), self._scope_intern, self._var_intern)
            all_lv.extend(lv)
            all_ua.extend(ua)

        id_to_key = {v: k for k, v in self._scope_intern.items()}

        for sid, vid in all_lv:
            self._fp.add_rule(_llm_var_rel(_bv(sid), _bv(vid)))
            self._scope_meta[sid] = id_to_key[sid]

        for sid, vid in all_ua:
            self._fp.add_rule(_unguarded_access_rel(_bv(sid), _bv(vid)))
            self._scope_meta[sid] = id_to_key[sid]

        self._scopes_with_llm = len({sid for sid, _ in all_lv})

    def result(self) -> LLMProofResult:
        """Query Z3. Returns proved-safe result or list of violation witnesses."""
        qs, qv = BitVecs("qs qv", _BITS)
        z3_result = self._fp.query(Exists([qs, qv], _llm_violation_rel(qs, qv)))

        if z3_result == unsat:
            return LLMProofResult(
                proved_safe=True, scopes_analyzed=self._scopes_with_llm
            )
        if z3_result == unknown:
            import warnings as _warnings

            _warnings.warn(
                "z3_engine: llm_response_unguarded query returned UNKNOWN — "
                "proved_safe=True is asserted but may not hold",
                RuntimeWarning,
                stacklevel=2,
            )
            return LLMProofResult(
                proved_safe=True, scopes_analyzed=self._scopes_with_llm
            )
        if z3_result != sat:
            return LLMProofResult(
                proved_safe=True, scopes_analyzed=self._scopes_with_llm
            )

        tuples = _extract_tuples(self._fp.get_answer())
        var_names = {v: k for k, v in self._var_intern.items()}

        violations: list[Z3Violation] = []
        for si, vi in tuples:
            scope_key = self._scope_meta.get(si, "")
            var_name = var_names.get(vi, f"?var_{vi}")
            file_part, _, func_part = scope_key.partition("::")
            violations.append(
                Z3Violation(
                    file=file_part,
                    line=0,
                    call=f"{var_name}.choices[0]",
                    context="llm_response_unguarded",
                )
            )
        return LLMProofResult(
            proved_safe=False,
            violations=violations,
            scopes_analyzed=self._scopes_with_llm,
        )


def run_llm(root: Path) -> LLMProofResult:
    """Load and query in one call."""
    engine = LLMResponseEngine()
    engine.load(root)
    return engine.result()


def verify_file(path: str) -> LLMProofResult:
    """Return an LLMProofResult for a single Python file.

    Use this to formally verify that a fixer patch has removed all
    llm_response_unguarded violations from a specific file.  Unlike
    ``run_llm(root)``, which loads an entire project, this function
    analyzes one file in isolation — suitable for per-patch certification.

    ``result.proved_safe`` is True iff Z3 found no (scope, var) pair
    where an LLM-assigned variable is accessed without a guard.
    """
    engine = LLMResponseEngine()
    lv, ua = _extract_llm_facts(path, engine._scope_intern, engine._var_intern)

    id_to_key = {v: k for k, v in engine._scope_intern.items()}
    for sid, vid in lv:
        engine._fp.add_rule(_llm_var_rel(_bv(sid), _bv(vid)))
        engine._scope_meta[sid] = id_to_key.get(sid, str(sid))
    for sid, vid in ua:
        engine._fp.add_rule(_unguarded_access_rel(_bv(sid), _bv(vid)))
        engine._scope_meta[sid] = id_to_key.get(sid, str(sid))

    engine._scopes_with_llm = len({sid for sid, _ in lv})
    return engine.result()
