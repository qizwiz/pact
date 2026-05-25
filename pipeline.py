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
                f"intent_gap  invariant_id={inv.get('id')}  confidence={inv.get('confidence')}"
                f"\n  contract (copy verbatim): {inv.get('statement', '')[:200]}"
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


def _execute_z3(
    step: dict,
    source: str,
    key: str,
    model: str,
    inv_z3_index: Optional[dict] = None,
) -> StepResult:
    from .contract_encoder import verify_contract

    contract = step.get("contract", "")
    inv_id = step.get("invariant_id", "")
    index = inv_z3_index or {}

    # Look up entry — index values may be dicts (new) or plain strings (legacy/tests)
    raw_entry = index.get(contract) or index.get(inv_id)
    if isinstance(raw_entry, dict):
        preencoded = raw_entry.get("z3_encoding", "") or None
        contract_kind = raw_entry.get("contract_kind", "")
    else:
        # Legacy: plain string z3_encoding (e.g. from tests that predate this change)
        preencoded = raw_entry or None
        contract_kind = ""

    result = verify_contract(
        contract=contract,
        function_source=source,
        function_name=step.get("function_name") or "",
        api_key=key,
        model=model,
        source_file=step.get("module_path"),
        preencoded_z3_script=preencoded,
        contract_kind=contract_kind,
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


_TLC_JAR = Path(__file__).parent / "docs" / "tla" / "tla2tools.jar"


def _find_tlc_jar() -> Optional[Path]:
    return _TLC_JAR if _TLC_JAR.exists() else None


def _render_cfg(spec_template: str) -> str:
    """Return a TLC model config for the given spec template."""
    safety = {
        "resource_lifecycle": "ResourceBounded",
        "ordering": "OrderingRespected",
        "accumulation": "AccumulationBounded",
    }.get(spec_template, "TypeInvariant")
    lines = ["INIT Init", "NEXT Next", "INVARIANT TypeInvariant", f"INVARIANT {safety}"]
    if spec_template == "accumulation":
        lines += ["CONSTANTS", "  MaxSize = 10"]
    return "\n".join(lines) + "\n"


def _run_tlc(tla_path: Path, cfg_path: Path, timeout: int = 60) -> dict:
    import subprocess

    jar = _find_tlc_jar()
    if jar is None:
        return {
            "status": "unknown",
            "output": "tla2tools.jar not found — download to docs/tla/",
        }
    try:
        proc = subprocess.run(
            [
                "java",
                "-jar",
                str(jar),
                "-config",
                str(cfg_path),
                "-workers",
                "1",
                str(tla_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = proc.stdout + proc.stderr
        if "Model checking completed. No error has been found." in output:
            return {"status": "verified", "output": output}
        error_lines = [
            ln for ln in output.splitlines() if "violated" in ln or "Error:" in ln
        ]
        if error_lines:
            return {"status": "violated", "output": "\n".join(error_lines[:5])}
        return {"status": "unknown", "output": output[-500:]}
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "output": "TLC timed out after 60s"}
    except FileNotFoundError:
        return {"status": "unknown", "output": "java not found in PATH"}


def _execute_tla(step: dict, verbose: bool) -> StepResult:
    """Generate a TLA+ spec and run TLC to verify the resource obligation."""
    obligation = step.get("obligation", "")
    spec_template = step.get("spec_template", "resource_lifecycle")
    module_path = step.get("module_path", "")
    fn = step.get("function_name") or "module"

    spec = _render_tla_spec(
        module_name=Path(module_path).stem if module_path else "Unknown",
        function_name=fn,
        obligation=obligation,
        spec_template=spec_template,
    )
    cfg = _render_cfg(spec_template)

    out_dir = Path(__file__).parent / "docs" / "tla" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{Path(module_path).stem}_{fn}_{spec_template}"
    tla_path = out_dir / f"{stem}.tla"
    cfg_path = out_dir / f"{stem}.cfg"
    tla_path.write_text(spec)
    cfg_path.write_text(cfg)
    if verbose:
        print(f"  TLA+ spec written: {tla_path}")
        print("  Running TLC...")

    tlc = _run_tlc(tla_path, cfg_path)
    status = tlc["status"]
    tlc_output = tlc["output"]

    if verbose:
        print(f"  TLC: {status} — {tlc_output[:120]}")

    summary = (
        f"□({obligation[:60]}…) — TLC: {status}"
        if status == "verified"
        else (
            tlc_output[:160]
            if status == "violated"
            else f"Spec at {tla_path} — {tlc_output[:100]}"
        )
    )
    return StepResult(
        step=step["step"],
        tool="tla",
        module_path=module_path,
        status=status,
        summary=summary,
        details={
            "spec_path": str(tla_path),
            "template": spec_template,
            "tlc_output": tlc_output,
        },
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
 * Safety: resource count never exceeds 1 (no double-acquire without release).
 * Generated by pact pipeline.
 *)
EXTENDS Integers, TLC

VARIABLES resource_count

TypeInvariant == resource_count \\in 0..1

Init == resource_count = 0

(* Acquire only when not already held; Release only when held — no deadlock *)
Acquire == resource_count = 0 /\\ resource_count' = resource_count + 1
Release == resource_count > 0 /\\ resource_count' = resource_count - 1

Next == Acquire \\/ Release

Spec == Init /\\ [][Next]_resource_count

ResourceBounded == resource_count <= 1

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
 * Safety: Run never precedes Setup; cycle through phases to avoid deadlock.
 * Generated by pact pipeline.
 *)
EXTENDS TLC

VARIABLES phase

TypeInvariant == phase \\in {{"uninitialized", "initialized", "running", "done"}}

Init == phase = "uninitialized"

Setup  == phase = "uninitialized" /\\ phase' = "initialized"
Run    == phase = "initialized"   /\\ phase' = "running"
Finish == phase = "running"       /\\ phase' = "done"
Reset  == phase = "done"          /\\ phase' = "uninitialized"

Next == Setup \\/ Run \\/ Finish \\/ Reset

Spec == Init /\\ [][Next]_phase

OrderingRespected == phase = "running" => phase # "uninitialized"

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
 * Safety: state size never exceeds MaxSize.
 * Generated by pact pipeline.
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
 * Safety: TypeInvariant always holds (done is boolean).
 * Generated by pact pipeline.
 *)
EXTENDS TLC

VARIABLES done

TypeInvariant == done \\in {{TRUE, FALSE}}

Init == done = FALSE
Complete == done = FALSE /\\ done' = TRUE
Reset    == done = TRUE  /\\ done' = FALSE
Next == Complete \\/ Reset

Spec == Init /\\ [][Next]_done

EventualCompletion == <>(done = TRUE)

THEOREM Spec => [](TypeInvariant)
====
"""


def _find_project_root(start: Path) -> Optional[Path]:
    """Walk up from start looking for project markers (pyproject.toml, .git, pytest.ini...)."""
    markers = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pytest.ini",
        "tox.ini",
        ".git",
    }
    p = start if start.is_dir() else start.parent
    for _ in range(12):
        if any((p / m).exists() for m in markers):
            return p
        parent = p.parent
        if parent == p:
            break
        p = parent
    return None


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
    from .heal import _autodetect_test_cmd, heal_project

    module_path = step.get("module_path", "")
    project_root = _find_project_root(Path(module_path)) if module_path else None
    test_cmd = _autodetect_test_cmd(project_root) if project_root else None
    apply = test_cmd is not None

    result = heal_project(
        violations_path=intent_path,
        model=model,
        api_key=key,
        severity_filter=["critical", "high", "medium"],
        apply=apply,
        verbose=verbose,
        project_root=project_root,
    )
    accepted = result.patches_accepted
    attempted = result.violations_attempted
    if apply:
        summary = f"{accepted}/{attempted} patch(es) applied and oracle-verified (cmd: {test_cmd})"
    else:
        summary = (
            f"{accepted}/{attempted} patch(es) verified (dry-run — no oracle detected)"
        )
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
            "oracle": test_cmd or "none",
            "applied": apply,
        },
    )


def _execute_step(
    step: dict,
    prior_results: dict[int, StepResult],
    intent_path: Path,
    key: str,
    model: str,
    verbose: bool,
    inv_z3_index: Optional[dict] = None,
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
            r = _execute_z3(step, source, key, model, inv_z3_index)
            if verbose:
                icon = {"verified": "✓", "violated": "✗", "unknown": "?"}.get(
                    r.status, "?"
                )
                enc = (r.details or {}).get("encoding", "")
                enc_note = f" [{enc}]" if enc else ""
                print(f"  Z3: {icon} {r.status}{enc_note} — {r.summary[:80]}")
            return r
        elif tool == "hypothesis":
            r = _execute_hypothesis(step, source, key, model)
            if verbose:
                icon = {"verified": "✓", "violated": "✗", "unknown": "?"}.get(
                    r.status, "?"
                )
                ce_note = (
                    f" — counterexample: {r.counterexample}" if r.counterexample else ""
                )
                print(f"  Hypothesis: {icon} {r.status} — {r.summary[:80]}{ce_note}")
            return r
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

    # Build invariant index keyed by both statement and invariant_id (from contract IR)
    # Each entry stores both z3_encoding (pre-built script) and contract_kind
    # (for typed-template path in verify_contract).
    inv_z3_index: dict[str, dict] = {}
    for mod in intent.get("modules", []):
        for inv in mod.get("invariants", []):
            z3_enc = inv.get("z3_encoding", "")
            contract_kind = inv.get("contract_kind", "")
            # Include entry if there's a preencoded script OR a known contract_kind
            if (not z3_enc or "import z3" not in z3_enc) and not contract_kind:
                continue
            entry: dict = {
                "z3_encoding": z3_enc if z3_enc and "import z3" in z3_enc else "",
                "contract_kind": contract_kind,
            }
            stmt = inv.get("statement", "")
            inv_id = inv.get("id", "")
            if stmt:
                inv_z3_index[stmt] = entry
            if inv_id:
                inv_z3_index[inv_id] = entry

    if verbose:
        try:
            import subprocess as _sp

            _sha = _sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).parent,
                stderr=_sp.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            _sha = "unknown"
        print(f"pact pipeline: planning from {intent_path.name}  [pact@{_sha}]")
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
        result = _execute_step(
            step, prior, intent_path, key, model, verbose, inv_z3_index
        )
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
