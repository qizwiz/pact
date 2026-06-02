"""
mutate — a MATHEMATICAL bug generator: real bugs by construction, no LLM.

The distribution ceiling on synthetic self-play only binds when the GENERATOR is the
model. Define the bug-space formally and it doesn't: take correct code, apply AST
mutation operators (< -> <=, + -> -, n -> n+-1, and -> or, drop a guard, ...). Each
mutant that behaves differently from the original IS a real bug — and the original is
its ground-truth TWIN, for free. The space is combinatorial over the AST, model-
independent, unlimited.

A mutant is kept only if it is LIVE: some input makes it diverge from the original
(differential by execution). Equivalent mutants (no behavioral change) are discarded —
they are not bugs. So every challenge this emits is a real, executable, twinned bug
that no LLM imagined.

Honest scope: these are genuine behavioral divergences, but the mutation distribution
is NOT the real-contest-bug distribution (those are semantic/economic). Good for
measuring/escaping the learning ceiling; transfer to money-bugs is a separate test.

    .venv/bin/python mutate.py   # demo: generate live mutants of a correct module
"""

from __future__ import annotations

import ast
import importlib.util
import random

# --------------------------------------------------------------------------- #
# mutation operators (single-point: each mutant has exactly one mutation)
# --------------------------------------------------------------------------- #
_CMP_SWAP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}
_BIN_SWAP = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.FloorDiv,
    ast.FloorDiv: ast.Div,
}
_BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}


class _MutateOne(ast.NodeTransformer):
    """Apply exactly the k-th eligible mutation in a deterministic walk."""

    def __init__(self, target: int):
        self.target = target
        self.count = -1
        self.applied = None  # (kind, lineno, before, after)

    def _hit(self) -> bool:
        self.count += 1
        return self.count == self.target

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in _CMP_SWAP and self._hit():
            before = type(node.ops[0]).__name__
            node.ops[0] = _CMP_SWAP[type(node.ops[0])]()
            self.applied = ("cmp", node.lineno, before, type(node.ops[0]).__name__)
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if type(node.op) in _BIN_SWAP and self._hit():
            before = type(node.op).__name__
            node.op = _BIN_SWAP[type(node.op)]()
            self.applied = ("binop", node.lineno, before, type(node.op).__name__)
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if type(node.op) in _BOOL_SWAP and self._hit():
            before = type(node.op).__name__
            node.op = _BOOL_SWAP[type(node.op)]()
            self.applied = ("boolop", node.lineno, before, type(node.op).__name__)
        return node

    def visit_Constant(self, node):
        self.generic_visit(node)
        if (
            isinstance(node.value, int)
            and not isinstance(node.value, bool)
            and self._hit()
        ):
            before = node.value
            delta = 1 if (node.value % 2 == 0) else -1  # off-by-one
            new = ast.Constant(value=node.value + delta)
            self.applied = ("const", node.lineno, before, node.value + delta)
            return ast.copy_location(new, node)
        return node


def _count_sites(src: str) -> int:
    m = _MutateOne(target=-2)  # never hits; just counts
    m.visit(ast.parse(src))
    return m.count + 1


def mutate(src: str, index: int):
    """Return (mutant_src, applied) for the index-th mutation site, or (None, None)."""
    tree = ast.parse(src)
    mut = _MutateOne(target=index)
    new_tree = mut.visit(tree)
    if mut.applied is None:
        return None, None
    ast.fix_missing_locations(new_tree)
    try:
        return ast.unparse(new_tree), mut.applied
    except Exception:
        return None, None


# --------------------------------------------------------------------------- #
# liveness: a mutant is a real bug iff some input distinguishes it from original
# --------------------------------------------------------------------------- #
def _load(src: str, name: str):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src, f"<{name}>", "exec"), mod.__dict__)
    return mod


def _call(fn, args):
    try:
        return ("ok", fn(*args))
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__)


def _samples(arity: int, n: int):
    pools = [
        lambda: random.randint(-5, 20),
        lambda: round(random.uniform(-5, 20), 2),
        lambda: [random.randint(-3, 9) for _ in range(random.randint(0, 6))],
        lambda: random.choice([0, 1, -1, 2]),
    ]
    out = [
        tuple(0 for _ in range(arity)),
        tuple(1 for _ in range(arity)),
    ]  # boundary seeds
    for _ in range(n):
        out.append(tuple(random.choice(pools)() for _ in range(arity)))
    return out


def witness(orig_src: str, mut_src: str, func: str, arity: int, tries: int = 200):
    """An input on which mutant diverges from original, or None (equivalent)."""
    o = getattr(_load(orig_src, "orig"), func)
    m = getattr(_load(mut_src, "mut"), func)
    for args in _samples(arity, tries):
        try:
            if _call(o, args) != _call(m, args):
                return args
        except TypeError:
            return None  # arity guess wrong
    return None


def live_mutants(orig_src: str, func: str, arity: int, want: int = 8):
    """Generate up to `want` LIVE (behaviorally-divergent) mutants of orig_src.func."""
    found = []
    n = _count_sites(orig_src)
    for i in range(n):
        mut_src, applied = mutate(orig_src, i)
        if mut_src is None:
            continue
        w = witness(orig_src, mut_src, func, arity)
        if w is not None:
            found.append({"mutant": mut_src, "applied": applied, "witness": w})
            if len(found) >= want:
                break
    return found, n


# --------------------------------------------------------------------------- #
# demo: a correct module → mathematically-generated real bugs, zero LLM
# --------------------------------------------------------------------------- #
_DEMO = """
def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x

def running_total(xs):
    total = 0
    for v in xs:
        total = total + v
    return total

def percentile_index(n, p):
    # index into a sorted list of length n for percentile p (0..100)
    return (n * p) // 100
"""

if __name__ == "__main__":
    targets = [("clamp", 3), ("running_total", 1), ("percentile_index", 2)]
    total_sites = 0
    total_live = 0
    for func, arity in targets:
        live, n = live_mutants(_DEMO, func, arity, want=6)
        total_sites += n
        total_live += len(live)
        print(
            f"\n=== {func}: {n} mutation sites, {len(live)} LIVE bugs generated (zero LLM) ==="
        )
        for b in live[:6]:
            kind, line, before, after = b["applied"]
            print(f"  line {line}: {kind} {before}->{after}  | witness {b['witness']}")
    print(
        f"\nTOTAL: {total_live} real, twinned, executable bugs from {total_sites} sites — no LLM, unbounded supply."
    )
