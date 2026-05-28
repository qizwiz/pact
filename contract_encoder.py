"""
pact contract_encoder — Z3-based behavioral contract verification.

Takes a behavioral_contract string (natural language from pact intent output)
and a Python function. Uses LLM to translate the contract to Z3 Python, then
executes it to find concrete counterexamples or prove the contract holds.

The LLM is a translator only — Z3 does all reasoning.

Pipeline:
  1. Extract function source (AST or raw)
  2. LLM: translate behavioral_contract → Z3 Python script  (contract_encode.md)
  3. Execute Z3 script in subprocess (30s timeout, z3+json imports only)
  4. Parse: unsat → contract holds; sat → concrete counterexample returned

Usage:
    from pact.contract_encoder import verify_contract
    result = verify_contract(
        contract="returns None if key not in mapping, never raises",
        function_source="def lookup(db, key): return db.get(key)",
        function_name="lookup",
        api_key=os.environ["ANTHROPIC_API_KEY"],
    )
    print(result.status)          # "unsat" | "sat" | "unknown" | "encoding_failed"
    print(result.counterexample)  # dict of concrete violating inputs, or None
    print(result.z3_script)       # the generated Z3 Python
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_PROMPT_DIR = Path(__file__).parent / "prompts"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_SUBPROCESS_TIMEOUT = 30  # seconds — Z3 can be slow on complex encodings
_SYSTEM = (
    "You are a formal verification engineer. "
    "Return ONLY valid JSON. No markdown fences, no explanation outside the JSON."
)


@dataclass
class ContractVerificationResult:
    function_name: str
    contract: str
    status: str  # "unsat" | "sat" | "unknown" | "encoding_failed"
    counterexample: Optional[dict] = None
    explanation: str = ""
    z3_script: str = ""
    encoding_approach: str = ""
    limitations: str = ""
    error: str = ""
    cegis_reasoning: str = ""  # LLM's analysis of the counterexample (SAT case)


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
    """Extract a single function's source from a larger file."""
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


