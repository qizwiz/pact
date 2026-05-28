"""
pact hypothesis_generator — adversarial input generation from behavioral contracts.

Takes a behavioral_contract string (natural language from pact intent output)
and a Python function. Uses LLM to translate the contract into a Hypothesis
@given test, then executes it to find worst-case inputs or build confidence
the contract holds under adversarial pressure.

The LLM is a translator only — Hypothesis does all search.

Pipeline:
  1. Extract function source (AST or raw)
  2. LLM: translate behavioral_contract → Hypothesis test  (contract_hypothesis.md)
  3. Execute Hypothesis test in subprocess (60s timeout)
  4. Parse: no counterexample → contract holds under testing;
            counterexample found → concrete worst-case input returned

Usage:
    from pact.hypothesis_generator import stress_contract
    result = stress_contract(
        contract="returns None if key not in mapping, never raises",
        function_source="def lookup(db, key): return db.get(key)",
        function_name="lookup",
        api_key=os.environ["ANTHROPIC_API_KEY"],
    )
    print(result.status)          # "passed" | "falsified" | "error" | "encoding_failed"
    print(result.counterexample)  # repr of worst-case input, or None
    print(result.hypothesis_test) # the generated @given test
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_PROMPT_DIR = Path(__file__).parent / "prompts"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_SUBPROCESS_TIMEOUT = 60  # Hypothesis needs more time than Z3 for search
_SYSTEM = (
    "You are a property-based testing engineer. "
    "Return ONLY valid JSON. No markdown fences, no explanation outside the JSON."
)


@dataclass
class HypothesisStressResult:
    function_name: str
    contract: str
    status: str  # "passed" | "falsified" | "error" | "encoding_failed"
    counterexample: Optional[str] = None  # repr of falsifying input
    explanation: str = ""
    hypothesis_test: str = ""  # the generated @given test
    strategy_description: str = ""
    error: str = ""


def _load_prompt(name: str) -> str:
    p = _PROMPT_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"Prompt not found: {p}")
    return p.read_text(encoding="utf-8")


def _render(template: str, **kwargs) -> str:
    for k, v in kwargs.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        import re

        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                pass
        raise


def _call_llm(prompt: str, model: str, key: str) -> dict:
    from .llm import make_client

    client = make_client(key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.content:
        raise RuntimeError("API returned empty content")
    return _parse_json(response.content[0].text)


def _extract_function_source(source: str, function_name: str) -> str:
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == function_name:
                    lines = source.splitlines()
                    func_lines = lines[node.lineno - 1 : node.end_lineno]
                    return textwrap.dedent("\n".join(func_lines))
    except SyntaxError:
        pass
    return source[:3000]


def _run_hypothesis_test(test_src: str) -> dict:
    """Execute Hypothesis test in a subprocess, parse JSON output."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(test_src)
        script_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if not stdout:
            return {
                "status": "error",
                "counterexample": None,
                "explanation": f"Hypothesis test produced no output. stderr: {stderr[:300]}",
            }

        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass

        return {
            "status": "error",
            "counterexample": None,
            "explanation": f"Could not parse Hypothesis output: {stdout[:200]}",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "counterexample": None,
            "explanation": f"Hypothesis test timed out after {_SUBPROCESS_TIMEOUT}s",
        }
    except Exception as exc:
        return {
            "status": "error",
            "counterexample": None,
            "explanation": f"Subprocess error: {exc}",
        }
    finally:
        Path(script_path).unlink(missing_ok=True)


def stress_contract(
    contract: str,
    function_source: str,
    function_name: str,
    api_key: Optional[str] = None,
    model: str = _DEFAULT_MODEL,
    max_examples: int = 500,
    z3_counterexample: Optional[str] = None,
) -> HypothesisStressResult:
    """
    Stress-test a behavioral contract against a function using Hypothesis.

    Parameters
    ----------
    contract:
        Natural language behavioral contract (from pact intent output).
    function_source:
        The Python source of the function (or full file — will extract by name).
    function_name:
        Name of the function to stress-test.
    api_key:
        Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
    model:
        LLM model for contract → Hypothesis translation.
    max_examples:
        Number of examples Hypothesis will try (default 500, more = more thorough).
    z3_counterexample:
        Optional Z3 counterexample (JSON string) from a prior verification step.
        When provided, the LLM uses it to seed Hypothesis's search toward the
        known-bad region rather than starting from scratch.
    """
    from .llm import resolve_key

    key = resolve_key(api_key)

    func_src = _extract_function_source(function_source, function_name)

    # Build optional Z3 seed section for the prompt
    if z3_counterexample:
        z3_section = (
            "\n## Z3 Counterexample (seed for Hypothesis search)\n\n"
            "Z3 has already found a concrete witness that violates this contract:\n\n"
            f"```\n{z3_counterexample}\n```\n\n"
            "Use this to anchor your Hypothesis strategy: include the Z3 witness values "
            "as an explicit example via `@example(...)` or `st.just(...) | st.integers(...)` "
            "so Hypothesis always exercises the known-bad case and then explores around it.\n"
        )
    else:
        z3_section = ""

    # Step 1: LLM translates contract → Hypothesis test
    try:
        template = _load_prompt("contract_hypothesis")
        prompt = _render(
            template,
            function_name=function_name,
            contract=contract,
            function_source=func_src[:3000],
            max_examples=str(max_examples),
            z3_counterexample_section=z3_section,
        )
        raw = _call_llm(prompt, model, key)
    except Exception as exc:
        return HypothesisStressResult(
            function_name=function_name,
            contract=contract,
            status="encoding_failed",
            explanation="LLM encoding step failed",
            error=str(exc),
        )

    hypothesis_test = raw.get("hypothesis_test", "")
    strategy_description = raw.get("strategy_description", "")

    if not hypothesis_test or "from hypothesis" not in hypothesis_test:
        return HypothesisStressResult(
            function_name=function_name,
            contract=contract,
            status="encoding_failed",
            explanation="LLM did not produce a valid Hypothesis test",
            hypothesis_test=hypothesis_test,
            strategy_description=strategy_description,
            error="no hypothesis import in generated test",
        )

    # Step 2: Run Hypothesis
    h_result = _run_hypothesis_test(hypothesis_test)

    return HypothesisStressResult(
        function_name=function_name,
        contract=contract,
        status=h_result.get("status", "error"),
        counterexample=h_result.get("counterexample"),
        explanation=h_result.get("explanation", ""),
        hypothesis_test=hypothesis_test,
        strategy_description=strategy_description,
    )
