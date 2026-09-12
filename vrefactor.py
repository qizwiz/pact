"""
pact rewrite — VERIFIED behavior-preserving refactoring: STRUCTURE proposes, an ORACLE disposes.

The dual of `pact heal`. Where `heal` *changes* behavior to fix a bug (LLM proposes, Z3 decides),
`rewrite` *preserves* behavior while shrinking code: tree-sitter finds structural rewrite sites (the
correct-by-construction generator), each candidate is APPLIED and then GRADED by a SOUND ORACLE — the
oracle must still PASS *and* a cost metric (byte length) must DROP — else the rewrite is REVERTED.
So `pact rewrite` can only ever emit oracle-passing, smaller code.

Behavioural equivalence of arbitrary code is UNDECIDABLE, so the rules are not "proven equivalences";
they are structurally-motivated rewrites, and the ORACLE is the un-fakeable equivalence check. The
oracle is the achievement; the rules are the generator.

What makes this distinct from every other refactor tool: the oracle is *pluggable*, and the strongest
oracle is a proof-assistant kernel. Point `--oracle` at a test suite, a compiler, or `lake env lean`,
and the same machine refactors Python, Go — or a Lean proof library, where "still elaborates against
the identical statement" is behaviour-preservation certified by the kernel.

Usage
-----
    pact rewrite FILE --oracle 'CMD' [--lang python] [--max N] [--dry] [--timeout S]
    pact rewrite FILE                # auto-detect the oracle (repo test runner, or a Lean project)
    pact rewrite --selftest          # proves it APPLIES a valid rewrite AND REJECTS a behaviour-breaker

The oracle command may contain the token ``{file}`` — it is replaced with the path of the file being
refactored. This is what makes a per-file oracle (``lake env lean {file}``) possible; without it the
oracle is run verbatim (a whole-project test suite that imports the file in place).

tree-sitter grammars are an optional extra: ``pip install pact-tool[refactor]``. Without them this
command degrades gracefully with an install hint (exactly like the TS/JS checker).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

try:
    import tree_sitter as _ts
    from tree_sitter_language_pack import get_language, get_parser

    _HAS_TS = True
    _TS_ERR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover - exercised only when the extra is absent
    _HAS_TS = False
    _TS_ERR = _exc

_INSTALL_HINT = (
    "pact rewrite needs tree-sitter grammars — install the extra:\n"
    "    pip install 'pact-tool[refactor]'\n"
    "(this pulls tree-sitter + tree-sitter-language-pack, ~300 grammars)"
)

NEG_CMP = {
    "==": "!=",
    "!=": "==",
    "is": "is not",
    "is not": "is",
    "in": "not in",
    "not in": "in",
}


def _t(b: bytes, n) -> str:
    return b[n.start_byte : n.end_byte].decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# rule matching — legacy s-expr pattern path + native tree-sitter query path
# ---------------------------------------------------------------------------


def _match_rule(pat, node, b, bnd) -> bool:
    """Structurally match a mined rule PATTERN (sexpr) against a live NODE; bind holes consistently."""
    if pat[0] == "H":
        src = b[node.start_byte : node.end_byte].decode("utf-8", "replace")
        if pat[1] in bnd:
            return bnd[pat[1]] == src
        bnd[pat[1]] = src
        return True
    if pat[0] == "L":
        return (
            node.child_count == 0
            and node.type == pat[1]
            and b[node.start_byte : node.end_byte].decode("utf-8", "replace") == pat[2]
        )
    if node.type != pat[1] or len(node.children) != len(pat[2]):
        return False
    return all(_match_rule(p, k, b, bnd) for p, k in zip(pat[2], node.children))


_QCACHE: dict = {}


def _compiled_query(lang: str, qstr: str):
    key = (lang, qstr)
    if key not in _QCACHE:
        _QCACHE[key] = _ts.Query(get_language(lang), qstr)
    return _QCACHE[key]


def _query_candidates(tree, b, rule, lang):
    """Match a mined rule via its NATIVE tree-sitter query: a repeated @hN capture is an equality
    constraint, @_litN captures are text-pinned, @_whole is the rewrite span. Structure proposes;
    the oracle still disposes."""
    out = []
    cur = _ts.QueryCursor(_compiled_query(lang, rule["query"]))
    for _pat, caps in cur.matches(tree.root_node):
        if "_whole" not in caps:
            continue
        ok = True
        hole_txt = {}
        for name, nodes in caps.items():
            texts = [_t(b, nd) for nd in nodes]
            if name[:1] == "h" and name[1:].isdigit():
                if len(set(texts)) != 1:  # a repeated hole must bind equal source
                    ok = False
                    break
                hole_txt[int(name[1:])] = texts[0]
            elif name.startswith("_lit"):
                if texts[0] != rule.get("lits", {}).get(name):
                    ok = False
                    break
        if not ok:
            continue
        rep = rule["template"]
        for hid, txt in hole_txt.items():
            rep = rep.replace("\x00%d\x00" % hid, txt)
        w = caps["_whole"][0]
        out.append(
            (
                w.start_byte,
                w.end_byte,
                rep,
                "mined-q:%s L%d" % (rule["root"], w.start_point[0] + 1),
            )
        )
    return out


def find_candidates(tree, b, rules=(), lang="python"):
    """Return [(start_byte, end_byte, replacement_str, label)] — structural + mined-rule rewrites.

    The hard-coded rules below are Python-specific (they gate on Python node kinds), so they are inert
    on other languages — the language-agnostic power comes from the mined `query` / `pattern` rules.
    """
    out = []
    qrules = [r for r in rules if r.get("query")]
    prules = [r for r in rules if not r.get("query")]
    for r in qrules:
        out.extend(_query_candidates(tree, b, r, lang))

    def walk(n):
        if n.type == "assignment":
            lhs = n.child_by_field_name("left")
            rhs = n.child_by_field_name("right")
            if (
                lhs
                and rhs
                and lhs.type == "identifier"
                and rhs.type == "binary_operator"
            ):
                rl = rhs.child_by_field_name("left")
                rr = rhs.child_by_field_name("right")
                ops = [c for c in rhs.children if not c.is_named]
                if (
                    rl
                    and rl.type == "identifier"
                    and _t(b, rl) == _t(b, lhs)
                    and rr
                    and len(ops) == 1
                ):
                    rep = "%s %s= %s" % (_t(b, lhs), _t(b, ops[0]), _t(b, rr))
                    out.append(
                        (
                            n.start_byte,
                            n.end_byte,
                            rep,
                            "aug-assign L%d" % (n.start_point[0] + 1),
                        )
                    )
        if n.type == "not_operator":
            arg = n.child_by_field_name("argument")
            inner = arg
            if inner and inner.type == "parenthesized_expression":
                named = [c for c in inner.children if c.is_named]
                inner = named[0] if named else None
            if inner and inner.type == "comparison_operator":
                named = [c for c in inner.children if c.is_named]
                ops = [c for c in inner.children if not c.is_named]
                if len(named) == 2 and len(ops) == 1:
                    opt = _t(b, ops[0])
                    if opt in NEG_CMP:
                        rep = "%s %s %s" % (
                            _t(b, named[0]),
                            NEG_CMP[opt],
                            _t(b, named[1]),
                        )
                        out.append(
                            (
                                n.start_byte,
                                n.end_byte,
                                rep,
                                "neg-cmp(%s) L%d" % (opt, n.start_point[0] + 1),
                            )
                        )
        if n.type == "not_operator":
            inner2 = n.child_by_field_name("argument")
            if inner2 and inner2.type == "not_operator":
                x = inner2.child_by_field_name("argument")
                if x:
                    out.append(
                        (
                            n.start_byte,
                            n.end_byte,
                            "bool(%s)" % _t(b, x),
                            "double-not L%d" % (n.start_point[0] + 1),
                        )
                    )
        if n.type == "comparison_operator":
            named = [c for c in n.children if c.is_named]
            ops = [c for c in n.children if not c.is_named]
            if len(named) == 2 and len(ops) == 1 and _t(b, ops[0]) in ("==", "is"):
                for i, other in ((0, 1), (1, 0)):
                    if named[i].type == "true":
                        out.append(
                            (
                                n.start_byte,
                                n.end_byte,
                                _t(b, named[other]),
                                "redundant-true L%d" % (n.start_point[0] + 1),
                            )
                        )
                        break
        for rule in prules:
            if n.type == rule["root"]:
                bnd: dict = {}
                if _match_rule(rule["pattern"], n, b, bnd):
                    rep = rule["template"]
                    for i, v in bnd.items():
                        rep = rep.replace("\x00%d\x00" % i, v)
                    out.append(
                        (
                            n.start_byte,
                            n.end_byte,
                            rep,
                            "mined:%s L%d" % (rule["root"], n.start_point[0] + 1),
                        )
                    )
        for c in n.children:
            walk(c)

    walk(tree.root_node)
    return out


# ---------------------------------------------------------------------------
# the oracle — pluggable, per-file via {file}
# ---------------------------------------------------------------------------

_LEAN_MARKERS = ("lakefile.lean", "lakefile.toml", "lean-toolchain")


def autodetect_oracle(root: Path, file_path: Path) -> Optional[str]:
    """Zero-config oracle. Reuses heal's test-runner detection for imperative projects; adds a Lean
    rung (a Lean project → per-file elaboration, the fast sound check). Returns a command or None.

    The Lean command uses {file} — a per-file elaboration (~seconds) rather than a whole-project
    `lake build` (thousands of jobs) — so the rewrite loop stays fast on a proof library.
    """
    root = root.resolve()
    for anc in (root, *root.parents):
        if any((anc / m).exists() for m in _LEAN_MARKERS):
            return "cd %s && lake env lean {file}" % anc
        if anc == file_path.anchor:  # do not walk above the filesystem root
            break
    try:
        from .heal import _autodetect_test_cmd  # reuse — no edit to tested code
    except Exception:
        return None
    return _autodetect_test_cmd(root)


def _oracle_ok(
    cmd: str, file_path: Path, cwd: Optional[Path], timeout: int, verbose: bool = False
) -> bool:
    """Run the oracle. ``{file}`` is substituted with the file path (per-file oracles). The oracle's
    exit code is the verdict: 0 = pass. A tighter, timeout-controlled sibling of heal._run_oracle
    (we need a per-attempt timeout, which _run_oracle hard-codes)."""
    run = cmd.replace("{file}", str(file_path)) if "{file}" in cmd else cmd
    if verbose:
        print("    oracle: %r" % run, file=sys.stderr)
    try:
        r = subprocess.run(
            run,
            shell=True,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the verified-refactor loop
# ---------------------------------------------------------------------------


def rewrite(
    path,
    oracle_cmd,
    lang="python",
    timeout=120,
    dry=False,
    max_apply=100,
    rules=(),
    cwd=None,
    verbose=False,
):
    """Apply behaviour-preserving rewrites to ``path``, keeping only those the oracle certifies AND
    that shrink the file. Emits one JSON event per accept/reject and a final ``done`` event. Returns
    0 on success, 2 on a harness error (oracle fails on the pristine file)."""
    if not _HAS_TS:
        print(_INSTALL_HINT, file=sys.stderr)
        return 2
    parser = get_parser(lang)
    p = Path(path)
    cwd = Path(cwd) if cwd else p.resolve().parent
    orig = p.read_bytes()
    if not dry and not _oracle_ok(oracle_cmd, p, cwd, timeout, verbose):
        print(
            json.dumps(
                {
                    "event": "HARNESS-ERROR",
                    "control": "pristine",
                    "note": "the oracle FAILS on the unmodified file; fix the oracle before refactoring",
                }
            )
        )
        return 2
    cur = orig
    applied, rejected = [], []
    try:
        for _ in range(max_apply + 5):
            cands = find_candidates(parser.parse(cur), cur, rules, lang)
            cands = [
                c
                for c in cands
                if len(cur[: c[0]] + c[2].encode() + cur[c[1] :]) < len(cur)
            ]
            if dry:
                for _sb, _eb, _rep, label in sorted(cands):
                    print(json.dumps({"event": "candidate", "rule": label}))
                break
            progressed = False
            for sb, eb, rep, label in sorted(cands):
                newb = cur[:sb] + rep.encode() + cur[eb:]
                p.write_bytes(newb)
                if _oracle_ok(oracle_cmd, p, cwd, timeout, verbose):
                    cur = newb
                    applied.append(label)
                    progressed = True
                    print(
                        json.dumps(
                            {"event": "accept", "rule": label, "bytes": len(cur)}
                        )
                    )
                    break
                else:
                    rejected.append(label)
                    print(
                        json.dumps(
                            {
                                "event": "reject",
                                "rule": label,
                                "reason": "oracle rejected the rewrite",
                            }
                        )
                    )
            if not progressed or len(applied) >= max_apply:
                break
    finally:
        p.write_bytes(orig if dry else cur)
    print(
        json.dumps(
            {
                "event": "done",
                "applied": len(applied),
                "rejected": len(rejected),
                "bytes_before": len(orig),
                "bytes_after": len(cur),
                "saved": len(orig) - len(cur),
                "rules": applied[:30],
            }
        )
    )
    return 0


# ---------------------------------------------------------------------------
# selftest — proves the gate discriminates in BOTH directions
# ---------------------------------------------------------------------------


def selftest() -> int:
    if not _HAS_TS:
        print("SKIP: tree-sitter grammars not installed (%s)" % _TS_ERR)
        print(_INSTALL_HINT)
        return 0  # not a failure — the extra is genuinely optional
    ok = True
    with tempfile.TemporaryDirectory() as _td:
        d = Path(_td)

        # CASE-A: a valid aug-assign IS applied and the oracle still passes.
        src = d / "a.py"
        src.write_text("def f(x):\n    x = x + 1\n    return x\n")
        (d / "test_a.py").write_text("import a\ndef test(): assert a.f(1) == 2\n")
        cmd = "cd %s && %s -c 'import test_a; test_a.test()'" % (d, sys.executable)
        rewrite(str(src), cmd, "python", 60, False, 100, cwd=d)
        caseA = "x += 1" in src.read_text() and "x = x + 1" not in src.read_text()
        print(
            "  %s CASE-A aug-assign applied + oracle passes"
            % ("ok " if caseA else "FAIL")
        )
        ok = ok and caseA

        # CASE-B (sound-gate proof): a neg-cmp rewrite that BREAKS behaviour is REJECTED.
        src2 = d / "b.py"
        src2.write_text(
            "class W:\n"
            "    def __eq__(self, o): return True\n"
            "    def __ne__(self, o): return True\n"  # inconsistent on purpose
            "def g():\n"
            "    return not (W() == W())\n"
        )
        (d / "test_b.py").write_text("import b\ndef test(): assert b.g() is False\n")
        cmd2 = "cd %s && %s -c 'import test_b; test_b.test()'" % (d, sys.executable)
        rewrite(str(src2), cmd2, "python", 60, False, 100, cwd=d)
        caseB = (
            "not (W() == W())" in src2.read_text()
            and "W() != W()" not in src2.read_text()
        )
        print(
            "  %s CASE-B behaviour-breaking rewrite REJECTED by the oracle"
            % ("ok " if caseB else "FAIL")
        )
        ok = ok and caseB

        # CASE-C ({file} per-file oracle proof): the oracle is a per-file command using {file}.
        # A fake "compiler" accepts iff the file does NOT contain the forbidden token — proving the
        # {file} substitution reaches a per-file oracle (the mechanism the Lean oracle rides on).
        src3 = d / "c.py"
        src3.write_text("def h(x):\n    x = x + 1\n    return x\n")
        (d / "test_c.py").write_text("import c\ndef test(): assert c.h(1) == 2\n")
        # oracle: python must import-run the test AND {file} must still parse (py_compile)
        cmd3 = (
            "%s -m py_compile {file} && cd %s && %s -c 'import test_c; test_c.test()'"
            % (
                sys.executable,
                d,
                sys.executable,
            )
        )
        rewrite(str(src3), cmd3, "python", 60, False, 100, cwd=d)
        caseC = "x += 1" in src3.read_text()
        print(
            "  %s CASE-C {file} per-file oracle drives the loop"
            % ("ok " if caseC else "FAIL")
        )
        ok = ok and caseC

    print("RESULT: %s -- 3/3 cases" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    import argparse

    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--selftest":
        return selftest()
    ap = argparse.ArgumentParser(
        prog="pact rewrite",
        description="Verified behaviour-preserving refactoring: tree-sitter proposes, an oracle disposes.",
    )
    ap.add_argument(
        "file",
        nargs="?",
        help="File to refactor in place (reverted unless the oracle passes).",
    )
    ap.add_argument(
        "--oracle",
        metavar="CMD",
        default=None,
        help="Oracle command; must exit 0 for a rewrite to be kept. '{file}' is replaced "
        "with the file path (per-file oracles, e.g. 'lake env lean {file}').",
    )
    ap.add_argument(
        "--test", metavar="CMD", default=None, help="Alias for --oracle (a test suite)."
    )
    ap.add_argument(
        "--lang", default="python", help="tree-sitter grammar name (default: python)."
    )
    ap.add_argument(
        "--max", type=int, default=100, help="Max rewrites to apply (default: 100)."
    )
    ap.add_argument(
        "--timeout", type=int, default=120, help="Per-oracle-run timeout in seconds."
    )
    ap.add_argument(
        "--dry", action="store_true", help="List candidate rewrites; change nothing."
    )
    ap.add_argument(
        "--rules",
        metavar="FILE",
        default=None,
        help="JSONL of mined rules (one per line).",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Echo each oracle invocation to stderr.",
    )
    args = ap.parse_args(argv)

    if not _HAS_TS:
        print(_INSTALL_HINT, file=sys.stderr)
        return 2
    if not args.file:
        ap.error("a FILE is required (or --selftest)")
    fpath = Path(args.file)
    if not fpath.is_file():
        print("error: %s is not a file" % fpath, file=sys.stderr)
        return 2

    oracle = args.oracle or args.test
    if not oracle and not args.dry:
        oracle = autodetect_oracle(fpath.resolve().parent, fpath)
        if not oracle:
            print(
                "error: no --oracle given and none auto-detected.\n"
                "  give one, e.g.  --oracle 'python -m pytest -q'  or  --oracle 'lake env lean {file}'",
                file=sys.stderr,
            )
            return 2
        print(json.dumps({"event": "oracle", "autodetected": oracle}))

    rules = []
    if args.rules:
        with open(args.rules) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rules.append(json.loads(line))
    return rewrite(
        str(fpath), oracle, args.lang, args.timeout, args.dry, args.max, rules
    )


if __name__ == "__main__":
    sys.exit(main())
