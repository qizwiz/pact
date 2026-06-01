"""Local instant-apply merge — apply a code edit without an LLM round-trip.

Mirrors the two-phase model used by "fast-apply" tools (agent-booster /
Morph instant-apply): a deterministic *pattern* phase, then a *fuzzy
chunk-similarity* merge phase, each returning a confidence score.

pact's twist on the idea: the merge is **never trusted on confidence alone.**
In the heal loop every merged result is gated through the existing
checker → Z3 → CrossHair verification, so a high-similarity-but-wrong merge is
rejected by a proof, not by a heuristic. Confidence only decides *whether to
attempt* a local apply before falling back to the LLM. This is precisely the
seam an apply tool can't close on its own — "looks like the right chunk" is
similarity, not correctness — and it is exactly what pact already verifies.

The module is pure stdlib (ast, difflib, textwrap): no Node/WASM runtime, no
network, no API key.
"""

from __future__ import annotations

import ast
import difflib
import textwrap
from dataclasses import dataclass
from typing import Optional

# Merge strategies, mirroring agent-booster's vocabulary.
EXACT_REPLACE = "exact_replace"
DEDENT_REPLACE = "dedent_replace"
FUZZY_REPLACE = "fuzzy_replace"
NONE = "none"


@dataclass
class InstantApplyResult:
    """Result of a local merge attempt.

    merged:     patched source, or None when no merge met the confidence bar.
    confidence: 1.0 for an exact hit; the best chunk-similarity ratio for a
                fuzzy hit; the best ratio *seen* (below threshold) on a miss.
    strategy:   which phase produced the result (see constants above).
    """

    merged: Optional[str]
    confidence: float
    strategy: str


def _unescape(s: str) -> str:
    # Match heal.apply_patch's handling of escaped newlines/tabs coming from
    # JSON-encoded LLM responses.
    return s.replace("\\n", "\n").replace("\\t", "\t")


@dataclass
class _Chunk:
    text: str  # verbatim source lines of the chunk (keepends), incl. decorators
    start: int  # 0-based start line index (inclusive)
    end: int  # 0-based end line index (exclusive)
    indent: str  # leading whitespace of the chunk's first line


def _chunks(source: str) -> list[_Chunk]:
    """Split source into logical chunks: every function/class definition.

    Decorators are folded into the chunk so a fuzzy replace doesn't orphan
    them. Nested defs are emitted too — more candidates is strictly better
    for the similarity search, and verification rejects a wrong pick.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines(keepends=True)
    chunks: list[_Chunk] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        start_lineno = node.lineno
        for dec in getattr(node, "decorator_list", []):
            start_lineno = min(start_lineno, dec.lineno)
        s = start_lineno - 1  # 0-based, inclusive
        e = end  # end_lineno is 1-based inclusive → 0-based exclusive == end_lineno
        text = "".join(lines[s:e])
        first = lines[s] if s < len(lines) else ""
        indent = first[: len(first) - len(first.lstrip())]
        chunks.append(_Chunk(text=text, start=s, end=e, indent=indent))
    return chunks


def _reindent(text: str, indent: str) -> str:
    """Dedent `text` to its own baseline, then re-indent every non-blank line
    by `indent`. Keeps blank lines blank (no trailing whitespace)."""
    dedented = textwrap.dedent(text)
    out_lines = []
    for ln in dedented.splitlines(keepends=True):
        if ln.strip() == "":
            out_lines.append(ln)  # leave blank lines blank
        else:
            out_lines.append(indent + ln)  # preserve relative indentation
    return "".join(out_lines)


def instant_apply(
    source: str,
    original: str,
    replacement: str,
    *,
    min_confidence: float = 0.7,
) -> InstantApplyResult:
    """Apply `replacement` in place of `original` within `source`, locally.

    Phase 1 — pattern: exact substring, then dedent-normalized block match.
              Deterministic, confidence 1.0 / 0.98.
    Phase 2 — fuzzy: find the function/class chunk most textually similar to
              `original` and splice `replacement` into its line span. Confidence
              is the similarity ratio; only returned when >= `min_confidence`.

    On a miss, `merged` is None and `confidence` carries the best ratio seen so
    a caller can decide how far it was from a usable merge.
    """
    original = _unescape(original)
    replacement = _unescape(replacement)

    if not original.strip():
        # An empty target would match everywhere — treat as unmergeable rather
        # than silently corrupting the source.
        return InstantApplyResult(None, 0.0, NONE)

    # --- Phase 1a: exact substring replace -------------------------------
    if original in source:
        return InstantApplyResult(
            source.replace(original, replacement, 1), 1.0, EXACT_REPLACE
        )

    # --- Phase 1b: dedent-normalized block replace -----------------------
    orig_stripped = textwrap.dedent(original).strip()
    src_lines = source.splitlines(keepends=True)
    target_n = len(orig_stripped.splitlines())
    if target_n:
        for i in range(len(src_lines) - target_n + 1):
            block = "".join(src_lines[i : i + target_n])
            if textwrap.dedent(block).strip() == orig_stripped:
                indent = block[: len(block) - len(block.lstrip())]
                merged = source.replace(block, _reindent(replacement, indent), 1)
                return InstantApplyResult(merged, 0.98, DEDENT_REPLACE)

    # --- Phase 2: fuzzy chunk-similarity merge ---------------------------
    best_ratio = 0.0
    best: Optional[_Chunk] = None
    for chunk in _chunks(source):
        ratio = difflib.SequenceMatcher(
            None,
            textwrap.dedent(chunk.text).strip(),
            orig_stripped,
        ).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, chunk

    if best is not None and best_ratio >= min_confidence:
        new_block = _reindent(replacement, best.indent)
        if not new_block.endswith("\n") and best.text.endswith("\n"):
            new_block += "\n"
        merged = (
            "".join(src_lines[: best.start])
            + new_block
            + "".join(src_lines[best.end :])
        )
        return InstantApplyResult(merged, round(best_ratio, 3), FUZZY_REPLACE)

    return InstantApplyResult(None, round(best_ratio, 3), NONE)
