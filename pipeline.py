"""
pact pipeline — prompt-based orchestrator that routes intent findings to formal tools.

Reads intent JSON produced by `pact intent analyze`, generates a tool-invocation
plan via LLM, then executes it deterministically:

  intent JSON
      ↓
  plan prompt (LLM) → [{tool, module, contract, obligation, ...}]
      ↓
  execute steps in dependency order:
    z3         → verify_contract()       (single-call behavioral contracts)
    tla        → generate_tla_spec()     (cross-call temporal obligations)
    hypothesis → stress_contract()       (adversarial inputs from contract)
    heal       → heal_project()           (minimal structural fix, CEGIS-verified)

Usage:
    pact pipeline <intent_json>
    pact pipeline intent_pact_self.json --model claude-sonnet-4-6 -v
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_PROMPT_DIR = Path(__file__).parent / "prompts"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_MAX_STEPS = 8


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    step: int
    tool: str
    module_path: str
    status: str  # "verified" | "violated" | "unknown" | "skipped" | "error"
    summary: str
    counterexample: Optional[str] = None
    details: dict = field(default_factory=dict)


@dataclass
class PipelineResult:
    intent_file: str
    plan: list[dict]
    results: list[StepResult]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "intent_file": self.intent_file,
                "plan": self.plan,
                "results": [asdict(r) for r in self.results],
            },
            indent=indent,
        )

    def violated_steps(self) -> list[StepResult]:
        return [r for r in self.results if r.status == "violated"]

    def summary(self) -> str:
        total = len(self.results)
        violated = len(self.violated_steps())
        verified = sum(1 for r in self.results if r.status == "verified")
        return (
            f"{total} step(s) executed: {verified} verified, "
            f"{violated} violated, "
            f"{total - verified - violated} other"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_prompt(name: str) -> str:
    p = _PROMPT_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"Prompt not found: {p}")
    return p.read_text(encoding="utf-8")


def _render(template: str, **kwargs: Any) -> str:
    for k, v in kwargs.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def _call_llm(prompt: str, model: str, key: str) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


def _intent_summary(intent: dict) -> str:
    """Distill intent JSON into a compact summary for the plan prompt."""
    lines: list[str] = []
    for m in intent.get("modules", []):
        path = m.get("path", "?")
        u = m.get("understanding", {})
        contract = u.get("behavioral_contract", "")
        obligations = u.get("resource_obligations", "")
        violations = m.get("violations", [])
        invariants = m.get("invariants", [])

        if not violations and not obligations:
            continue

        lines.append(f"\n### {Path(path).name}  ({path})")
        if contract:
            lines.append(f"behavioral_contract: {contract[:300]}")
        if obligations and obligations != "(none detected in visible source)":
            lines.append(f"resource_obligations: {obligations[:400]}")

        high_invs = [
            i
            for i in invariants
            if i.get("type") == "intent_gap" and i.get("confidence", 0) >= 0.85
        ]
        for inv in high_invs[:3]:
            lines.append(
                f"intent_gap [{inv.get('id')}] confidence={inv.get('confidence')}: "
                f"{inv.get('statement', '')[:200]}"
            )

        for v in violations[:4]:
            lines.append(
                f"violation [{v.get('severity')}]: {v.get('explanation', '')[:200]}"
            )

    return "\n".join(lines) if lines else "(no actionable findings)"


def _topo_sort(steps: list[dict]) -> list[dict]:
    """Return steps in dependency order (topological sort on depends_on)."""
    by_step = {s["step"]: s for s in steps}
    visited: set[int] = set()
    result: list[dict] = []

    def visit(n: int) -> None:
        if n in visited:
            return
        visited.add(n)
        for dep in by_step.get(n, {}).get("depends_on", []):
            visit(dep)
        if n in by_step:
            result.append(by_step[n])

    for s in steps:
        visit(s["step"])
    return result


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _execute_z3(step: dict, source: str, key: str, model: str) -> StepResult:
    from .contract_encoder import verify_contract

    result = verify_contract(
        contract=step.get("contract", ""),
        function_source=source,
        function_name=step.get("function_name") or "",
        api_key=key,
        model=model,
        source_file=step.get("module_path"),
    )
    status = (
        "verified"
        if result.status == "unsat"
        else "violated" if result.status == "sat" else "unknown"
    )
    return StepResult(
        step=step["step"],
        tool="z3",
        module_path=step.get("module_path", ""),
        status=status,
        summary=result.explanation or result.status,
        counterexample=(
            json.dumps(result.counterexample) if result.counterexample else None
        ),
        details={"z3_status": result.status, "encoding": result.encoding_approach},
    )


def _execute_hypothesis(step: dict, source: str, key: str, model: str) -> StepResult:
    from .hypothesis_generator import stress_contract

    result = stress_contract(
        contract=step.get("contract", ""),
        function_source=source,
        function_name=step.get("function_name") or "",
        api_key=key,
        model=model,
    )
    status = (
        "violated"
        if result.status == "falsified"
        else "verified" if result.status == "passed" else "unknown"
    )
    return StepResult(
        step=step["step"],
        tool="hypothesis",
        module_path=step.get("module_path", ""),
        status=status,
        summary=result.explanation or result.status,
        counterexample=result.counterexample,
        details={"hypothesis_status": result.status},
    )


def _execute_tla(step: dict, verbose: bool) -> StepResult:
    """Generate a TLA+ spec for a resource obligation. Does not run TLC (requires Java)."""
    obligation = step.get("obligation", "")
    spec_template = step.get("spec_template", "resource_lifecycle")
    module_path = step.get("module_path", "")
    fn = step.get("function_name") or "module"

    # Generate a minimal TLA+ spec from the obligation text
    spec = _render_tla_spec(
        module_name=Path(module_path).stem if module_path else "Unknown",
        function_name=fn,
        obligation=obligation,
        spec_template=spec_template,
    )

    # Write spec to docs/tla/ alongside existing specs
    out_dir = Path(__file__).parent / "docs" / "tla" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{Path(module_path).stem}_{fn}_{spec_template}"
    tla_path = out_dir / f"{stem}.tla"
    tla_path.write_text(spec)
    if verbose:
        print(f"  TLA+ spec written: {tla_path}")

    return StepResult(
        step=step["step"],
        tool="tla",
        module_path=module_path,
        status="unknown",  # TLC not run — requires Java
        summary=f"Spec generated at {tla_path}. Run TLC to verify: □({obligation[:80]}...)",
        details={"spec_path": str(tla_path), "template": spec_template},
    )


def _render_tla_spec(
    module_name: str,
    function_name: str,
    obligation: str,
    spec_template: str,
) -> str:
    """Render a TLA+ spec skeleton for the given resource obligation."""
    safe_mod = "".join(c if c.isalnum() else "_" for c in module_name)
    safe_fn = "".join(c if c.isalnum() else "_" for c in function_name)
    spec_name = f"{safe_mod}_{safe_fn}_{spec_template}"

    if spec_template == "resource_lifecycle":
        return f"""\
