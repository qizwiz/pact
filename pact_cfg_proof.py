"""
pact_cfg_proof.py — AST → CFG → Z3 safety proof for guard properties.

Given a Python function containing an `async for VAR in ITER:` loop, this
module:
  1. Extracts the loop body from the AST
  2. Builds a basic-block CFG (handling if/continue/break/return)
  3. Encodes reachability and path constraints as Z3 Boolean variables
  4. Proves: for every execution path that reaches a VAR.attr access site,
     the path must have passed through a `if VAR is None: continue` guard

This is the rigorous version of what pact_sheaf approximates with sequential
order. The proof is derived entirely from the AST — no hand-written model.
"""

from __future__ import annotations

import ast as _ast
from dataclasses import dataclass, field
from pathlib import Path

try:
    import z3

    _HAS_Z3 = True
except ImportError:
    _HAS_Z3 = False


# ---------------------------------------------------------------------------
# CFG data structures
# ---------------------------------------------------------------------------


@dataclass
class BasicBlock:
    id: int
    stmts: list[_ast.stmt] = field(default_factory=list)
    # Each successor is (target_block_id, condition_z3_expr_or_None)
    successors: list[tuple[int, object]] = field(default_factory=list)

    def __repr__(self):
        return f"BB{self.id}(stmts={len(self.stmts)}, succs={[s for s,_ in self.successors]})"


@dataclass
class CFG:
    blocks: dict[int, BasicBlock] = field(default_factory=dict)
    entry: int = 0
    exit: int = -1

    def new_block(self) -> BasicBlock:
        bid = len(self.blocks)
        bb = BasicBlock(id=bid)
        self.blocks[bid] = bb
        return bb


# ---------------------------------------------------------------------------
# CFG builder from async-for loop body
# ---------------------------------------------------------------------------


def _build_loop_body_cfg(stmts: list[_ast.stmt], var: str, solver_ctx) -> CFG:
    """
    Build a CFG for a loop body where `var` is the loop iteration variable.

    Handles:
    - Sequential statements → linear edges
    - `if VAR is None: continue` → branch + continue edge
    - `if COND: continue` → branch with opaque condition
    - `if COND: return/raise` → branch with opaque condition, dead end
    - Simple `continue` → jump to exit (loop repeat)
    - Simple `return`/`raise` → jump to exit
    """
    cfg = CFG()
    exit_bb = cfg.new_block()  # BB0 = exit / loop-back
    cfg.exit = exit_bb.id

    entry_bb = cfg.new_block()  # BB1 = entry into loop body
    cfg.entry = entry_bb.id

    _fill_block(cfg, entry_bb, stmts, exit_bb.id, var)
    return cfg


def _is_none_check(test: _ast.expr, var: str) -> bool:
    """True iff test is `VAR is None` or `VAR is not None`."""
    if isinstance(test, _ast.Compare):
        if (
            isinstance(test.left, _ast.Name)
            and test.left.id == var
            and len(test.ops) == 1
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], _ast.Constant)
            and test.comparators[0].value is None
        ):
            return isinstance(test.ops[0], (_ast.Is, _ast.IsNot))
    return False


def _is_none_check_positive(test: _ast.expr, var: str) -> bool:
    """True iff test is `VAR is None` (positive form)."""
    if isinstance(test, _ast.Compare):
        return (
            isinstance(test.left, _ast.Name)
            and test.left.id == var
            and len(test.ops) == 1
            and isinstance(test.ops[0], _ast.Is)
            and isinstance(test.comparators[0], _ast.Constant)
            and test.comparators[0].value is None
        )
    return False


def _body_is_just_continue(body: list[_ast.stmt]) -> bool:
    return len(body) == 1 and isinstance(body[0], _ast.Continue)


def _body_exits(body: list[_ast.stmt]) -> bool:
    """True if body unconditionally exits (continue/return/raise)."""
    if not body:
        return False
    last = body[-1]
    return isinstance(last, (_ast.Continue, _ast.Return, _ast.Raise))


