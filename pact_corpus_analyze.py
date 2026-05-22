"""
pact_corpus_analyze.py — Deep corpus analysis with CFG proofs.

Reads corpus.jsonl and upgrades each violation with:
  1. pact_sheaf Ȟ¹ rank + Z3 certification
  2. pact_cfg_proof Z3 UNSAT certificate (for streaming patterns)
  3. Synthesis readiness flag (can we auto-generate fix + test?)

Output: JSON-L with proof metadata attached to each violation.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import os
import ast as _ast
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request

try:
    from pact_sheaf import h1_rank_for_file  # noqa: F401 check_file unused here

    _HAS_SHEAF = True
except ImportError:
    _HAS_SHEAF = False

try:
    from pact_cfg_proof import prove_loop_guard

    _HAS_CFG = True
except ImportError:
    _HAS_CFG = False


# ---------------------------------------------------------------------------
# Fetch source from GitHub raw URL
# ---------------------------------------------------------------------------


def _fetch_raw(repo: str, file_path: str, token: str) -> Optional[str]:
    """Fetch raw file content from GitHub. Returns None on failure."""
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{file_path}"
    req = Request(url, headers={"Authorization": f"token {token}"})
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Streaming-pattern detection
# ---------------------------------------------------------------------------


def _has_async_for_llm_stream(source: str) -> list[tuple[str, str]]:
    """
    Return list of (func_name, loop_var) for every async-for loop that
    iterates over something that looks like an LLM streaming call.
    """
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return []

    results = []
    _LLM_STREAM_METHODS = {"create", "stream", "complete", "generate"}

    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            func_name = node.name
            for child in _ast.walk(node):
                if isinstance(child, _ast.AsyncFor) and isinstance(
                    child.target, _ast.Name
                ):
                    # Check what we're iterating over
                    iter_src = (
                        _ast.unparse(child.iter) if hasattr(_ast, "unparse") else ""
                    )
                    if any(m in iter_src for m in _LLM_STREAM_METHODS):
                        results.append((func_name, child.target.id))
    return results


# ---------------------------------------------------------------------------
# Single-entry analysis
# ---------------------------------------------------------------------------


@dataclass
class AnalyzedEntry:
    repo: str
    stars: int
    file: str
    line: int
    mode: str
    call: str
    # New fields
    h1_rank: Optional[int] = None
    z3_certified: Optional[bool] = None
    has_streaming_pattern: bool = False
    streaming_funcs: list = None  # list of (func, var)
    cfg_proved: Optional[bool] = None
    synthesis_ready: bool = False
    error: Optional[str] = None


def analyze_entry(entry: dict, token: str) -> AnalyzedEntry:
    result = AnalyzedEntry(
        repo=entry["repo"],
        stars=entry.get("stars", 0),
        file=entry["file"],
        line=entry["line"],
        mode=entry["mode"],
        call=entry.get("call", ""),
        streaming_funcs=[],
    )

    source = _fetch_raw(entry["repo"], entry["file"], token)
    if source is None:
        result.error = "fetch_failed"
        return result

    # Write to temp file for pact tools
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(source)
        tmp = f.name

    try:
        # pact_sheaf analysis
        if _HAS_SHEAF:
            try:
                result.h1_rank = h1_rank_for_file(tmp)
                result.z3_certified = result.h1_rank == 0
            except Exception as e:
                result.error = f"sheaf: {e}"

        # Detect streaming patterns
        streaming = _has_async_for_llm_stream(source)
        if streaming:
            result.has_streaming_pattern = True
            result.streaming_funcs = streaming

            # Try CFG proof for each streaming function
            if _HAS_CFG:
                for func_name, loop_var in streaming:
                    try:
                        proof = prove_loop_guard(tmp, func_name, loop_var)
                        if not proof.proved:
                            result.cfg_proved = False
                            result.synthesis_ready = True
                            break
                        else:
                            if result.cfg_proved is None:
                                result.cfg_proved = True
                    except Exception as e:
                        import warnings

                        warnings.warn(
                            f"pact_corpus_analyze: cfg proof failed: {e}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                        result.error = f"cfg: {e}"

        # Synthesis readiness: unguarded sheaf violation + has streaming → synthesizable
        if result.h1_rank and result.h1_rank > 0 and result.has_streaming_pattern:
            result.synthesis_ready = True

    finally:
        os.unlink(tmp)

    return result


# ---------------------------------------------------------------------------
# Corpus-level analysis
# ---------------------------------------------------------------------------


def analyze_corpus(
    corpus_path: str,
    token: str,
    modes: set = None,
    max_repos: int = 50,
    min_stars: int = 1000,
    out_path: str = None,
) -> list[AnalyzedEntry]:
    """
    Analyze corpus entries. Filters by mode and stars, dedups by (repo, file).
    """
    if modes is None:
        modes = {"llm_response_unguarded", "optional_dereference", "missing_await"}

    # Load corpus, filter, dedup
    seen = set()
    candidates = []
    with open(corpus_path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("mode") not in modes:
                continue
            if d.get("stars", 0) < min_stars:
                continue
            key = (d["repo"], d["file"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(d)
    # Sort by stars desc, take top max_repos unique repos
    repo_seen = set()
    filtered = []
    for c in sorted(candidates, key=lambda x: x.get("stars", 0), reverse=True):
        if len(repo_seen) >= max_repos:
            break
        repo_seen.add(c["repo"])
        filtered.append(c)

    print(f"Analyzing {len(filtered)} entries across {len(repo_seen)} repos...")

    results = []
    for i, entry in enumerate(filtered):
        print(f"  [{i+1}/{len(filtered)}] {entry['repo']} {entry['file'][:50]}")
        result = analyze_entry(entry, token)
        results.append(result)
        if out_path:
            with open(out_path, "a") as f:
                f.write(json.dumps(asdict(result)) + "\n")

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_synthesis_report(results: list[AnalyzedEntry]):
    ready = [r for r in results if r.synthesis_ready]
    unguarded = [r for r in results if r.h1_rank and r.h1_rank > 0]
    streaming = [r for r in results if r.has_streaming_pattern]
    cfg_violations = [r for r in results if r.cfg_proved is False]

    print("\n=== CORPUS ANALYSIS REPORT ===")
    print(f"Total analyzed: {len(results)}")
    print(f"Sheaf violations (Ȟ¹ > 0): {len(unguarded)}")
    print(f"Streaming patterns found: {len(streaming)}")
    print(f"CFG proof violations: {len(cfg_violations)}")
    print(f"Synthesis-ready (fix + test + proof): {len(ready)}")

    if cfg_violations:
        print("\nTop CFG-provable streaming violations:")
        for r in sorted(cfg_violations, key=lambda x: x.stars, reverse=True)[:10]:
            funcs = ", ".join(f"{f}({v})" for f, v in (r.streaming_funcs or [])[:2])
            print(f"  {r.repo} ({r.stars}★) — {r.file}:{r.line} — {funcs}")

    if unguarded:
        print("\nTop sheaf violations:")
        for r in sorted(unguarded, key=lambda x: x.stars, reverse=True)[:10]:
            print(f"  {r.repo} ({r.stars}★) — {r.file}:{r.line} — Ȟ¹={r.h1_rank}")


if __name__ == "__main__":
    token = subprocess.check_output(["gh", "auth", "token"]).decode().strip()
    corpus = str(Path.home() / "src/pact/corpus.jsonl")
    results = analyze_corpus(
        corpus,
        token,
        modes={"llm_response_unguarded", "optional_dereference"},
        max_repos=30,
        min_stars=2000,
        out_path="/tmp/pact_corpus_analysis.jsonl",
    )
    print_synthesis_report(results)
