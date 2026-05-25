"""
pact_interproc -- interprocedural Z3 analysis for whole-codebase invariant checking.

Extends pact's per-function checker to find violations that only manifest
across module boundaries, using Z3 Fixedpoint (CHC/Datalog) to propagate
guard requirements through the call graph.

Schema
------
EDB (extracted from AST):
  json_unguarded(func_id)       function directly calls json.loads without guard
  subprocess_unchecked(func_id) function calls subprocess.run without checking returncode
  api_key_unchecked(func_id)    function calls _call/Anthropic() without key guard
  calls(caller_id, callee_id)   call graph edge (within the codebase)
  is_public(func_id)            function is module-level (not _private) and exported

IDB (derived by Z3):
  tainted_json(F)     :- json_unguarded(F)
  tainted_json(F)     :- calls(F, G), tainted_json(G), not try_wraps_json(F)
  violation_json(F)   :- tainted_json(F), is_public(F)

  tainted_sub(F)      :- subprocess_unchecked(F)
  tainted_sub(F)      :- calls(F, G), tainted_sub(G)
  violation_sub(F)    :- tainted_sub(F), is_public(F)

Usage:
    python -m pact.pact_interproc [<dir>] [--json] [--verbose]
    from pact.pact_interproc import analyze_codebase
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import z3
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
)

# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


def _func_id(file_path: Path, func_name: str) -> str:
    return f"{file_path.stem}::{func_name}"


def _is_guarded_json_loads(node: ast.Call, tree: ast.AST) -> bool:
    """True if this json.loads call is inside a try/except json.JSONDecodeError."""
    for parent in ast.walk(tree):
        if isinstance(parent, ast.ExceptHandler):
            if parent.type is not None:
                exc_name = (
                    parent.type.id
                    if isinstance(parent.type, ast.Name)
                    else (
                        parent.type.attr
                        if isinstance(parent.type, ast.Attribute)
                        else ""
                    )
                )
                if exc_name in ("JSONDecodeError", "ValueError", "Exception"):
                    # Check if node is inside this handler's try block
                    for child in ast.walk(parent):
                        if child is node:
                            return True
    return False


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _in_try_except(
    node: ast.AST, parents: dict[ast.AST, ast.AST], exc_names: set[str]
) -> bool:
    """Walk up the parent chain to see if node is inside a try/except matching exc_names."""
    cur = node
    while cur in parents:
        parent = parents[cur]
        if isinstance(parent, ast.Try):
            for handler in parent.handlers:
                if handler.type is None:
                    return True
                names = []
                if isinstance(handler.type, ast.Tuple):
                    for elt in handler.type.elts:
                        names.append(
                            elt.id
                            if isinstance(elt, ast.Name)
                            else (elt.attr if isinstance(elt, ast.Attribute) else "")
                        )
                elif isinstance(handler.type, ast.Name):
                    names.append(handler.type.id)
                elif isinstance(handler.type, ast.Attribute):
                    names.append(handler.type.attr)
                if exc_names & set(names):
                    return True
        cur = parent
    return False


def _enclosing_func(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> Optional[str]:
    cur = node
    while cur in parents:
        parent = parents[cur]
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
        cur = parent
    return None  # module-level code → use "<module>"


def _file_import_map(file_path: Path, codebase_stems: set[str]) -> dict[str, str]:
    """
    Parse file_path's imports and return {local_name: source_stem} for names
    imported from other files that are in the codebase.

    Handles:
      from .module import name          → {name: module}
      from .module import name as alias → {alias: module}
      from module import name           → {name: module}  (if module in codebase)
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return {}

    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module is None:
            continue
        # For relative imports, the module is relative to the current package.
        # We take just the last component as the stem to match filenames.
        module_stem = node.module.split(".")[-1]
        if module_stem not in codebase_stems:
            continue
        for alias in node.names:
            local = alias.asname if alias.asname else alias.name
            result[local] = module_stem
    return result


@dataclass
class FuncFacts:
    func_id: str
    file: str
    func_name: str
    json_unguarded: bool = False
    subprocess_unchecked: bool = False
    api_key_unchecked: bool = False
    try_wraps_json: bool = False  # function has try/except JSONDecodeError|ValueError
    calls: list[str] = None  # func_ids this function calls (within-codebase)
    is_public: bool = False
    line: int = 0

    def __post_init__(self):
        if self.calls is None:
            self.calls = []