def _fill_block(
    cfg: CFG,
    current: BasicBlock,
    stmts: list[_ast.stmt],
    exit_id: int,
    var: str,
) -> int:
    """
    Recursively fill `current` block with `stmts`.
    Returns the id of the block that control falls through to after stmts.
    A return of exit_id means the block already jumps to exit.
    """
    for i, stmt in enumerate(stmts):
        if isinstance(stmt, _ast.Continue):
            current.successors.append((exit_id, None))
            return exit_id

        if isinstance(stmt, (_ast.Return, _ast.Raise)):
            current.successors.append((exit_id, None))
            return exit_id

        if isinstance(stmt, _ast.If):
            test = stmt.test

            if _is_none_check(test, var) and _body_exits(stmt.body):
                # `if VAR is None: continue/return/raise`
                # True branch → exits (guard), False branch → falls through
                none_true = _is_none_check_positive(test, var)
                guard_bb = cfg.new_block()  # true branch (exits)
                fall_bb = cfg.new_block()  # false branch (continues)

                current.successors.append((guard_bb.id, ("none_check", var, none_true)))
                current.successors.append(
                    (fall_bb.id, ("none_check_neg", var, none_true))
                )

                # True branch: fill guard body, then exits
                _fill_block(cfg, guard_bb, stmt.body, exit_id, var)
                if not guard_bb.successors:
                    guard_bb.successors.append((exit_id, None))

                # False branch: continue filling from here
                current = fall_bb
                remaining = stmts[i + 1 :]
                return _fill_block(cfg, fall_bb, remaining, exit_id, var)

            else:
                # Generic if — model as opaque branch
                true_bb = cfg.new_block()
                false_bb = cfg.new_block()
                merge_bb = cfg.new_block()

                current.successors.append((true_bb.id, ("opaque_true", i)))
                current.successors.append((false_bb.id, ("opaque_false", i)))

                true_end = _fill_block(cfg, true_bb, stmt.body, exit_id, var)
                orelse = stmt.orelse or []
                false_end = _fill_block(cfg, false_bb, orelse, exit_id, var)

                if true_end != exit_id and not true_bb.successors:
                    true_bb.successors.append((merge_bb.id, None))
                elif true_end != exit_id:
                    cfg.blocks[true_end].successors.append((merge_bb.id, None))

                if false_end != exit_id and not false_bb.successors:
                    false_bb.successors.append((merge_bb.id, None))
                elif false_end != exit_id:
                    cfg.blocks[false_end].successors.append((merge_bb.id, None))

                current = merge_bb
                remaining = stmts[i + 1 :]
                return _fill_block(cfg, merge_bb, remaining, exit_id, var)
        else:
            current.stmts.append(stmt)

    # Fall off the end → exit
    if not current.successors:
        current.successors.append((exit_id, None))
    return exit_id


# ---------------------------------------------------------------------------
# Access site detection
# ---------------------------------------------------------------------------


def _find_var_attr_accesses(stmts: list[_ast.stmt], var: str) -> list[tuple[int, str]]:
    """Return (lineno, attr) for all VAR.attr accesses in stmts."""
    accesses = []
    for stmt in stmts:
        for node in _ast.walk(stmt):
            if (
                isinstance(node, _ast.Attribute)
                and isinstance(node.value, _ast.Name)
                and node.value.id == var
                and isinstance(node.ctx, _ast.Load)
            ):
                accesses.append((node.lineno, node.attr))
    return accesses


# ---------------------------------------------------------------------------
# Z3 reachability encoding
# ---------------------------------------------------------------------------


def _encode_cfg_z3(cfg: CFG, var: str, chunk_is_none_sym: object) -> tuple:
    """
    Encode the CFG as exact Z3 reachability constraints.

    reach[b] ⟺ ∨{ reach[p] ∧ edge_cond(p→b)  for each predecessor p }

    This is bidirectional — not just implication — so Z3 cannot freely set
    reach[b]=True; it must be justified by a concrete reachable predecessor.

    Returns (reach_dict, constraints_list).
    """
    reach = {bid: z3.Bool(f"reach_{bid}") for bid in cfg.blocks}

    # Build predecessor map
    preds: dict[int, list[tuple[int, object]]] = {bid: [] for bid in cfg.blocks}
    for bid, bb in cfg.blocks.items():
        for succ_id, cond in bb.successors:
            if succ_id in preds:
                preds[succ_id].append((bid, cond))

    constraints = []

    for bid in cfg.blocks:
        if bid == cfg.entry:
            constraints.append(reach[bid])
            continue

        pred_list = preds[bid]
        if not pred_list:
            constraints.append(z3.Not(reach[bid]))
            continue

        # Each predecessor contributes: reach[pred] ∧ edge_condition
        pred_terms = []
        for pred_id, cond in pred_list:
            if cond is None:
                pred_terms.append(reach[pred_id])
            elif isinstance(cond, tuple) and cond[0] == "none_check":
                # True branch of `if VAR is None:` — reached when chunk IS None
                _, _, positive = cond
                if positive:
                    pred_terms.append(z3.And(reach[pred_id], chunk_is_none_sym))
                else:
                    pred_terms.append(z3.And(reach[pred_id], z3.Not(chunk_is_none_sym)))
            elif isinstance(cond, tuple) and cond[0] == "none_check_neg":
                # False branch of `if VAR is None:` — reached when chunk is NOT None
                _, _, positive = cond
                if positive:
                    pred_terms.append(z3.And(reach[pred_id], z3.Not(chunk_is_none_sym)))
                else:
                    pred_terms.append(z3.And(reach[pred_id], chunk_is_none_sym))
            elif isinstance(cond, tuple) and cond[0] in ("opaque_true", "opaque_false"):
                # Opaque branch — conservatively allow either direction
                pred_terms.append(reach[pred_id])

        constraints.append(reach[bid] == z3.Or(pred_terms))

    return reach, constraints