def _run_z3_script(script: str) -> dict:
    """Execute Z3 script in a subprocess, return parsed JSON output."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
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
        if not stdout:
            stderr = result.stderr.strip()[:500]
            return {
                "status": "unknown",
                "counterexample": None,
                "explanation": f"Z3 script produced no output. stderr: {stderr}",
            }
        # Find the JSON line (last non-empty line)
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        return {
            "status": "unknown",
            "counterexample": None,
            "explanation": f"Z3 script output not parseable: {stdout[:200]}",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "unknown",
            "counterexample": None,
            "explanation": f"Z3 script timed out after {_SUBPROCESS_TIMEOUT}s",
        }
    except Exception as exc:
        return {
            "status": "unknown",
            "counterexample": None,
            "explanation": f"Z3 subprocess error: {exc}",
        }
    finally:
        Path(script_path).unlink(missing_ok=True)


def verify_contract_typed(
    contract: str,
    function_source: str,
    function_name: str,
    contract_kind: str,
    api_key: str,
    model: str = _DEFAULT_MODEL,
) -> ContractVerificationResult:
    """
    Verify a contract using a pre-built Z3 template for the given contract_kind.

    1. Calls LLM with contract_params.md prompt to extract template parameters.
    2. Calls render_z3_template(contract_kind, params) to get a runnable Z3 script.
    3. Runs the Z3 script.
    4. Returns a ContractVerificationResult.

    Returns status="unknown" (not "encoding_failed") on LLM extraction failure so
    the caller can fall through to the free-form path.
    """
    from .contract_templates import SUPPORTED_KINDS, render_z3_template

    if contract_kind not in SUPPORTED_KINDS:
        return ContractVerificationResult(
            function_name=function_name,
            contract=contract,
            status="unknown",
            explanation=f"contract_kind '{contract_kind}' not in SUPPORTED_KINDS",
            encoding_approach="typed_template",
        )

    func_src = _extract_function_source(function_source, function_name)

    # Step 1: extract template params from contract + source
    try:
        template = _load_prompt("contract_params")
        prompt = _render(
            template,
            contract_kind=contract_kind,
            contract=contract,
            function_source=func_src[:3000],
        )
        params = _call_llm(prompt, model, api_key)
    except Exception as exc:
        return ContractVerificationResult(
            function_name=function_name,
            contract=contract,
            status="unknown",
            explanation=f"param extraction failed: {exc}",
            encoding_approach="typed_template",
            error=str(exc),
        )

    # Step 2: render template
    try:
        script = render_z3_template(contract_kind, params)
    except Exception as exc:
        return ContractVerificationResult(
            function_name=function_name,
            contract=contract,
            status="unknown",
            explanation=f"template render failed: {exc}",
            encoding_approach="typed_template",
            error=str(exc),
        )

    # Step 3: run Z3 script
    z3_result = _run_z3_script(script)
    return ContractVerificationResult(
        function_name=function_name,
        contract=contract,
        status=z3_result.get("status", "unknown"),
        counterexample=z3_result.get("counterexample"),
        explanation=z3_result.get("explanation", ""),
        z3_script=script,
        encoding_approach=f"typed_template:{contract_kind}",
    )


def verify_contract(
    contract: str,
    function_source: str,
    function_name: str,
    api_key: Optional[str] = None,
    model: str = _DEFAULT_MODEL,
    source_file: Optional[str] = None,
    preencoded_z3_script: Optional[str] = None,
    contract_kind: str = "",
) -> ContractVerificationResult:
    """
    Verify a behavioral contract against a function using Z3.

    Parameters
    ----------
    contract:
        Natural language behavioral contract (from pact intent output).
        e.g. "returns None if key not in mapping, never raises KeyError"
    function_source:
        The Python source of the function (or full file — will extract by name).
    function_name:
        Name of the function to verify.
    api_key:
        Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
    model:
        LLM model for contract → Z3 translation.
    source_file:
        Optional path hint for error messages.
    preencoded_z3_script:
        Pre-encoded Z3 script from the contract IR (populated during intent analysis).
        When provided, skips the LLM encoding round-trip entirely.
    contract_kind:
        When in SUPPORTED_KINDS, tries verify_contract_typed first before the
        free-form LLM encoding path.
    """
    # Extract just the function if given a full file
    func_src = _extract_function_source(function_source, function_name)

    # Short-circuit: reuse pre-encoded Z3 script from contract IR
    if preencoded_z3_script and "import z3" in preencoded_z3_script:
        z3_result = _run_z3_script(preencoded_z3_script)
        return ContractVerificationResult(
            function_name=function_name,
            contract=contract,
            status=z3_result.get("status", "unknown"),
            counterexample=z3_result.get("counterexample"),
            explanation=z3_result.get("explanation", ""),
            z3_script=preencoded_z3_script,
            encoding_approach="preencoded",
        )

    from .llm import resolve_key

    key = resolve_key(api_key)

    # Typed-template short-circuit: when contract_kind is known, extract params
    # via LLM and render a pre-built template instead of free-form Z3 generation.
    # Falls through to the free-form path if typed result is "unknown".
    if contract_kind:
        from .contract_templates import SUPPORTED_KINDS

        if contract_kind in SUPPORTED_KINDS:
            typed_result = verify_contract_typed(
                contract=contract,
                function_source=function_source,
                function_name=function_name,
                contract_kind=contract_kind,
                api_key=key,
                model=model,
            )
            if typed_result.status != "unknown":
                return typed_result
            # "unknown" → fall through to free-form path

    # Step 1: LLM translates contract → Z3 script
    try:
        template = _load_prompt("contract_encode")
        prompt = _render(
            template,
            function_name=function_name,
            contract=contract,
            function_source=func_src[:3000],
        )
        raw = _call_llm(prompt, model, key)
    except Exception as exc:
        return ContractVerificationResult(
            function_name=function_name,
            contract=contract,
            status="encoding_failed",
            explanation="LLM encoding step failed",
            error=str(exc),
        )

    z3_script = raw.get("z3_script", "")
    encoding_approach = raw.get("encoding_approach", "")
    limitations = raw.get("limitations", "")

    if not z3_script or "import z3" not in z3_script:
        return ContractVerificationResult(
            function_name=function_name,
            contract=contract,
            status="encoding_failed",
            explanation="LLM did not produce a valid Z3 script",
            z3_script=z3_script,
            encoding_approach=encoding_approach,
            limitations=limitations,
            error="no z3 import in generated script",
        )

    # Step 2: Execute Z3 script
    z3_result = _run_z3_script(z3_script)
    status = z3_result.get("status", "unknown")
    counterexample = z3_result.get("counterexample")
    explanation = z3_result.get("explanation", "")
    cegis_reasoning = ""

    # Step 3: CEGIS — when Z3 finds a counterexample, the LLM reasons about it.
    # LLM sees: original contract, function source, Z3 script, concrete violating input.
    # It decides: genuine violation or encoding error? If encoding error, refines the script.
    if status == "sat" and counterexample:
        try:
            cegis_template = _load_prompt("contract_cegis")
            cegis_prompt = _render(
                cegis_template,
                function_name=function_name,
                contract=contract,
                function_source=func_src[:3000],
                z3_script=z3_script,
                counterexample=json.dumps(counterexample, indent=2),
            )
            cegis_raw = _call_llm(cegis_prompt, model, key)
            cegis_reasoning = cegis_raw.get("reasoning", "")
            is_genuine = cegis_raw.get("is_genuine_violation", True)
            refined = cegis_raw.get("refined_z3_script", "")

            if not is_genuine and refined and "import z3" in refined:
                # Encoding was wrong — run the refined script
                refined_result = _run_z3_script(refined)
                if refined_result.get("status") in ("unsat", "sat", "unknown"):
                    status = refined_result.get("status", "unknown")
                    counterexample = refined_result.get("counterexample")
                    explanation = refined_result.get("explanation", "")
                    z3_script = refined  # surface the corrected script
        except Exception as exc:
            warnings.warn(
                f"CEGIS round is best-effort; base result still valid: {exc}",
                RuntimeWarning,
            )

    return ContractVerificationResult(
        function_name=function_name,
        contract=contract,
        status=status,
        counterexample=counterexample,
        explanation=explanation,
        z3_script=z3_script,
        encoding_approach=encoding_approach,
        limitations=limitations,
        cegis_reasoning=cegis_reasoning,
    )


def verify_contracts_from_intent(
    intent_json_path: str,
    source_root: str,
    api_key: Optional[str] = None,
    model: str = _DEFAULT_MODEL,
    severity_filter: str = "high",
) -> list[ContractVerificationResult]:
    """
    Run Z3 contract verification on all violations from a pact intent JSON.

    Filters for intent_gap invariants and high-confidence behavioral contracts,
    then verifies each against its source function using Z3.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        intent_data = json.loads(Path(intent_json_path).read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"Intent JSON is not valid: {intent_json_path}: {exc}"
        ) from exc
    results: list[ContractVerificationResult] = []
    root = Path(source_root)

    for module in intent_data.get("modules", []):
        understanding = module.get("understanding", {})
        contract = understanding.get("behavioral_contract", "")
        if not contract or len(contract) < 20:
            continue

        file_path = Path(module.get("path", ""))
        if not file_path.exists():
            # Try relative to root
            file_path = root / file_path.name
        if not file_path.exists():
            continue

        source = file_path.read_text(encoding="utf-8", errors="replace")

        # Find functions that have intent_gap violations — highest priority
        for inv in module.get("invariants", []):
            if inv.get("type") != "intent_gap":
                continue
            for func_name in inv.get("applies_to", []):
                result = verify_contract(
                    contract=inv.get("statement", contract),
                    function_source=source,
                    function_name=func_name,
                    api_key=key,
                    model=model,
                    source_file=str(file_path),
                )
                results.append(result)

    return results