def extract_facts(file_path: Path) -> list[FuncFacts]:
    """Extract per-function facts from a Python source file."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    parents = _parent_map(tree)
    facts: dict[str, FuncFacts] = {}

    def _get_or_create(name: str, lineno: int = 0) -> FuncFacts:
        fid = _func_id(file_path, name)
        if fid not in facts:
            is_pub = (
                not name.startswith("_")
                or name.startswith("__")
                and name.endswith("__")
            )
            facts[fid] = FuncFacts(
                func_id=fid,
                file=str(file_path),
                func_name=name,
                is_public=is_pub,
                line=lineno,
            )
        return facts[fid]

    # Create a fact entry for each top-level function
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(parents.get(node), ast.Module):
                _get_or_create(node.name, node.lineno)

    # Scan all call sites
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        enc = _enclosing_func(node, parents) or "<module>"
        fact = _get_or_create(enc)

        # json.loads() without guard
        func = node.func
        is_json_loads = (
            isinstance(func, ast.Attribute)
            and func.attr == "loads"
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        )
        if is_json_loads:
            guarded = _in_try_except(
                node, parents, {"JSONDecodeError", "ValueError", "Exception"}
            )
            if not guarded:
                fact.json_unguarded = True

        # subprocess.run() without returncode check
        is_subprocess = (
            isinstance(func, ast.Attribute)
            and func.attr in ("run", "call", "check_output")
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if is_subprocess:
            # check if result is assigned and .returncode or .check() is used
            parent = parents.get(node)
            has_check = False
            if isinstance(parent, ast.Assign):
                for sibling in ast.walk(parents.get(parent, ast.Module())):
                    if isinstance(sibling, ast.Attribute) and sibling.attr in (
                        "returncode",
                        "check",
                    ):
                        has_check = True
                        break
            kw_names = {kw.arg for kw in node.keywords}
            if "check" in kw_names:
                has_check = True
            if not has_check:
                fact.subprocess_unchecked = True

        # anthropic.Anthropic() or _call() without prior key check
        is_anthropic = (
            isinstance(func, ast.Attribute) and func.attr == "Anthropic"
        ) or (isinstance(func, ast.Name) and func.id in ("_call", "_call_with_tools"))
        if is_anthropic:
            # Check if there's a key/api_key guard in this function
            enc_func = _enclosing_func(node, parents)
            if enc_func:
                # Look for `if not key` or `raise RuntimeError` pattern in same function scope
                guarded = False
                for fn_node in ast.walk(tree):
                    if (
                        isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and fn_node.name == enc_func
                    ):
                        for child in ast.walk(fn_node):
                            if isinstance(child, ast.Raise):
                                guarded = True
                            if isinstance(child, ast.If):
                                test_src = (
                                    ast.unparse(child.test)
                                    if hasattr(ast, "unparse")
                                    else ""
                                )
                                if "key" in test_src or "api_key" in test_src:
                                    guarded = True
                if not guarded:
                    fact.api_key_unchecked = True

        # Within-codebase calls: record func name calls
        if isinstance(func, ast.Name) and func.id not in (
            "print",
            "len",
            "str",
            "int",
            "float",
            "list",
            "dict",
            "set",
            "isinstance",
            "hasattr",
            "getattr",
            "setattr",
            "range",
            "enumerate",
            "zip",
            "map",
            "filter",
            "sorted",
            "reversed",
            "open",
            "super",
            "type",
            "repr",
            "hash",
            "id",
            "any",
            "all",
            "min",
            "max",
            "sum",
        ):
            fact.calls.append(func.id)  # resolved to func_id later
        elif isinstance(func, ast.Attribute):
            fact.calls.append(func.attr)

    # Mark functions that guard json exceptions — used as NOT guard in Z3 propagation
    _json_exc = {"JSONDecodeError", "ValueError", "Exception"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            catches_json = handler.type is None or any(
                (isinstance(handler.type, ast.Name) and handler.type.id in _json_exc)
                or (
                    isinstance(handler.type, ast.Attribute)
                    and handler.type.attr in _json_exc
                )
                or (
                    isinstance(handler.type, ast.Tuple)
                    and any(
                        (isinstance(e, ast.Name) and e.id in _json_exc)
                        or (isinstance(e, ast.Attribute) and e.attr in _json_exc)
                        for e in handler.type.elts
                    )
                )
                for _ in [None]
            )
            if catches_json:
                enc = _enclosing_func(node, parents) or "<module>"
                _get_or_create(enc).try_wraps_json = True

    return list(facts.values())


# ---------------------------------------------------------------------------
# Z3 Fixedpoint analysis
# ---------------------------------------------------------------------------


def _interproc_z3(all_facts: list[FuncFacts]) -> dict[str, list[str]]:
    """
    Use Z3 Fixedpoint to derive which public functions are transitively tainted.
    Returns dict: violation_type -> list[func_id]
    """
    # Build name → list[func_id] for within-codebase call resolution.
    # Multiple functions may share the same unqualified name across files.
    # We keep ALL candidates as a conservative fallback so taint is never missed.
    func_ids = [f.func_id for f in all_facts]
    name_to_fids: dict[str, list[str]] = {}
    for f in all_facts:
        name_to_fids.setdefault(f.func_name, []).append(f.func_id)

    # Build per-file import maps when multiple files are present.
    # When a file explicitly imports a name from another codebase file, we can
    # resolve that call to the specific source file rather than all candidates,
    # eliminating false-positive taint edges to unrelated same-named functions.
    codebase_stems = {Path(f.file).stem for f in all_facts}
    file_import_maps: dict[str, dict[str, str]] = {}
    if len({f.file for f in all_facts}) > 1:
        for fp_str in {f.file for f in all_facts}:
            file_import_maps[fp_str] = _file_import_map(Path(fp_str), codebase_stems)

    # Resolve call names to func_ids.
    # Priority:
    #   1. Explicit cross-file import → resolve to that specific file's function
    #   2. Same-file definition (no import) → resolve to caller's own file
    #   3. Neither → conservative fallback to all same-named candidates
    all_fids_set = set(func_ids)
    for f in all_facts:
        resolved = []
        imports = file_import_maps.get(f.file, {})
        caller_stem = Path(f.file).stem
        for callee_name in f.calls:
            # Priority 1: explicit import from another codebase file
            source_stem = imports.get(callee_name)
            if source_stem:
                qualified = f"{source_stem}::{callee_name}"
                if qualified in all_fids_set:
                    resolved.append(qualified)
                    continue
            # Priority 2: callee defined in the same file (no import needed)
            same_file_fid = f"{caller_stem}::{callee_name}"
            if same_file_fid in all_fids_set:
                resolved.append(same_file_fid)
                continue
            # Priority 3: conservative — all candidates (may be external or unknown)
            for fid in name_to_fids.get(callee_name, []):
                resolved.append(fid)
        f.calls = list(set(resolved))  # deduplicate

    # Enumerate all func_ids
    all_ids = list(set(func_ids))
    id_map = {fid: i for i, fid in enumerate(all_ids)}
    N = len(all_ids)

    if N == 0:
        return {}

    _BITS = 16  # supports up to 65536 functions
    _MAX_FUNCS = 1 << _BITS
    if N > _MAX_FUNCS:
        import warnings

        warnings.warn(
            f"pact_interproc: {N} functions exceed _BITS={_BITS} capacity "
            f"({_MAX_FUNCS}); IDs will wrap via modular arithmetic and "
            "taint analysis results may be incorrect — raise _BITS to fix",
            RuntimeWarning,
            stacklevel=2,
        )
    FuncSort = BitVecSort(_BITS)

    def bv(i: int) -> z3.BitVecRef:
        return BitVecVal(i, _BITS)

    fp = Fixedpoint()
    fp.set(engine="datalog")

    # Declare relations
    json_ung = Function("json_unguarded", FuncSort, BoolSort())
    sub_unc = Function("subprocess_unchecked", FuncSort, BoolSort())
    api_key_unc = Function("api_key_unchecked", FuncSort, BoolSort())
    try_wraps_json_rel = Function("try_wraps_json", FuncSort, BoolSort())
    is_pub = Function("is_public", FuncSort, BoolSort())
    calls_rel = Function("calls", FuncSort, FuncSort, BoolSort())
    tainted_json = Function("tainted_json", FuncSort, BoolSort())
    tainted_sub = Function("tainted_sub", FuncSort, BoolSort())
    tainted_api = Function("tainted_api", FuncSort, BoolSort())
    viol_json = Function("violation_json", FuncSort, BoolSort())
    viol_sub = Function("violation_sub", FuncSort, BoolSort())
    viol_api = Function("violation_api_key", FuncSort, BoolSort())

    for rel in [
        json_ung,
        sub_unc,
        api_key_unc,
        try_wraps_json_rel,
        is_pub,
        calls_rel,
        tainted_json,
        tainted_sub,
        tainted_api,
        viol_json,
        viol_sub,
        viol_api,
    ]:
        fp.register_relation(rel)

    # Add EDB facts
    for f in all_facts:
        if f.func_id not in id_map:
            continue
        idx = bv(id_map[f.func_id])
        if f.json_unguarded:
            fp.add_rule(json_ung(idx))
        if f.subprocess_unchecked:
            fp.add_rule(sub_unc(idx))
        if f.api_key_unchecked:
            fp.add_rule(api_key_unc(idx))
        if f.try_wraps_json:
            fp.add_rule(try_wraps_json_rel(idx))
        if f.is_public:
            fp.add_rule(is_pub(idx))
        for callee_fid in f.calls:
            if callee_fid in id_map:
                fp.add_rule(calls_rel(idx, bv(id_map[callee_fid])))

    # Add IDB rules using ForAll/Implies (required by Z3 Datalog engine)
    _F, _G = BitVecs("_F _G", _BITS)

    # tainted_json(F) :- json_unguarded(F)
    fp.add_rule(ForAll([_F], Implies(json_ung(_F), tainted_json(_F))))
    # tainted_json(F) :- calls(F, G), tainted_json(G), not try_wraps_json(F)
    fp.add_rule(
        ForAll(
            [_F, _G],
            Implies(
                And(calls_rel(_F, _G), tainted_json(_G), Not(try_wraps_json_rel(_F))),
                tainted_json(_F),
            ),
        )
    )
    # violation_json(F) :- tainted_json(F), is_public(F)
    fp.add_rule(ForAll([_F], Implies(And(tainted_json(_F), is_pub(_F)), viol_json(_F))))

    # tainted_sub(F) :- subprocess_unchecked(F)
    fp.add_rule(ForAll([_F], Implies(sub_unc(_F), tainted_sub(_F))))
    # tainted_sub(F) :- calls(F, G), tainted_sub(G)
    fp.add_rule(
        ForAll(
            [_F, _G], Implies(And(calls_rel(_F, _G), tainted_sub(_G)), tainted_sub(_F))
        )
    )
    # violation_sub(F) :- tainted_sub(F), is_public(F)
    fp.add_rule(ForAll([_F], Implies(And(tainted_sub(_F), is_pub(_F)), viol_sub(_F))))

    # tainted_api(F) :- api_key_unchecked(F)
    fp.add_rule(ForAll([_F], Implies(api_key_unc(_F), tainted_api(_F))))
    # tainted_api(F) :- calls(F, G), tainted_api(G)
    fp.add_rule(
        ForAll(
            [_F, _G], Implies(And(calls_rel(_F, _G), tainted_api(_G)), tainted_api(_F))
        )
    )
    # violation_api_key(F) :- tainted_api(F), is_public(F)
    fp.add_rule(ForAll([_F], Implies(And(tainted_api(_F), is_pub(_F)), viol_api(_F))))

    # Query and extract results
    _Q = z3.BitVec("_Q", _BITS)
    violations: dict[str, list[str]] = {"json": [], "subprocess": [], "api_key": []}
    rev_map = {v: k for k, v in id_map.items()}

    for viol_rel, key in [
        (viol_json, "json"),
        (viol_sub, "subprocess"),
        (viol_api, "api_key"),
    ]:
        result = fp.query(Exists([_Q], viol_rel(_Q)))
        if result == z3.sat:
            ans = fp.get_answer()

            # ans encodes the satisfying assignments; walk to find bv values
            def _collect_bv(expr: z3.ExprRef) -> list[int]:
                found = []
                if z3.is_bv_value(expr):
                    found.append(expr.as_long())
                for child in expr.children():
                    found.extend(_collect_bv(child))
                return found

            for idx in _collect_bv(ans):
                if idx in rev_map and rev_map[idx] not in violations[key]:
                    violations[key].append(rev_map[idx])

    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class InterProcViolation:
    func_id: str
    file: str
    func_name: str
    line: int
    violation_type: str
    propagation_depth: int  # 0 = direct, 1+ = transitive


def analyze_codebase(root: Path, verbose: bool = False) -> list[InterProcViolation]:
    """Analyze all Python files under root for interprocedural violations."""
    files = sorted(root.rglob("*.py"))
    files = [
        f
        for f in files
        if not any(
            p in f.parts for p in ("__pycache__", ".venv", "node_modules", ".git")
        )
        and not f.name.startswith("test_")
        and not f.name.startswith("conftest")
    ]

    all_facts: list[FuncFacts] = []
    for fpath in files:
        facts = extract_facts(fpath)
        all_facts.extend(facts)
        if verbose and facts:
            direct = [f for f in facts if f.json_unguarded or f.subprocess_unchecked]
            if direct:
                print(
                    f"  {fpath.name}: {len(direct)} direct violations across {len(facts)} funcs"
                )

    if verbose:
        print(f"\n[interproc] {len(files)} files, {len(all_facts)} functions analyzed")
        print("[interproc] running Z3 fixedpoint...")

    # Build a lookup for direct violations (for propagation depth)
    direct_json = {f.func_id for f in all_facts if f.json_unguarded}
    direct_sub = {f.func_id for f in all_facts if f.subprocess_unchecked}
    fact_by_id = {f.func_id: f for f in all_facts}

    z3_viols = _interproc_z3(all_facts)

    results: list[InterProcViolation] = []
    for fid in z3_viols.get("json", []):
        f = fact_by_id.get(fid)
        if f and f.func_name != "<module>":  # skip module-level noise
            depth = 0 if fid in direct_json else 1
            results.append(
                InterProcViolation(
                    func_id=fid,
                    file=f.file,
                    func_name=f.func_name,
                    line=f.line,
                    violation_type="json_unguarded_transitive",
                    propagation_depth=depth,
                )
            )

    for fid in z3_viols.get("subprocess", []):
        f = fact_by_id.get(fid)
        if f and f.func_name != "<module>":
            depth = 0 if fid in direct_sub else 1
            results.append(
                InterProcViolation(
                    func_id=fid,
                    file=f.file,
                    func_name=f.func_name,
                    line=f.line,
                    violation_type="subprocess_unchecked_transitive",
                    propagation_depth=depth,
                )
            )

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="pact interproc",
        description="Interprocedural Z3 analysis — find violations that propagate across call boundaries.",
    )
    p.add_argument("root", type=Path, nargs="?", default=Path("."))
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    root = args.root.resolve()
    results = analyze_codebase(root, verbose=args.verbose)

    if args.json:
        import dataclasses

        print(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
        return

    if not results:
        print("[interproc] no interprocedural violations found")
        return

    transitive = [r for r in results if r.propagation_depth > 0]
    direct = [r for r in results if r.propagation_depth == 0]

    print(
        f"[interproc] {len(results)} violations ({len(direct)} direct, {len(transitive)} transitive through call graph)"
    )
    print()

    by_type: dict[str, list[InterProcViolation]] = {}
    for r in results:
        by_type.setdefault(r.violation_type, []).append(r)

    for vtype, viols in sorted(by_type.items()):
        print(f"  {vtype}: {len(viols)} functions")
        for v in sorted(viols, key=lambda x: (x.propagation_depth, x.file)):
            tag = (
                "direct" if v.propagation_depth == 0 else f"depth-{v.propagation_depth}"
            )
            print(f"    [{tag}] {v.func_id}  (line {v.line})")
        print()


if __name__ == "__main__":
    main()