# ---------------------------------------------------------------------------
# Main proof function
# ---------------------------------------------------------------------------


@dataclass
class ProofResult:
    proved: bool
    access_sites: list[tuple[int, str]]  # (lineno, attr) that were checked
    unguarded: list[tuple[int, str]]  # access sites where proof FAILED
    certificate: str  # human-readable explanation

    def __repr__(self):
        status = "PROVED SAFE" if self.proved else "UNSAFE"
        return f"ProofResult({status}, sites={self.access_sites}, unguarded={self.unguarded})"


def prove_loop_guard(
    path: str,
    func_name: str,
    loop_var: str,
) -> ProofResult:
    """
    Prove that all `loop_var.attr` accesses in the `async for loop_var in ...`
    loop body inside `func_name` are guarded by `if loop_var is None: continue`.

    Derives Z3 constraints automatically from the AST — no hand-written model.
    """
    if not _HAS_Z3:
        raise RuntimeError("z3 not installed")

    src = Path(path).read_text(encoding="utf-8", errors="replace")
    tree = _ast.parse(src, filename=path)

    # Find the target function
    func_node = None
    for node in _ast.walk(tree):
        if (
            isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            func_node = node
            break

    if func_node is None:
        raise ValueError(f"Function {func_name!r} not found in {path}")

    # Find the async for loop with the target variable
    loop_node = None
    for node in _ast.walk(func_node):
        if (
            isinstance(node, _ast.AsyncFor)
            and isinstance(node.target, _ast.Name)
            and node.target.id == loop_var
        ):
            loop_node = node
            break

    if loop_node is None:
        raise ValueError(f"No `async for {loop_var}` loop found in {func_name}")

    stmts = loop_node.body

    # Build CFG
    cfg = _build_loop_body_cfg(stmts, loop_var, None)

    # Find all VAR.attr access sites, mapped to which block they live in
    # We need block-level access detection
    access_sites: list[tuple[int, str]] = []
    block_accesses: dict[int, list[tuple[int, str]]] = {}
    for bid, bb in cfg.blocks.items():
        accs = _find_var_attr_accesses(bb.stmts, loop_var)
        if accs:
            block_accesses[bid] = accs
            access_sites.extend(accs)

    if not access_sites:
        return ProofResult(
            proved=True,
            access_sites=[],
            unguarded=[],
            certificate=f"No {loop_var}.attr accesses found — trivially safe.",
        )

    # Z3 proof
    chunk_is_none = z3.Bool("chunk_is_none")
    reach, constraints = _encode_cfg_z3(cfg, loop_var, chunk_is_none)

    unguarded = []
    for bid, accs in block_accesses.items():
        # Prove: reach[bid] ∧ chunk_is_none is UNSAT
        # i.e., no execution reaches this block with chunk=None
        solver = z3.Solver()
        solver.add(constraints)
        solver.add(reach[bid])  # block is reachable
        solver.add(chunk_is_none)  # chunk IS None at loop entry
        result = solver.check()

        if result == z3.unsat:
            pass  # proved safe
        else:
            unguarded.extend(accs)

    proved = len(unguarded) == 0
    if proved:
        cert = (
            f"Z3 UNSAT certificate: for all {loop_var}.attr access sites, "
            f"reach(block) ∧ {loop_var}_is_none is UNSAT. "
            f"No execution path reaches {loop_var}.attr when {loop_var} is None."
        )
    else:
        cert = (
            f"Z3 SAT witness found: {unguarded} are reachable with {loop_var}=None. "
            f"Fix: add `if {loop_var} is None: continue` before these sites."
        )

    return ProofResult(
        proved=proved,
        access_sites=access_sites,
        unguarded=unguarded,
        certificate=cert,
    )