---- MODULE {spec_name} ----
(*
 * Resource lifecycle obligation for {module_name}.{function_name}:
 *   {obligation[:200]}
 *
 * Safety property: resource count never exceeds bound across all calls.
 * Liveness property: any acquired resource is eventually released.
 *
 * Generated by pact pipeline. Run with TLC to verify.
 *)
EXTENDS Integers, TLC

VARIABLES resource_count

TypeInvariant == resource_count \\in Nat

Init == resource_count = 0

Acquire == resource_count' = resource_count + 1
Release == resource_count > 0 /\\ resource_count' = resource_count - 1

Next == Acquire \\/ Release

Spec == Init /\\ [][Next]_resource_count

(* Safety: count never exceeds 1 per caller *)
ResourceBounded == resource_count <= 1

(* Liveness: acquired resources are eventually released *)
EventualRelease == [](resource_count > 0 => <>(resource_count = 0))

THEOREM Spec => [](TypeInvariant /\\ ResourceBounded)
====
"""
    elif spec_template == "ordering":
        return f"""\
---- MODULE {spec_name} ----
(*
 * Ordering constraint for {module_name}.{function_name}:
 *   {obligation[:200]}
 *
 * Safety: target operation never precedes required setup.
 *
 * Generated by pact pipeline. Run with TLC to verify.
 *)
