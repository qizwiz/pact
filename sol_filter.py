"""
sol_filter — gate a directory of .sol files down to the Halmos-tractable, 0.8.x subset.

Source-agnostic: point it at ANY directory of Solidity (a cloned contest repo, our
examples, whatever). It keeps only contracts our pipeline (mutate + Halmos) can actually
handle — because grounding showed real-contract corpora skew pre-0.8 / huge / import-heavy
and Halmos chokes on big, loopy, dependency-laden code.

Pure-text heuristics (fast, no network, no forge needed):
  - pragma ^0.8.x          (pre-0.8 rejected — different arithmetic + Halmos/our setup is 0.8)
  - size <= MAX_LINES      (symbolic execution doesn't scale on huge contracts)
  - self-contained         (external imports -> needs deps -> won't compile standalone for Halmos)
  - few/bounded loops      (unbounded loops blow up symbolic unrolling)

Output: per file PASS / REJECT(reason) + a summary. The survivors are the real-0.8
substrate to mutate (CTFs) or scan against invariants (0-day lens).

    .venv/bin/python sol_filter.py <dir> [more dirs...]
"""

from __future__ import annotations

import os
import re
import sys

MAX_LINES = 160  # Halmos-tractability heuristic


def classify(path: str, src: str) -> tuple[bool, str]:
    lines = src.splitlines()
    # must define a real contract (not just an interface/library — nothing to mutate)
    if not re.search(r"\bcontract\s+\w+", src):
        return False, "no contract (interface/library only)"
    if not re.search(r"\bfunction\b[^;{]*\{", src):
        return False, "no function with a body (nothing to mutate)"
    # pragma
    m = re.search(r"pragma\s+solidity\s+([^\n;]+)", src)
    if not m:
        return False, "no pragma"
    pragma = m.group(1)
    if not re.search(r"0\.8", pragma):
        return False, f"pre-0.8 pragma ({pragma.strip()})"
    # size
    if len(lines) > MAX_LINES:
        return False, f"too big ({len(lines)} lines > {MAX_LINES})"
    # self-contained: external imports (anything not a same-dir relative file) -> needs deps
    imports = [li for li in lines if re.match(r"\s*import\b", li)]
    ext = [
        li for li in imports if not re.search(r'["\']\./', li)  # not a local ./ import
    ]
    if ext:
        return False, f"external imports ({len(ext)}; needs deps, not standalone)"
    # loops (unbounded symbolic blowup)
    loops = len(re.findall(r"\b(for|while)\s*\(", src))
    if loops > 2:
        return False, f"loop-heavy ({loops} loops; Halmos unrolling risk)"
    # has some state-changing surface worth checking
    if not re.search(r"\bmapping\b|\buint\d*\b", src):
        return False, "no obvious numeric/mapping state to invariant-check"
    return True, f"ok (0.8, {len(lines)} lines, {loops} loops, self-contained)"


def main(dirs: list[str]) -> None:
    files = []
    for d in dirs:
        if os.path.isfile(d) and d.endswith(".sol"):
            files.append(d)
            continue
        for root, _, fs in os.walk(d):
            if any(
                skip in root for skip in ("/lib/", "/out/", "/cache/", "/node_modules/")
            ):
                continue
            for f in fs:
                if f.endswith(".sol") and not f.endswith(".t.sol"):
                    files.append(os.path.join(root, f))
    passed = []
    print(f"scanning {len(files)} .sol file(s)...\n")
    for f in sorted(files):
        try:
            src = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        ok, why = classify(f, src)
        tag = "🟢 PASS  " if ok else "🔴 reject"
        rel = f.replace(os.path.expanduser("~"), "~")
        print(f"  {tag} {rel}  — {why}")
        if ok:
            passed.append(f)
    print(
        f"\n{len(passed)}/{len(files)} are 0.8.x + small + self-contained + Halmos-candidate."
    )
    print(
        "(these are the real substrate: mutate -> CTFs, or scan vs invariants -> 0-day lens)"
    )


if __name__ == "__main__":
    args = sys.argv[1:] or [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples/solidity")
    ]
    main(args)
