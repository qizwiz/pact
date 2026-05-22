"""
spec_learner.py — ML loop for learning TLA+ abstraction refinements from bugs.

Pipeline:
  bug → spec_gap (identify abstraction miss) → spec_refine (propose fix)
       → spec_validate (symbolic check) → corpus (structured training data)
       → spec_gap_improve / spec_refine_improve / spec_validate_improve (self-repair)

Each triple (bug, gap, refinement) is a training example. Over time the corpus
drives the improve prompts toward refinements that TLC can verify.

Usage:
    python -m pact.spec_learner record --bug-desc "..." --bug-file f.py \
        --bug-line 42 --bug-fix "added check=True" --tla docs/tla/PactLoop.tla
    python -m pact.spec_learner report
    python -m pact.spec_learner improve  # trigger prompt self-improvement
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

_CORPUS_PATH = Path(__file__).parent / "corpus" / "spec_gaps.jsonl"
_PROMPT_DIR = Path(__file__).parent / "prompts"
_DEFAULT_MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SpecGapRecord:
    """One training example: a bug that escaped a TLA+ spec."""

    timestamp: float = field(default_factory=time.time)

    # The concrete bug
    bug_description: str = ""
    bug_file: str = ""
    bug_line: int = 0
    bug_manifestation: str = ""
    bug_fix: str = ""

    # The TLA+ spec that missed it
    tla_spec_path: str = ""
    tla_spec_text: str = ""
    tlc_output: str = ""

    # Gap analysis output (from spec_gap prompt)
    gap_name: str = ""
    variable: str = ""
    abstraction_level_before: str = ""
    abstraction_level_after: str = ""
    gap_description: str = ""
    execution_trace: list[str] = field(default_factory=list)
    invariant_that_should_have_failed: str = ""
    tla_refinement: str = ""
    state_space_multiplier: str = ""
    gap_confidence: float = 0.0

    # Refinement output (from spec_refine prompt)
    variable_changes: list[dict] = field(default_factory=list)
    new_operators: str = ""
    new_invariants: list[dict] = field(default_factory=list)
    modified_actions: list[dict] = field(default_factory=list)
    tlc_config_additions: str = ""
    verification_claim: str = ""
    refine_confidence: float = 0.0

    # Validation output (from spec_validate prompt)
    verdict: str = ""  # CATCHES_BUG | MISSES_BUG | BREAKS_SPEC | UNBOUNDED | UNCERTAIN
    bug_replay: list[dict] = field(default_factory=list)
    bounded: bool = False
    estimated_states: str = ""
    refinement_quality: str = ""
    validate_confidence: float = 0.0

    # Actual TLC result (ground truth, filled in if TLC is run)
    tlc_actual_result: str = ""
    tlc_matches_prediction: Optional[bool] = None


def _call(prompt: str, model: str, key: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=(
            "You are a formal methods expert. "
            "Return JSON only — no markdown fences, no text outside the JSON."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM returned invalid JSON: {exc}\n\nRaw text:\n{text}"
        ) from exc


def _render(template_name: str, **kwargs) -> str:
    template = (_PROMPT_DIR / template_name).read_text(encoding="utf-8")
    for k, v in kwargs.items():
        template = template.replace(f"{{{{{k}}}}}", str(v))
    return template


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def analyze_gap(
    record: SpecGapRecord,
    model: str = _DEFAULT_MODEL,
    key: str = "",
    verbose: bool = False,
) -> SpecGapRecord:
    """Run spec_gap prompt to identify the abstraction gap."""
    prompt = _render(
        "spec_gap.md",
        bug_description=record.bug_description,
        bug_file=record.bug_file,
        bug_line=record.bug_line,
        bug_manifestation=record.bug_manifestation,
        bug_fix=record.bug_fix,
        tla_spec=record.tla_spec_text,
        tlc_output=record.tlc_output or "(TLC was not run — spec passed)",
    )
    if verbose:
        print("[spec_learner] running spec_gap...")
    try:
        raw = _call(prompt, model, key)
        record.gap_name = raw.get("gap_name", "")
        record.variable = raw.get("variable", "")
        record.abstraction_level_before = raw.get("abstraction_level_before", "")
        record.abstraction_level_after = raw.get("abstraction_level_after", "")
        record.gap_description = raw.get("gap_description", "")
        record.execution_trace = raw.get("execution_trace", [])
        record.invariant_that_should_have_failed = raw.get(
            "invariant_that_should_have_failed", ""
        )
        record.tla_refinement = raw.get("tla_refinement", "")
        record.state_space_multiplier = raw.get("state_space_multiplier", "unknown")
        record.gap_confidence = float(raw.get("confidence", 0.0))
        if verbose:
            print(f"  gap: {record.gap_name} ({record.gap_confidence:.2f})")
    except Exception as exc:
        if verbose:
            print(f"  spec_gap failed: {exc}")
    return record


def propose_refinement(
    record: SpecGapRecord,
    tla_config_text: str = "",
    model: str = _DEFAULT_MODEL,
    key: str = "",
    verbose: bool = False,
) -> SpecGapRecord:
    """Run spec_refine prompt to generate a concrete spec refinement."""
    prompt = _render(
        "spec_refine.md",
        gap_name=record.gap_name,
        variable=record.variable,
        abstraction_level_before=record.abstraction_level_before,
        abstraction_level_after=record.abstraction_level_after,
        gap_description=record.gap_description,
        bug_description=record.bug_description,
        invariant_that_should_have_failed=record.invariant_that_should_have_failed,
        tla_spec=record.tla_spec_text,
        tlc_config=tla_config_text,
    )
    if verbose:
        print("[spec_learner] running spec_refine...")
    try:
        raw = _call(prompt, model, key)
        record.variable_changes = raw.get("variable_changes", [])
        record.new_operators = raw.get("new_operators", "")
        record.new_invariants = raw.get("new_invariants", [])
        record.modified_actions = raw.get("modified_actions", [])
        record.tlc_config_additions = raw.get("tlc_config_additions", "")
        record.verification_claim = raw.get("verification_claim", "")
        record.state_space_multiplier = raw.get(
            "state_space_multiplier", record.state_space_multiplier
        )
        record.refine_confidence = float(raw.get("confidence", 0.0))
        if verbose:
            n = len(record.new_invariants)
            print(
                f"  refinement: {n} new invariant(s) ({record.refine_confidence:.2f})"
            )
    except Exception as exc:
        if verbose:
            print(f"  spec_refine failed: {exc}")
    return record


def validate_refinement(
    record: SpecGapRecord,
    model: str = _DEFAULT_MODEL,
    key: str = "",
    verbose: bool = False,
) -> SpecGapRecord:
    """Run spec_validate prompt to symbolically check the refinement."""
    new_invariants_tla = "\n".join(
        f"{inv.get('name', '?')} == {inv.get('tla', '?')}"
        for inv in record.new_invariants
    )
    modified_actions_tla = "\n\n".join(
        f"{a.get('action', '?')} ==\n{a.get('tla', '')}"
        for a in record.modified_actions
    )
    prompt = _render(
        "spec_validate.md",
        bug_description=record.bug_description,
        execution_trace="\n".join(
            f"  {i+1}. {s}" for i, s in enumerate(record.execution_trace)
        ),
        gap_name=record.gap_name,
        gap_description=record.gap_description,
        variable_changes=json.dumps(record.variable_changes, indent=2),
        new_invariants=new_invariants_tla,
        modified_actions=modified_actions_tla,
        tla_spec=record.tla_spec_text,
    )
    if verbose:
        print("[spec_learner] running spec_validate...")
    try:
        raw = _call(prompt, model, key)
        record.verdict = raw.get("verdict", "UNCERTAIN")
        record.bug_replay = raw.get("bug_replay", [])
        assessment = raw.get("state_space_assessment", {})
        record.bounded = bool(assessment.get("bounded", False))
        record.estimated_states = str(assessment.get("estimated_states", "unknown"))
        record.refinement_quality = raw.get("refinement_quality", "")
        record.validate_confidence = float(raw.get("confidence", 0.0))
        if verbose:
            print(f"  verdict: {record.verdict} ({record.validate_confidence:.2f})")
    except Exception as exc:
        if verbose:
            print(f"  spec_validate failed: {exc}")

    # Ground-truth TLC check — runs only when the record has a spec and a
    # tla_spec_path that we can pass to TLC (or when we can locate a .tla
    # file next to the source file being analysed).
    if record.tla_spec_text:
        record = _run_tlc_on_spec(record, verbose=verbose)

    return record


def _run_tlc_on_spec(record: "SpecGapRecord", verbose: bool = False) -> "SpecGapRecord":
    """Write spec+config to a temp dir, run TLC, parse result, update record.

    TLC is expected at ``java -jar ~/.local/share/tla2tools.jar`` (standard
    pact dev setup).  Silently skips if java or the jar are unavailable.
    """
    import shutil
    import subprocess
    import tempfile

    java = shutil.which("java")
    jar = Path.home() / ".local" / "share" / "tla2tools.jar"
    if not java or not jar.exists():
        return record

    # The spec text the LLM produced (may include new_invariants and actions)
    spec_text = record.tla_spec_text.strip()
    if not spec_text:
        return record

    # Derive a module name from the first MODULE line or fall back to Spec
    import re

    m = re.search(r"MODULE\s+(\w+)", spec_text)
    module_name = m.group(1) if m else "PactSpec"

    # Emit new_invariants and modified_actions as extra TLA+ operators appended
    # to the spec (before the final ====)
    extras: list[str] = []
    for inv in record.new_invariants:
        name = inv.get("name", "")
        tla = inv.get("tla", "")
        if name and tla:
            extras.append(f"\n{name} == {tla}")
    for act in record.modified_actions:
        action = act.get("action", "")
        tla = act.get("tla", "")
        if action and tla:
            extras.append(f"\n{action} ==\n{tla}")

    if extras:
        spec_text = re.sub(r"={4,}\s*$", "\n".join(extras) + "\n" + "=" * 77, spec_text)

    # Build a minimal cfg from tlc_config_additions (if any) plus SPECIFICATION
    cfg_lines = ["SPECIFICATION Spec"]
    if record.tlc_config_additions.strip():
        cfg_lines.append(record.tlc_config_additions.strip())
    # Add invariants we injected
    inv_names = [
        inv.get("name", "") for inv in record.new_invariants if inv.get("name")
    ]
    if inv_names:
        cfg_lines.append("INVARIANTS " + " ".join(inv_names))
    cfg_text = "\n".join(cfg_lines)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tla_path = Path(tmp) / f"{module_name}.tla"
            cfg_path = Path(tmp) / f"{module_name}.cfg"
            tla_path.write_text(spec_text, encoding="utf-8")
            cfg_path.write_text(cfg_text, encoding="utf-8")

            proc = subprocess.run(
                [
                    java,
                    "-XX:+UseParallelGC",
                    "-jar",
                    str(jar),
                    "-config",
                    str(cfg_path),
                    "-deadlock",
                    str(tla_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = proc.stdout + proc.stderr
            if verbose:
                print(f"  tlc: exit={proc.returncode}, output={output[-200:]!r}")

            no_error = "No error has been found" in output
            error_found = "Error:" in output and not no_error
            record.tlc_actual_result = (
                "CLEAN" if no_error else ("ERROR" if error_found else "UNKNOWN")
            )
            # Does TLC agree with the LLM verdict?
            llm_says_catches = record.verdict == "CATCHES_BUG"
            record.tlc_matches_prediction = (llm_says_catches and error_found) or (
                not llm_says_catches and no_error
            )
            if verbose:
                print(
                    f"  tlc: {record.tlc_actual_result}, matches_prediction={record.tlc_matches_prediction}"
                )
    except Exception as exc:
        if verbose:
            print(f"  tlc: skipped ({exc})")

    return record


# ---------------------------------------------------------------------------
# Corpus management
# ---------------------------------------------------------------------------


def save(record: SpecGapRecord) -> None:
    _CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CORPUS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record)) + "\n")


def load_corpus() -> list[SpecGapRecord]:
    if not _CORPUS_PATH.exists():
        return []
    records = []
    for line in _CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(SpecGapRecord(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            pass
    return records


def report(records: list[SpecGapRecord]) -> str:
    if not records:
        return "No records yet."
    lines = [f"Corpus: {len(records)} bug(s)\n"]

    catches = [r for r in records if r.verdict == "CATCHES_BUG"]
    misses = [r for r in records if r.verdict == "MISSES_BUG"]
    breaks = [r for r in records if r.verdict == "BREAKS_SPEC"]
    unbounded = [r for r in records if r.verdict == "UNBOUNDED"]
    uncertain = [r for r in records if r.verdict in ("UNCERTAIN", "")]

    lines.append(
        f"Verdicts: CATCHES={len(catches)} MISSES={len(misses)} "
        f"BREAKS={len(breaks)} UNBOUNDED={len(unbounded)} UNCERTAIN={len(uncertain)}"
    )

    if catches:
        lines.append("\nSuccessful refinements:")
        for r in catches:
            lines.append(
                f"  {r.gap_name}: {r.bug_file}:{r.bug_line} → {r.abstraction_level_before} → {r.abstraction_level_after}"
            )

    gap_freq: dict[str, int] = {}
    for r in records:
        gap_freq[r.gap_name] = gap_freq.get(r.gap_name, 0) + 1
    if gap_freq:
        lines.append("\nMost common gaps:")
        for g, c in sorted(gap_freq.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  {g}: {c}x")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-improvement
# ---------------------------------------------------------------------------


def _improve_prompt(
    prompt_name: str,
    improve_name: str,
    good_records: list[SpecGapRecord],
    bad_records: list[SpecGapRecord],
    failure_mode: str,
    current_score: float,
    model: str,
    key: str,
    verbose: bool,
) -> None:
    current = (_PROMPT_DIR / prompt_name).read_text(encoding="utf-8")
    good_samples = json.dumps([asdict(r) for r in good_records[:3]], indent=2)
    bad_samples = json.dumps([asdict(r) for r in bad_records[:3]], indent=2)

    improve_template = (_PROMPT_DIR / improve_name).read_text(encoding="utf-8")
    prompt = (
        improve_template.replace("{{prompt_text}}", current)
        .replace("{{good_samples}}", good_samples)
        .replace("{{bad_samples}}", bad_samples)
        .replace("{{failure_mode}}", failure_mode)
        .replace("{{current_score}}", f"{current_score:.2f}")
    )
    if verbose:
        print(f"[spec_learner] improving {prompt_name}...")
    try:
        raw = _call(prompt, model, key)
        improved = raw.get("improved_prompt", "")
        score = float(raw.get("overall_score", 0.0))
        if improved and score > current_score:
            (_PROMPT_DIR / prompt_name).write_text(improved, encoding="utf-8")
            if verbose:
                print(
                    f"  {prompt_name} rewritten (score {current_score:.2f} → {score:.2f})"
                )
        elif verbose:
            print(
                f"  {prompt_name} not improved (score {score:.2f} ≤ {current_score:.2f})"
            )
    except Exception as exc:
        if verbose:
            print(f"  improve failed: {exc}")


def improve(
    model: str = _DEFAULT_MODEL,
    key: str = "",
    verbose: bool = False,
) -> None:
    """Trigger self-improvement for any prompt with a poor track record."""
    records = load_corpus()
    if not records:
        if verbose:
            print("[spec_learner] no corpus yet — nothing to improve from")
        return

    # spec_gap: improves when gap_confidence is low or refinement fails
    gap_good = [
        r for r in records if r.gap_confidence >= 0.7 and r.verdict == "CATCHES_BUG"
    ]
    gap_bad = [
        r for r in records if r.gap_confidence < 0.5 or r.verdict == "MISSES_BUG"
    ]
    if len(gap_bad) >= 2:
        current_score = len(gap_good) / max(len(records), 1)
        _improve_prompt(
            "spec_gap.md",
            "spec_gap_improve.md",
            gap_good,
            gap_bad,
            failure_mode="low_gap_confidence_or_miss",
            current_score=current_score,
            model=model,
            key=key,
            verbose=verbose,
        )

    # spec_refine: improves when validation is MISSES_BUG or BREAKS_SPEC
    refine_good = [r for r in records if r.verdict == "CATCHES_BUG"]
    refine_bad = [r for r in records if r.verdict in ("MISSES_BUG", "BREAKS_SPEC")]
    if len(refine_bad) >= 2:
        current_score = len(refine_good) / max(len(records), 1)
        _improve_prompt(
            "spec_refine.md",
            "spec_refine_improve.md",
            refine_good,
            refine_bad,
            failure_mode=refine_bad[0].verdict if refine_bad else "unknown",
            current_score=current_score,
            model=model,
            key=key,
            verbose=verbose,
        )

    # spec_validate: improves when tlc_matches_prediction is False
    validate_good = [r for r in records if r.tlc_matches_prediction is True]
    validate_bad = [r for r in records if r.tlc_matches_prediction is False]
    if len(validate_bad) >= 2:
        current_score = len(validate_good) / max(len(records), 1)
        _improve_prompt(
            "spec_validate.md",
            "spec_validate_improve.md",
            validate_good,
            validate_bad,
            failure_mode="prediction_mismatch",
            current_score=current_score,
            model=model,
            key=key,
            verbose=verbose,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="TLA+ abstraction gap learner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    rec = sub.add_parser("record", help="Analyze a bug and add it to the corpus")
    rec.add_argument("--bug-desc", required=True)
    rec.add_argument("--bug-file", required=True)
    rec.add_argument("--bug-line", type=int, required=True)
    rec.add_argument("--bug-manifestation", default="")
    rec.add_argument("--bug-fix", default="")
    rec.add_argument("--tla", required=True, help="Path to TLA+ spec")
    rec.add_argument("--tlc-output", default="")
    rec.add_argument("--model", default=_DEFAULT_MODEL)
    rec.add_argument("--verbose", action="store_true")

    sub.add_parser("report", help="Print corpus summary")
    sub.add_parser("improve", help="Trigger prompt self-improvement")

    args = parser.parse_args(argv)
    key = os.environ.get("ANTHROPIC_API_KEY", "")

    if args.cmd == "record":
        tla_path = Path(args.tla)
        record = SpecGapRecord(
            bug_description=args.bug_desc,
            bug_file=args.bug_file,
            bug_line=args.bug_line,
            bug_manifestation=args.bug_manifestation,
            bug_fix=args.bug_fix,
            tla_spec_path=str(tla_path),
            tla_spec_text=(
                tla_path.read_text(encoding="utf-8") if tla_path.exists() else ""
            ),
            tlc_output=args.tlc_output,
        )
        record = analyze_gap(record, model=args.model, key=key, verbose=args.verbose)
        cfg_path = tla_path.with_suffix(".cfg")
        cfg_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
        record = propose_refinement(
            record,
            tla_config_text=cfg_text,
            model=args.model,
            key=key,
            verbose=args.verbose,
        )
        record = validate_refinement(
            record, model=args.model, key=key, verbose=args.verbose
        )
        save(record)
        print(f"Recorded: {record.gap_name} → {record.verdict}")
        return 0

    if args.cmd == "report":
        print(report(load_corpus()))
        return 0

    if args.cmd == "improve":
        improve(model=_DEFAULT_MODEL, key=key, verbose=True)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