EXTENDS TLC

VARIABLES phase

TypeInvariant == phase \\in {{"uninitialized", "initialized", "running", "done"}}

Init == phase = "uninitialized"

Setup   == phase = "uninitialized" /\\ phase' = "initialized"
Run     == phase = "initialized"   /\\ phase' = "running"
Finish  == phase = "running"       /\\ phase' = "done"

Next == Setup \\/ Run \\/ Finish

Spec == Init /\\ [][Next]_phase

(* Safety: Run never happens before Setup *)
OrderingRespected == [](phase = "running" => phase # "uninitialized")

THEOREM Spec => [](TypeInvariant /\\ OrderingRespected)
====
"""
    elif spec_template == "accumulation":
        return f"""\
---- MODULE {spec_name} ----
(*
 * Accumulation bound for {module_name}.{function_name}:
 *   {obligation[:200]}
 *
 * Safety: accumulated state size is bounded.
 *
 * Generated by pact pipeline. Run with TLC to verify.
 *)
EXTENDS Integers, TLC

CONSTANTS MaxSize

VARIABLES state_size

TypeInvariant == state_size \\in 0..MaxSize

Init == state_size = 0

Append == state_size < MaxSize /\\ state_size' = state_size + 1
Clear  == state_size > 0       /\\ state_size' = 0

Next == Append \\/ Clear

Spec == Init /\\ [][Next]_state_size

AccumulationBounded == state_size <= MaxSize

THEOREM Spec => [](TypeInvariant /\\ AccumulationBounded)
====
"""
    else:  # liveness / default
        return f"""\
---- MODULE {spec_name} ----
(*
 * Liveness obligation for {module_name}.{function_name}:
 *   {obligation[:200]}
 *
 * Generated by pact pipeline. Run with TLC to verify.
 *)
EXTENDS TLC

VARIABLES done

Init == done = FALSE
Complete == done = FALSE /\\ done' = TRUE
Next == Complete \\/ UNCHANGED done
Spec == Init /\\ [][Next]_done /\\ WF_done(Complete)

EventualCompletion == <>(done = TRUE)

THEOREM Spec => EventualCompletion
====
"""


def _load_source(module_path: str) -> str:
    try:
        return Path(module_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _execute_heal(
    step: dict,
    intent_path: Path,
    key: str,
    model: str,
    verbose: bool,
) -> StepResult:
    from .heal import heal_project

    module_path = step.get("module_path", "")
    result = heal_project(
        violations_path=intent_path,
        model=model,
        api_key=key,
        severity_filter=["critical", "high", "medium"],
        apply=False,
        verbose=verbose,
        project_root=Path(module_path).parent if module_path else None,
    )
    accepted = result.patches_accepted
    attempted = result.violations_attempted
    summary = f"{accepted}/{attempted} patch(es) verified (dry-run — use pact heal --apply to write)"
    return StepResult(
        step=step["step"],
        tool="heal",
        module_path=module_path,
        status="verified" if accepted > 0 else "unknown",
        summary=summary,
        details={
            "patches_accepted": accepted,
            "patches_rejected": result.patches_rejected,
            "violations_attempted": attempted,
        },
    )


def _execute_step(
    step: dict,
    prior_results: dict[int, StepResult],
    intent_path: Path,
    key: str,
    model: str,
    verbose: bool,
) -> StepResult:
    tool = step.get("tool", "")
    module_path = step.get("module_path", "")
    source = _load_source(module_path)

    if verbose:
        fn = step.get("function_name") or "module-level"
        print(f"  step {step['step']}: {tool} → {Path(module_path).name}:{fn}")

    # Skip heal if no prior dependency confirmed a violation
    deps = step.get("depends_on", [])
    if tool == "heal":
        dep_violated = any(
            prior_results.get(d, StepResult(d, "", "", "unknown", "")).status
            == "violated"
            for d in deps
        )
        if not dep_violated:
            return StepResult(
                step=step["step"],
                tool=tool,
                module_path=module_path,
                status="skipped",
                summary="No violation confirmed by dependencies — heal not needed",
            )

    try:
        if tool == "z3":
            return _execute_z3(step, source, key, model)
        elif tool == "hypothesis":
            return _execute_hypothesis(step, source, key, model)
        elif tool == "tla":
            return _execute_tla(step, verbose)
        elif tool == "heal":
            return _execute_heal(step, intent_path, key, model, verbose)
        else:
            return StepResult(
                step=step["step"],
                tool=tool,
                module_path=module_path,
                status="skipped",
                summary=f"Tool '{tool}' not yet wired in execution layer",
            )
    except Exception as exc:
        return StepResult(
            step=step["step"],
            tool=tool,
            module_path=module_path,
            status="error",
            summary=str(exc),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_pipeline(
    intent_path: Path,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    verbose: bool = False,
) -> PipelineResult:
    """
    Run the pact verification pipeline from an intent JSON file.

    Generates a plan via LLM then executes Z3, TLA+, and Hypothesis steps
    in dependency order. Returns structured results for all steps.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    intent = json.loads(intent_path.read_text())
    summary = _intent_summary(intent)

    if verbose:
        print(f"pact pipeline: planning from {intent_path.name}")
        print(f"  actionable modules: {summary.count('###')}")

    # Generate plan
    template = _load_prompt("plan")
    prompt = _render(template, intent_summary=summary)
    plan = _call_llm(prompt, model, key)

    # Cap at MAX_STEPS
    plan = plan[:_MAX_STEPS]

    if verbose:
        print(f"  plan: {len(plan)} step(s)")
        for s in plan:
            print(
                f"    step {s.get('step')}: {s.get('tool')} → {s.get('rationale', '')[:60]}"
            )

    if not plan:
        return PipelineResult(
            intent_file=str(intent_path),
            plan=[],
            results=[],
        )

    # Execute in dependency order
    ordered = _topo_sort(plan)
    results: list[StepResult] = []
    prior: dict[int, StepResult] = {}

    for step in ordered:
        result = _execute_step(step, prior, intent_path, key, model, verbose)
        results.append(result)
        prior[result.step] = result

    return PipelineResult(
        intent_file=str(intent_path),
        plan=plan,
        results=results,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="pact pipeline: route intent findings to Z3, TLA+, Hypothesis"
    )
    parser.add_argument("intent_json", help="Path to intent JSON file")
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    parser.add_argument("--out", help="Write pipeline results to JSON file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    result = run_pipeline(
        intent_path=Path(args.intent_json),
        model=args.model,
        verbose=args.verbose,
    )

    print(f"\npact pipeline: {result.summary()}")
    for r in result.results:
        icon = {
            "verified": "✓",
            "violated": "✗",
            "unknown": "?",
            "skipped": "–",
            "error": "!",
        }.get(r.status, "?")
        print(
            f"  {icon} step {r.step} [{r.tool}] {Path(r.module_path).name}: {r.summary[:80]}"
        )
        if r.counterexample:
            print(f"      counterexample: {r.counterexample[:120]}")

    if args.out:
        Path(args.out).write_text(result.to_json())
        print(f"\nResults written to {args.out}")

    return 1 if result.violated_steps() else 0
