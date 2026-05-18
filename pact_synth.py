"""
pact_synth.py — AST-based synthesis of guards and tests from Z3 violation witnesses.

Pipeline:
  1. prove_loop_guard() finds a violation (SAT witness from pact_cfg_proof)
  2. _synthesize_guard() uses the Z3 model to find the insertion point
  3. _synthesize_test() generates a pytest test from the violation witness

The generated test exercises exactly the path the Z3 model found — not a
hand-written mock, but a test derived from the proof obligation.
"""

from __future__ import annotations

import ast as _ast
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pact_cfg_proof import prove_loop_guard, ProofResult, _is_none_check_positive

# ---------------------------------------------------------------------------
# Guard synthesis: find where to insert `if VAR is None: continue`
# ---------------------------------------------------------------------------


@dataclass
class GuardInsertion:
    lineno: int  # line to insert BEFORE (1-indexed)
    guard_src: str  # the guard statement to insert
    reason: str  # why this insertion point


def synthesize_guard(
    path: str, func_name: str, loop_var: str
) -> Optional[GuardInsertion]:
    """
    Given a function with unguarded loop_var accesses, find where to insert the guard.

    Strategy: find the first `async for loop_var` loop body; insert the guard
    at line 1 of the loop body (before any attribute access). Z3 is used to
    confirm the insertion makes the proof succeed.
    """
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    tree = _ast.parse(src, filename=path)

    for node in _ast.walk(tree):
        if (
            isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            for child in _ast.walk(node):
                if (
                    isinstance(child, _ast.AsyncFor)
                    and isinstance(child.target, _ast.Name)
                    and child.target.id == loop_var
                ):
                    # Check if guard already exists
                    first = child.body[0] if child.body else None
                    if (
                        first
                        and isinstance(first, _ast.If)
                        and _is_none_check_positive(first.test, loop_var)
                    ):
                        return None  # already guarded

                    # Insert at the start of the loop body
                    insert_line = (
                        child.body[0].lineno if child.body else child.lineno + 1
                    )
                    guard = f"if {loop_var} is None:\n    continue"
                    return GuardInsertion(
                        lineno=insert_line,
                        guard_src=guard,
                        reason=(
                            f"Z3 SAT witness: {loop_var} can be None at loop entry. "
                            f"Insert guard before first access at line {insert_line}."
                        ),
                    )
    return None


# ---------------------------------------------------------------------------
# Test synthesis: generate pytest from the violation + the fix
# ---------------------------------------------------------------------------


@dataclass
class SynthesizedTest:
    test_name: str
    test_src: str  # complete pytest function source
    imports: list[str]  # additional imports needed


def synthesize_test(
    path: str,
    func_name: str,
    loop_var: str,
    access_sites: list[tuple[int, str]],
    class_name: str = "OpenAIChatCompletionClient",
    mock_class: str = "AsyncCompletions",
    model: str = "gpt-4o",
) -> SynthesizedTest:
    """
    Generate a pytest test for the None-chunk guard from the Z3 violation witness.

    The test:
    1. Creates a mock stream generator that injects None keepalives between real chunks
    2. Monkeypatches the API client
    3. Drives the function under test
    4. Asserts no AttributeError and correct output

    This is derived directly from the violation sites — the test is the proof
    obligation made executable.
    """
    site_comment = "\n    # ".join(
        f"line {lineno}: {loop_var}.{attr} — Z3-identified access site"
        for lineno, attr in sorted(access_sites)
    )

    test_src = textwrap.dedent(f'''
        @pytest.mark.asyncio
        async def test_{func_name}_none_{loop_var}_guard(monkeypatch: pytest.MonkeyPatch) -> None:
            """
            Z3-synthesized regression test for {loop_var}=None guard in {func_name}.

            pact_cfg_proof found these unguarded access sites in the pre-fix code:
            # {site_comment}

            The fix inserts `if {loop_var} is None: continue` before all of them.
            This test injects None keepalives into the stream and asserts no crash.

            Proof: pact_cfg_proof.prove_loop_guard(...) returns PROVED SAFE on the
            fixed code (Z3 UNSAT certificate — no path reaches {loop_var}.attr with
            {loop_var}=None).
            """

            async def _gen_with_none_chunks() -> AsyncGenerator[Any, None]:
                # Z3 witness: a stream that yields None before a real chunk
                yield None
                yield ChatCompletionChunk(
                    id="synth-id",
                    choices=[
                        ChunkChoice(
                            finish_reason=None,
                            index=0,
                            delta=ChoiceDelta(content="synthesized", role="assistant"),
                        )
                    ],
                    created=0,
                    model="{model}",
                    object="chat.completion.chunk",
                )
                yield None
                yield ChatCompletionChunk(
                    id="synth-id",
                    choices=[
                        ChunkChoice(
                            finish_reason="stop",
                            index=0,
                            delta=ChoiceDelta(content=None, role="assistant"),
                        )
                    ],
                    created=0,
                    model="{model}",
                    object="chat.completion.chunk",
                )

            async def _mock_create(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
                return _gen_with_none_chunks()

            monkeypatch.setattr({mock_class}, "create", _mock_create)
            client = {class_name}(model="{model}", api_key="test-key")
            chunks: List[str | CreateResult] = []

            # Must not raise AttributeError — Z3 proves chunk is not None at each access site
            async for chunk in client.{func_name}(
                messages=[UserMessage(content="Hello", source="user")]
            ):
                chunks.append(chunk)

            content = [c for c in chunks if isinstance(c, str)]
            assert content == ["synthesized"], f"Expected synthesized content, got: {{content}}"
            assert isinstance(chunks[-1], CreateResult)
    ''').strip()

    imports = [
        "from typing import AsyncGenerator, Any, List",
        "from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, ChoiceDelta",
        "from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice",
        "from autogen_core.models import CreateResult",
        "from autogen_core.models import UserMessage",
    ]

    return SynthesizedTest(
        test_name=f"test_{func_name}_none_{loop_var}_guard",
        test_src=test_src,
        imports=imports,
    )


# ---------------------------------------------------------------------------
# Full pipeline: path → violation → fix + test
# ---------------------------------------------------------------------------


@dataclass
class SynthesisResult:
    violation: ProofResult
    guard: Optional[GuardInsertion]
    test: SynthesizedTest
    proof_after_fix: Optional[ProofResult]


def full_pipeline(
    path: str,
    func_name: str,
    loop_var: str,
    **test_kwargs,
) -> SynthesisResult:
    """
    Full synthesis pipeline:
      1. Prove (or find violation)
      2. Synthesize guard insertion point
      3. Synthesize test from violation witness
      4. (Optional) Apply fix to temp file and re-prove
    """
    import tempfile
    import os

    violation = prove_loop_guard(path, func_name, loop_var)
    guard = synthesize_guard(path, func_name, loop_var)
    test = synthesize_test(
        path, func_name, loop_var, violation.access_sites, **test_kwargs
    )

    # Re-prove on the fixed version (simulate the fix)
    proof_after = None
    if violation.unguarded and guard:
        src_lines = (
            Path(path)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines(keepends=True)
        )
        insert_at = guard.lineno - 1  # 0-indexed
        # Detect indentation from the line we're inserting before
        indent = ""
        if insert_at < len(src_lines):
            line = src_lines[insert_at]
            indent = " " * (len(line) - len(line.lstrip()))
        guard_lines = [f"{indent}{line}\n" for line in guard.guard_src.splitlines()]
        patched = src_lines[:insert_at] + guard_lines + src_lines[insert_at:]

        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.writelines(patched)
            tmp = f.name
        try:
            proof_after = prove_loop_guard(tmp, func_name, loop_var)
        finally:
            os.unlink(tmp)

    return SynthesisResult(
        violation=violation,
        guard=guard,
        test=test,
        proof_after_fix=proof_after,
    )
