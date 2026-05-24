"""
pact intent -- LLM-powered semantic world-model extraction.

Pipeline:
  1. Triage  — identify which files encode essential design decisions
  2. Understand — extract deep world model per module (purpose, design intent,
                  key abstractions, behavioral contract, failure modes, assumptions)
  3. Invariants — derived from the module's own stated intent
  4. Violations — contradictions with the module's own intent
  5. Improve — score output quality, rewrite prompts that underperformed

Prompts live in prompts/*.md and self-improve: each run scores its output and
rewrites weak prompts. The prompts converge toward optimal through use.

Usage:
    pact intent <file.py>                   # single file
    pact intent <dir> --out intent.json     # full project
    pact intent <dir> --improve             # also update prompts after run
    pact intent <dir> --model claude-opus-4-7 --improve
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Prompt loading — prompts are files, not hardcoded strings
# ---------------------------------------------------------------------------

_PROMPT_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template from prompts/<name>.md."""
    p = _PROMPT_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"Prompt not found: {p}")
    return p.read_text(encoding="utf-8")


def _save_prompt(name: str, text: str) -> None:
    """Overwrite prompts/<name>.md with improved text."""
    p = _PROMPT_DIR / f"{name}.md"
    p.write_text(text, encoding="utf-8")


def _render(template: str, **kwargs) -> str:
    """Replace {{key}} placeholders in a prompt template."""
    for k, v in kwargs.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class ProjectUnderstanding:
    purpose: str
    design_intent: str
    key_abstractions: str
    behavioral_contract: str
    failure_modes: str
    assumptions: str


@dataclass
class Invariant:
    id: str
    type: str
    statement: str
    applies_to: list[str]
    formal: str
    derived_from: str
    confidence: float


@dataclass
class Violation:
    invariant_id: str
    file: str
    line: int
    evidence: str
    severity: str
    explanation: str


@dataclass
class ImprovementScore:
    specificity: float
    groundedness: float
    calibration: float
    completeness: float
    actionability: float
    non_obviousness: float

    @property
    def overall(self) -> float:
        return (
            sum(
                [
                    self.specificity,
                    self.groundedness,
                    self.calibration,
                    self.completeness,
                    self.actionability,
                    self.non_obviousness,
                ]
            )
            / 6
        )


@dataclass
class TruncationAudit:
    last_complete_unit: str = ""
    cutoff_line: str = ""
    visible_definitions: list[str] = field(default_factory=list)
    docstring_only_names: list[str] = field(default_factory=list)


@dataclass
class ModuleIntent:
    path: str
    understanding: ProjectUnderstanding
    invariants: list[Invariant] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    prompt_score: Optional[ImprovementScore] = None
    truncation_audit: Optional[TruncationAudit] = None


@dataclass
class ProjectIntent:
    project: str
    generated_at: str
    source_model: str
    project_summary: str = ""
    key_files: list[str] = field(default_factory=list)
    modules: list[ModuleIntent] = field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent)

    def violations_by_severity(self) -> dict[str, list[dict]]:
        out: dict[str, list] = {"critical": [], "high": [], "medium": [], "low": []}
        for m in self.modules:
            for v in m.violations:
                out.setdefault(v.severity, []).append(
                    {
                        "file": v.file,
                        "line": v.line,
                        "invariant": v.invariant_id,
                        "evidence": v.evidence,
                        "explanation": v.explanation,
                    }
                )
        return out

    def avg_prompt_score(self) -> Optional[float]:
        scores = [m.prompt_score.overall for m in self.modules if m.prompt_score]
        return sum(scores) / len(scores) if scores else None


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".git",
        "node_modules",
        "dist",
        "build",
        ".eggs",
    }
)
_MAX_FILE_BYTES = 40_000


def _iter_python_files(root: Path) -> list[Path]:
    results = []
    for p in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name == "conftest.py":
            continue
        results.append(p)
    return results


def _read_truncated(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    if len(raw) > _MAX_FILE_BYTES:
        return raw[:_MAX_FILE_BYTES].decode("utf-8", errors="replace"), True
    return raw.decode("utf-8", errors="replace"), False


def _signature_summary(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source[:2000]
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node) or ""
            if isinstance(node, ast.ClassDef):
                sig = f"class {node.name}"
            else:
                prefix = (
                    "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                )
                args = [a.arg for a in node.args.args]
                sig = f"{prefix} {node.name}({', '.join(args[:8])}{'...' if len(args) > 8 else ''})"
            lines.append(f"  {sig}  # line {node.lineno}")
            if doc:
                lines.append(f'    """{doc[:200]}"""')
    return "\n".join(lines)


def _file_listing(root: Path) -> str:
    files = _iter_python_files(root)
    lines = []
    for f in sorted(files):
        rel = f.relative_to(root)
        size = f.stat().st_size
        lines.append(f"  {rel}  ({size:,} bytes)")
    return "\n".join(lines)


def _collect_readme(root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = root / name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")[:6000]
    return ""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-sonnet-4-6"
_SYSTEM = (
    "You are performing semantic analysis for a formal verification pipeline. "
    "Return ONLY valid JSON. No markdown fences, no explanation outside the JSON."
)


def _get_key(api_key: Optional[str] = None) -> str:
    key = (
        api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("PACT_ANTHROPIC_API_KEY")
    )
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set.\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...   then re-run.\n"
            "  Or pass --api-key <key>."
        )
    return key


_READ_FILE_TOOL = {
    "name": "read_file_lines",
    "description": (
        "Read a range of lines from a source file. "
        "Line numbers are 1-indexed. Omit end_line to read to end of file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file",
            },
            "start_line": {
                "type": "integer",
                "description": "First line (1-indexed)",
                "default": 1,
            },
            "end_line": {
                "type": "integer",
                "description": "Last line (1-indexed, inclusive)",
            },
        },
        "required": ["path"],
    },
}


def _execute_read_file(inp: dict) -> str:
    try:
        path = Path(inp["path"])
        if not path.exists():
            return f"[error: file not found: {path}]"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(
            keepends=True
        )
        start = max(0, int(inp.get("start_line", 1)) - 1)
        end_raw = inp.get("end_line")
        end = int(end_raw) if end_raw is not None else len(lines)
        chunk = lines[start:end]
        return "".join(f"{start + i + 1:4d}  {line}" for i, line in enumerate(chunk))
    except Exception as exc:
        return f"[error reading file: {exc}]"


def _parse_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Model wrote reasoning before the JSON — find the first top-level { and parse from there.
    start = text.find("{")
    if start > 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    # Try extracting from a ```json fence that may appear mid-text
    m = re.search(r"```(?:json)?\s*(\{.*?)\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"Non-JSON response (no valid JSON found): {text[:500]}")


def _call(prompt: str, model: str, key: str, max_tokens: int = 4096) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.content:
        raise RuntimeError("API returned empty content")
    return _parse_text(response.content[0].text)


def _call_with_tools(
    prompt: str,
    model: str,
    key: str,
    max_tokens: int = 8192,
    max_tool_rounds: int = 6,
) -> dict:
    """Call with read_file_lines tool — model reads source on demand, no truncation."""
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for _ in range(max_tool_rounds):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            tools=[_READ_FILE_TOOL],
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _execute_read_file(block.input),
                        }
                    )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        elif response.stop_reason in ("end_turn", "stop_sequence", None):
            text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            return _parse_text(text)

        else:
            raise RuntimeError(f"Unexpected stop_reason: {response.stop_reason}")

    raise RuntimeError(f"Tool loop exhausted after {max_tool_rounds} rounds")


# ---------------------------------------------------------------------------
# Step 1: Triage
# ---------------------------------------------------------------------------


def _triage(
    root: Path,
    model: str,
    key: str,
    verbose: bool,
    improve: bool = False,
) -> tuple[str, list[str]]:
    """Return (project_essence, ordered_key_files)."""
    if verbose:
        print("[intent] step 1: triage — identifying key files...")

    template = _load_prompt("triage")
    prompt = _render(
        template,
        project_name=root.name,
        file_listing=_file_listing(root),
        readme_excerpt=_collect_readme(root),
    )

    raw = _call(prompt, model, key, max_tokens=8192)
    essence = raw.get("project_essence", "")
    key_files = [f["path"] for f in raw.get("key_files", [])]

    if verbose and essence:
        print(f"  essence: {essence[:180].replace(chr(10), ' ')}...")
        print(
            f"  key files ({len(key_files)}): {', '.join(key_files[:5])}{'...' if len(key_files) > 5 else ''}"
        )

    # Self-improvement: if triage found no key files or essence is suspiciously
    # short, record as a failure and trigger prompt rewrite.
    if improve and (len(key_files) == 0 or len(essence) < 50):
        _improve_triage_prompt(
            prompt_text=template,
            bad_sample=raw,
            failure_signal=(
                f"triage returned {len(key_files)} key files and "
                f"{len(essence)}-char essence for project '{root.name}'"
            ),
            model=model,
            key=key,
            verbose=verbose,
        )

    return essence, key_files


def _improve_triage_prompt(
    prompt_text: str,
    bad_sample: dict,
    failure_signal: str,
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """Rewrite triage.md when output quality is low."""
    try:
        improve_template = _load_prompt("triage_improve")
        improve_prompt = _render(
            improve_template,
            prompt_text=prompt_text,
            good_samples="[]",
            bad_samples=json.dumps([bad_sample], indent=2),
            failure_signals=failure_signal,
        )
        result = _call(improve_prompt, model, key, max_tokens=8192)
        rewritten = result.get("rewritten_prompt", "")
        if rewritten and len(rewritten) > 200:
            prompt_path = _PROMPT_DIR / "triage.md"
            prompt_path.write_text(rewritten, encoding="utf-8")
            if verbose:
                scores = result.get("scores", {})
                print(f"  triage prompt rewritten (scores: {scores})")
    except Exception as exc:
        if verbose:
            print(f"  triage improve failed: {exc}")


# ---------------------------------------------------------------------------
# Step 2–4: Module understanding + invariants + violations
# ---------------------------------------------------------------------------


def _extract_git_log(path: Path) -> str:
    """Git history of intent: raw log + pattern analysis (reverts, repeated fixes, density)."""
    import subprocess
    import re as _re

    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--oneline", "-40", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(path.parent),
        )
        raw = result.stdout.strip()
    except Exception:
        return "(git not available)"

    if not raw:
        return "(no git history for this file)"

    lines = raw.splitlines()

    # --- Pattern analysis ---
    patterns: list[str] = []

    # Reverts: explicit evidence of an intent that couldn't be sustained
    revert_re = _re.compile(r"\brevert\b", _re.IGNORECASE)
    reverts = [ln for ln in lines if revert_re.search(ln)]
    if reverts:
        patterns.append(
            f"REVERT ({len(reverts)}x — intent attempted and pulled back):\n"
            + "\n".join(f"  {r}" for r in reverts)
        )

    # Repeated fix keywords — structural instability, not a one-time bug
    fix_re = _re.compile(r"\b(fix|fixes|fixed|repair|correct|bug)\b", _re.IGNORECASE)
    fix_subjects = [ln for ln in lines if fix_re.search(ln)]
    if len(fix_subjects) >= 3:
        patterns.append(
            f"REPEATED FIXES ({len(fix_subjects)}x — recurring instability):\n"
            + "\n".join(f"  {s}" for s in fix_subjects[:6])
        )

    # "ensure/guarantee/always" commits with no paired verification commit
    ensure_re = _re.compile(
        r"\b(ensure|guarantee|always|enforce|must)\b", _re.IGNORECASE
    )
    verify_re = _re.compile(
        r"\b(test|verify|assert|check|spec|proof)\b", _re.IGNORECASE
    )
    ensure_subjects = [ln for ln in lines if ensure_re.search(ln)]
    unverified = [ln for ln in ensure_subjects if not verify_re.search(ln)]
    if unverified:
        patterns.append(
            f"UNVERIFIED ASSERTIONS ({len(unverified)}x — 'ensure/always' with no test/verify commit):\n"
            + "\n".join(f"  {s}" for s in unverified[:4])
        )

    # Dense commit activity — load-bearing or poorly bounded
    density_note = ""
    if len(lines) >= 20:
        density_note = f"HIGH COMMIT DENSITY ({len(lines)}+ commits visible — this file changes frequently)"

    sections = [f"Recent commits ({len(lines)} shown):\n" + "\n".join(lines[:10])]
    if len(lines) > 10:
        sections[0] += f"\n  ... ({len(lines) - 10} more)"
    if patterns:
        sections.append(
            "=== INTENT PATTERNS (first-class signals) ===\n" + "\n\n".join(patterns)
        )
    if density_note:
        sections.append(density_note)

    return "\n\n".join(sections)


def _extract_intent_signals(source: str) -> str:
    """Module docstring + per-function docstrings (first line) + TODO/FIXME/HACK/BUG comments."""
    signals: list[str] = []

    try:
        tree = ast.parse(source)
        mod_doc = ast.get_docstring(tree)
        if mod_doc:
            signals.append(f"[MODULE DOCSTRING]\n{mod_doc[:600]}")

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                if doc:
                    first = doc.split("\n")[0].strip()[:150]
                    if first:
                        signals.append(f'[line {node.lineno}] {node.name}: "{first}"')
    except SyntaxError:
        pass

    _intent_re = re.compile(
        r"#\s*(TODO|FIXME|HACK|BUG|NOTE|WARN(?:ING)?|XXX)[:\s]+(.*)",
        re.IGNORECASE,
    )
    for i, line in enumerate(source.splitlines(), 1):
        m = _intent_re.search(line)
        if m:
            tag = m.group(1).upper()
            text = m.group(2).strip()[:120]
            signals.append(f"[line {i}] {tag}: {text}")

    return "\n".join(signals) if signals else "(none found)"


_SIGNATURE_ONLY_BYTES = 30_000  # files larger than this get sig-summary + tool calls


def _understand_module(
    path: Path,
    project_essence: str,
    model: str,
    key: str,
    verbose: bool,
) -> ModuleIntent:
    raw_bytes = path.read_bytes()
    full_source = raw_bytes.decode("utf-8", errors="replace")

    # For large files, send signature summary as primary context so the model
    # doesn't exhaust tool rounds trying to sequentially page through 100KB+.
    # Tool calls are still available for targeted reads of specific functions.
    if len(raw_bytes) > _SIGNATURE_ONLY_BYTES:
        sig = _signature_summary(full_source)
        source = sig
        trunc_note = (
            f"\n[LARGE FILE — {len(raw_bytes):,} bytes shown as signature map. "
            f'Use read_file_lines(path="{path.resolve()}", start_line=N, end_line=M) '
            f"to read any function body in full.]"
        )
        truncated = True
    else:
        source, truncated = _read_truncated(path)
        trunc_note = (
            "\n[FILE TRUNCATED — remaining signatures:]\n" + _signature_summary(source)
            if truncated
            else ""
        )

    if verbose:
        size = f"{len(raw_bytes):,} bytes{', sig-map' if len(raw_bytes) > _SIGNATURE_ONLY_BYTES else (', truncated' if truncated else '')}"
        print(f"  → {path.name} ({size})")

    # Intent signals always derived from full source, not sig map
    git_log = _extract_git_log(path)
    intent_signals = _extract_intent_signals(full_source)

    template = _load_prompt("understand")
    prompt = _render(
        template,
        project_essence=project_essence,
        filename=path.name,
        file_path=str(path.resolve()),
        source=source,
        truncation_note=trunc_note,
        git_log=git_log,
        intent_signals=intent_signals,
    )

    # Use tool-enabled call — model reads specific function bodies on demand
    raw = _call_with_tools(prompt, model, key, max_tokens=8192)

    u = raw.get("understanding", {})
    understanding = ProjectUnderstanding(
        purpose=u.get("purpose", ""),
        design_intent=u.get("design_intent", ""),
        key_abstractions=u.get("key_abstractions", ""),
        behavioral_contract=u.get("behavioral_contract", ""),
        failure_modes=u.get("failure_modes", ""),
        assumptions=u.get("assumptions", ""),
    )

    invariants = [
        Invariant(
            id=inv.get("id", f"inv_{i:03d}"),
            type=inv.get("type", "other"),
            statement=inv.get("statement", ""),
            applies_to=inv.get("applies_to", []),
            formal=inv.get("formal", ""),
            derived_from=inv.get("derived_from", ""),
            confidence=float(inv.get("confidence", 0.5)),
        )
        for i, inv in enumerate(raw.get("invariants", []))
    ]

    violations = [
        Violation(
            invariant_id=v.get("invariant_id", ""),
            file=str(path),
            line=int(v.get("line", 0)),
            evidence=v.get("evidence", ""),
            severity=v.get("severity", "medium"),
            explanation=v.get("explanation", ""),
        )
        for v in raw.get("violations", [])
    ]

    ta_raw = raw.get("truncation_audit", {})
    truncation_audit = (
        TruncationAudit(
            last_complete_unit=ta_raw.get("last_complete_unit", ""),
            cutoff_line=ta_raw.get("cutoff_line", ""),
            visible_definitions=ta_raw.get("visible_definitions", []),
            docstring_only_names=ta_raw.get("docstring_only_names", []),
        )
        if ta_raw
        else None
    )

    return ModuleIntent(
        path=str(path),
        understanding=understanding,
        invariants=invariants,
        violations=violations,
        truncation_audit=truncation_audit,
    )


# ---------------------------------------------------------------------------
# Step 5: Prompt self-improvement
# ---------------------------------------------------------------------------


def _improve_prompt(
    prompt_name: str,
    source_excerpt: str,
    output: dict,
    model: str,
    key: str,
    verbose: bool,
) -> Optional[ImprovementScore]:
    """Score the output and rewrite the prompt if it underperformed. Returns scores."""
    if verbose:
        print(f"    scoring output quality for {prompt_name}...")

    template = _load_prompt("improve")
    current_prompt = _load_prompt(prompt_name)
    prompt = _render(
        template,
        prompt_text=current_prompt,
        source_excerpt=source_excerpt[:3000],
        output=json.dumps(output, indent=2)[:3000],
    )

    try:
        raw = _call(prompt, model, key, max_tokens=8192)
    except Exception as exc:
        if verbose:
            print(f"    improvement scoring failed: {exc}")
        return None

    scores_raw = raw.get("scores", {})

    def _s(key_: str) -> float:
        v = scores_raw.get(key_, {})
        return float(v.get("score", 0) if isinstance(v, dict) else v) / 10.0

    score = ImprovementScore(
        specificity=_s("specificity"),
        groundedness=_s("groundedness"),
        calibration=_s("calibration"),
        completeness=_s("completeness"),
        actionability=_s("actionability"),
        non_obviousness=_s("non_obviousness"),
    )

    if verbose:
        print(
            f"    scores — specificity:{score.specificity:.1f} "
            f"groundedness:{score.groundedness:.1f} "
            f"calibration:{score.calibration:.1f} "
            f"completeness:{score.completeness:.1f} "
            f"overall:{score.overall:.2f}"
        )

    # Rewrite prompt if overall score < 0.8
    improved = raw.get("improved_prompt", "")
    if improved and score.overall < 0.8:
        _save_prompt(prompt_name, improved)
        if verbose:
            print(f"    ✓ prompt '{prompt_name}' rewritten (was {score.overall:.2f})")
    elif verbose:
        print(f"    prompt '{prompt_name}' good enough ({score.overall:.2f}), kept")

    return score


def _batch_improve(
    modules: list,
    sources: dict[str, str],
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """Score all modules, pick the 3 weakest, run ONE combined improvement pass."""
    if verbose:
        print("[intent] step 5: batch prompt improvement...")

    scored: list[tuple[float, object, str]] = []
    for m in modules:
        src = sources.get(m.path, "")[:2000]
        score = _improve_prompt("understand", src, asdict(m), model, key, verbose=False)
        if score is not None:
            scored.append((score.overall, m, src))

    if not scored:
        if verbose:
            print("  no scores collected — skipping batch rewrite")
        return

    # Sort by worst score first; take up to 3
    scored.sort(key=lambda t: t[0])
    worst = scored[:3]
    avg_score = sum(t[0] for t in worst) / len(worst)

    if avg_score >= 0.8:
        if verbose:
            print(f"  all modules scoring well ({avg_score:.2f}) — no rewrite needed")
        return

    if verbose:
        names = [Path(t[1].path).name for t in worst]
        print(f"  worst modules: {names} (avg {avg_score:.2f}) — rewriting prompt")

    # Build a combined improve prompt with all 3 examples
    template = _load_prompt("improve")
    current_prompt = _load_prompt("understand")

    # Use the absolute worst module for the improve call
    _, worst_module, worst_src = worst[0]
    prompt = _render(
        template,
        prompt_text=current_prompt,
        source_excerpt=worst_src,
        output=json.dumps(asdict(worst_module), indent=2)[:3000],
    )

    try:
        raw = _call(prompt, model, key, max_tokens=8192)
        improved = raw.get("improved_prompt", "")
        if improved:
            _save_prompt("understand", improved)
            if verbose:
                print(
                    f"  ✓ understand prompt rewritten (batch, worst score {worst[0][0]:.2f})"
                )
    except Exception as exc:
        if verbose:
            print(f"  batch improve failed: {exc}")


# ---------------------------------------------------------------------------
# Project-level synthesis — adversarial oracle pattern
# ---------------------------------------------------------------------------


def _module_summary(m: ModuleIntent) -> dict:
    """Compact summary of a module for project-level prompts."""
    return {
        "path": m.path,
        "purpose": m.understanding.purpose[:200],
        "invariants": [
            {"id": inv.id, "statement": inv.statement, "confidence": inv.confidence}
            for inv in m.invariants
        ],
        "violations": [
            {"invariant_id": v.invariant_id, "line": v.line, "severity": v.severity}
            for v in m.violations
        ],
    }


def _invariant_skeptic(
    proposed_invariants: list[dict],
    module_summaries: list[dict],
    model: str,
    key: str,
    verbose: bool,
    improve: bool = False,
) -> tuple[list[str], list[str]]:
    """
    Adversarial oracle: second LLM call tries to falsify each proposed invariant.
    Returns (surviving_ids, falsified_ids).

    The two LLMs don't share context — the skeptic only sees the claim and the
    evidence base, not the proposer's reasoning. This prevents collusion.
    """
    if not proposed_invariants:
        return [], []

    template = _load_prompt("invariant_skeptic")
    prompt = _render(
        template,
        proposed_invariants=json.dumps(proposed_invariants, indent=2),
        module_summaries=json.dumps(module_summaries, indent=2)[:8000],
    )

    try:
        raw = _call_with_tools(prompt, model, key, max_tokens=4096)
        surviving = raw.get("surviving_invariants", [])
        falsified = raw.get("falsified_invariants", [])
        verdicts = raw.get("verdicts", [])
        if verbose and falsified:
            print(
                f"  oracle falsified {len(falsified)} project invariants: {falsified}"
            )
        if improve and verdicts:
            _improve_invariant_skeptic_prompt(
                verdicts, proposed_invariants, model, key, verbose
            )
        return surviving, falsified
    except Exception as exc:
        if verbose:
            print(f"  invariant_skeptic failed: {exc}")
        return [inv["id"] for inv in proposed_invariants], []


def _improve_invariant_skeptic_prompt(
    verdicts: list[dict],
    proposed_invariants: list[dict],
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """Score skeptic prompt quality and rewrite if calibration is poor."""
    if len(verdicts) < 2:
        return

    n_total = len(verdicts)
    n_falsified = sum(1 for v in verdicts if v.get("verdict") == "FALSIFIED")
    n_unverifiable = sum(1 for v in verdicts if v.get("verdict") == "UNVERIFIABLE")
    n_survives = sum(1 for v in verdicts if v.get("verdict") == "SURVIVES")

    falsification_rate = n_falsified / n_total
    unverifiable_rate = n_unverifiable / n_total

    # Oracle is underperforming if: never falsifies anything OR falsifies everything
    # OR buries everything in UNVERIFIABLE
    calibration_ok = 0.05 <= falsification_rate <= 0.90
    unverifiable_ok = unverifiable_rate <= 0.50

    if calibration_ok and unverifiable_ok:
        return  # oracle is calibrated

    def _by_verdict(verdict_type: str) -> list[dict]:
        return [v for v in verdicts if v.get("verdict") == verdict_type][:3]

    performance_signals = (
        f"falsification_rate: {falsification_rate:.0%}, "
        f"unverifiable_rate: {unverifiable_rate:.0%}, "
        f"n_total: {n_total}, n_survives: {n_survives}, "
        f"n_falsified: {n_falsified}, n_unverifiable: {n_unverifiable}"
    )

    try:
        template = _load_prompt("invariant_skeptic_improve")
        prompt = _render(
            template,
            prompt_text=_load_prompt("invariant_skeptic"),
            survives_samples=json.dumps(_by_verdict("SURVIVES"), indent=2),
            falsified_samples=json.dumps(_by_verdict("FALSIFIED"), indent=2),
            unverifiable_samples=json.dumps(_by_verdict("UNVERIFIABLE"), indent=2),
            performance_signals=performance_signals,
        )
        raw = _call(prompt, model, key, max_tokens=8192)
        improved = raw.get("improved_prompt", "")
        overall = raw.get("overall_score", 0.0)
        if improved and overall < 0.8:
            _save_prompt("invariant_skeptic", improved)
            if verbose:
                print(
                    f"\n[skeptic] ✓ invariant_skeptic prompt rewritten "
                    f"(score was {overall:.2f}, falsification_rate {falsification_rate:.0%})"
                )
    except Exception as exc:
        if verbose:
            print(f"\n[skeptic] prompt improvement failed: {exc}")


def _project_intent(
    intent: "ProjectIntent", model: str, key: str, verbose: bool, improve: bool = False
) -> None:
    """
    Step 5: synthesize project-level invariants from all module analyses,
    then run adversarial oracle to falsify weak ones.

    Mutates intent.project_summary with cross-module context if it improves.
    Stores oracle-validated invariants in intent.project_invariants (added dynamically).
    """
    if verbose:
        print("[intent] step 5: project-level synthesis + adversarial oracle...")

    summaries = [_module_summary(m) for m in intent.modules]

    template = _load_prompt("project_intent")
    prompt = _render(
        template,
        project_name=intent.project,
        triage_file="(inline — see module_summaries)",
        module_summaries=json.dumps(summaries, indent=2)[:10000],
    )

    try:
        raw = _call_with_tools(prompt, model, key, max_tokens=6144)
    except Exception as exc:
        if verbose:
            print(f"  project_intent synthesis failed: {exc}")
        return

    proposed = raw.get("project_invariants", [])
    clusters = raw.get("violation_clusters", [])
    priority = raw.get("analysis_priority", {})

    if verbose:
        print(
            f"  proposed {len(proposed)} project invariants, {len(clusters)} clusters"
        )

    # Adversarial oracle: second independent call tries to falsify proposed invariants
    surviving_ids, falsified_ids = _invariant_skeptic(
        proposed, summaries, model, key, verbose, improve=improve
    )

    oracle_validated = [inv for inv in proposed if inv.get("id") in surviving_ids]

    # Store on intent object (extend schema dynamically — no dataclass change needed)
    intent.__dict__["project_invariants"] = oracle_validated
    intent.__dict__["violation_clusters"] = clusters
    intent.__dict__["analysis_priority"] = priority
    intent.__dict__["oracle_falsified"] = falsified_ids

    if verbose:
        print(
            f"  oracle validated {len(oracle_validated)}/{len(proposed)} project invariants"
        )
        if priority.get("highest_risk_violation"):
            hrv = priority["highest_risk_violation"]
            print(
                f"  highest-risk violation: {hrv.get('location')} — {hrv.get('reason','')[:80]}"
            )

    if improve and proposed:
        oracle_results = (
            f"{len(oracle_validated)} of {len(proposed)} invariants survived oracle. "
            f"falsified: {falsified_ids[:5]}. "
            f"surviving: {[inv.get('id') for inv in oracle_validated][:5]}"
        )
        failure_signals: list[str] = []
        if len(oracle_validated) == 0:
            failure_signals.append(
                f"zero_survivors: 0 of {len(proposed)} invariants survived"
            )
        for inv in proposed:
            stmt = inv.get("statement", "")
            formal = inv.get("formal", "")
            modules = inv.get("applies_to_modules", [])
            if len(modules) < 2:
                failure_signals.append(
                    f"cross_module_missing: invariant '{inv.get('id')}' has only {len(modules)} module"
                )
            if "∀" not in formal and "∃" not in formal and "if" not in formal.lower():
                failure_signals.append(
                    f"no_formal_statement: invariant '{inv.get('id')}' lacks ∀/∃/if-then formal"
                )
            for generic in ("all", "every", "should", "must handle"):
                if generic in stmt.lower() and len(stmt) < 80:
                    failure_signals.append(
                        f"generic_invariant: '{stmt[:60]}' may be too generic"
                    )
                    break
        single_clusters = [c for c in clusters if len(c.get("violations", [])) < 2]
        for c in single_clusters:
            failure_signals.append(
                f"single_violation_cluster: cluster '{c.get('name')}' has only 1 violation"
            )
        if failure_signals:
            _improve_project_intent_prompt(
                proposed, clusters, oracle_results, failure_signals, model, key, verbose
            )


# ---------------------------------------------------------------------------
# Dead code audit
# ---------------------------------------------------------------------------


def dead_code_audit(
    root: Path,
    intent_path: Path,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    verbose: bool = False,
    improve: bool = False,
) -> dict:
    """
    Identify structurally superseded code given the current architecture.
    Returns dict with dead_code_candidates, removal_patches, high_risk_flags.
    """
    key = _get_key(api_key)

    try:
        raw_intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"dead_code_audit: intent file is not valid JSON: {intent_path} — {exc}"
        ) from exc
    summaries = raw_intent.get("modules", [])
    essence = raw_intent.get("project_summary", "")

    # Build a description of known architecture transitions
    transitions = raw_intent.get("violation_clusters", [])
    transition_text = (
        json.dumps(transitions, indent=2)[:3000]
        if transitions
        else ("No explicit transitions recorded — infer from module analyses.")
    )

    template = _load_prompt("dead_code")
    prompt = _render(
        template,
        project_essence=essence[:1000],
        architecture_transitions=transition_text,
        module_summaries=json.dumps(summaries, indent=2)[:8000],
    )

    try:
        result = _call_with_tools(prompt, model, key, max_tokens=6144)
        candidates = result.get("dead_code_candidates", [])
        patches = result.get("removal_patches", [])
        flags = result.get("high_risk_flags", [])
        if verbose:
            print(
                f"[dead_code] {len(candidates)} candidates, "
                f"{len(patches)} removal patches, "
                f"{len(flags)} high-risk flags"
            )
        if improve:
            _improve_dead_code_prompt(
                candidates, patches, summaries, model, key, verbose
            )
        return result
    except Exception as exc:
        if verbose:
            print(f"[dead_code] failed: {exc}")
        return {}


def _improve_dead_code_prompt(
    candidates: list[dict],
    patches: list[dict],
    module_summaries: list[dict],
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """Score dead_code prompt quality and rewrite if output is empty or risky."""
    # Underperforming: zero candidates despite having modules, or false positives
    false_positives = [
        c for c in candidates if c.get("risk") == "LOW" and c.get("remaining_callers")
    ]
    no_replacement = [c for c in candidates if not c.get("replacement")]

    failure_signals_parts = []
    if len(candidates) == 0 and len(module_summaries) > 3:
        failure_signals_parts.append(
            f"empty_result: 0 candidates from {len(module_summaries)} modules"
        )
    for c in false_positives:
        callers = c.get("remaining_callers", [])
        failure_signals_parts.append(
            f"risk_mislabel: {c.get('name')} marked LOW but has remaining_callers: {callers[:2]}"
        )
    for c in no_replacement:
        failure_signals_parts.append(
            f"no_replacement_named: {c.get('name')} lacks a specific replacement"
        )

    if not failure_signals_parts:
        return  # output looks clean

    try:
        template = _load_prompt("dead_code_improve")
        prompt = _render(
            template,
            prompt_text=_load_prompt("dead_code"),
            candidates_sample=json.dumps(candidates[:4], indent=2),
            patches_sample=json.dumps(patches[:3], indent=2),
            failure_signals="\n".join(failure_signals_parts),
        )
        raw = _call(prompt, model, key, max_tokens=8192)
        improved = raw.get("improved_prompt", "")
        overall = raw.get("overall_score", 0.0)
        if improved and overall < 0.8:
            _save_prompt("dead_code", improved)
            if verbose:
                print(
                    f"\n[dead_code] ✓ dead_code prompt rewritten "
                    f"(score was {overall:.2f}, {len(failure_signals_parts)} failure signals)"
                )
    except Exception as exc:
        if verbose:
            print(f"\n[dead_code] prompt improvement failed: {exc}")


def _improve_project_intent_prompt(
    proposed: list[dict],
    clusters: list[dict],
    oracle_results: str,
    failure_signals: list[str],
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """Score project_intent prompt quality and rewrite if oracle survival rate is poor."""
    try:
        template = _load_prompt("project_intent_improve")
        prompt = _render(
            template,
            prompt_text=_load_prompt("project_intent"),
            invariants_sample=json.dumps(proposed[:4], indent=2),
            clusters_sample=json.dumps(clusters[:3], indent=2),
            oracle_results=oracle_results,
            failure_signals="\n".join(failure_signals[:10]),
        )
        raw = _call(prompt, model, key, max_tokens=8192)
        improved = raw.get("improved_prompt", "")
        overall = raw.get("overall_score", 0.0)
        if improved and overall < 0.8:
            _save_prompt("project_intent", improved)
            if verbose:
                print(
                    f"\n[project_intent] ✓ project_intent prompt rewritten "
                    f"(score was {overall:.2f}, {len(failure_signals)} failure signals)"
                )
    except Exception as exc:
        if verbose:
            print(f"\n[project_intent] prompt improvement failed: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_file_intent(
    path: Path,
    project_essence: str = "",
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    improve: bool = False,
    verbose: bool = False,
) -> ModuleIntent:
    """Extract world model + invariants + violations from one Python file."""
    key = _get_key(api_key)
    source, _ = _read_truncated(path)

    module = _understand_module(path, project_essence, model, key, verbose)

    if improve:
        score = _improve_prompt(
            "understand",
            source,
            asdict(module),
            model,
            key,
            verbose,
        )
        module.prompt_score = score

    return module


def _verify_intent_gaps(
    intent: "ProjectIntent",
    module_sources: dict[str, str],
    model: str,
    key: str,
    verbose: bool,
) -> None:
    """
    Z3-verify intent_gap invariants: prune false positives, promote confirmed gaps.

    For each high-confidence intent_gap invariant, runs contract_encoder.verify_contract()
    against the first function listed in applies_to:
      UNSAT → claim IS enforced; mark as z3_refuted (false positive), lower confidence.
      SAT   → confirmed gap with concrete counterexample; promote to high severity.
      other → leave unchanged (Z3 couldn't encode the claim).
    """
    try:
        from pact.contract_encoder import verify_contract
    except ImportError:
        return

    for module in intent.modules:
        source = module_sources.get(module.path, "")
        if not source:
            continue

        for inv in module.invariants:
            if inv.type != "intent_gap" or inv.confidence < 0.85:
                continue
            if not inv.applies_to:
                continue

            func_name = inv.applies_to[0]
            if verbose:
                print(
                    f"  [Z3] verifying intent_gap: {func_name} — {inv.statement[:60]}…"
                )

            try:
                result = verify_contract(
                    contract=inv.statement,
                    function_source=source,
                    function_name=func_name,
                    api_key=key,
                    model=model,
                )
            except Exception:
                continue

            if result.status == "unsat":
                # Claim IS formally enforced — LLM was wrong, this is a false positive
                inv.confidence = max(0.0, inv.confidence - 0.4)
                inv.derived_from = (
                    f"z3_refuted: {inv.derived_from} "
                    f"[Z3 proved contract holds — UNSAT, no counterexample]"
                )
                # Remove associated violations for this invariant
                module.violations = [
                    v for v in module.violations if v.invariant_id != inv.id
                ]
                if verbose:
                    print(
                        f"    → UNSAT: false positive pruned (confidence now {inv.confidence:.2f})"
                    )

            elif result.status == "sat" and result.counterexample:
                # Confirmed gap — Z3 found a concrete violating input
                ce = str(result.counterexample)
                inv.derived_from = (
                    f"z3_confirmed: {inv.derived_from} "
                    f"[Z3 counterexample: {ce[:120]}]"
                )
                # Upgrade any associated violations to high severity
                for v in module.violations:
                    if v.invariant_id == inv.id:
                        v.severity = "high"
                        v.explanation = (
                            f"{v.explanation}\nZ3 counterexample: {ce[:200]}"
                            + (
                                f"\nCEGIS: {result.cegis_reasoning}"
                                if result.cegis_reasoning
                                else ""
                            )
                        )
                if verbose:
                    print(f"    → SAT: confirmed gap, counterexample: {ce[:80]}")


def extract_project_intent(
    root: Path,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    output: Optional[Path] = None,
    improve: bool = False,
    verbose: bool = False,
    max_files: int = 15,
) -> ProjectIntent:
    """
    Full pipeline: triage → understand each key module → (optionally) improve prompts.
    """
    import datetime

    key = _get_key(api_key)

    intent = ProjectIntent(
        project=root.name,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        source_model=model,
    )

    # Step 1: triage
    try:
        essence, key_files = _triage(root, model, key, verbose)
        intent.project_summary = essence
        intent.key_files = key_files
    except Exception as exc:
        import warnings

        warnings.warn(
            f"extract_project_intent triage failed; all modules will be analyzed without "
            f"project-level context, reducing invariant/violation quality: {exc}",
            UserWarning,
            stacklevel=2,
        )
        if verbose:
            print(f"  triage failed: {exc}")
        essence = ""
        key_files = []

    # Resolve key files to paths; fall back to top-N by size
    if key_files:
        resolved = []
        for rel in key_files:
            p = root / rel
            if p.exists():
                resolved.append(p)
        if not resolved:
            resolved = sorted(
                [f for f in _iter_python_files(root) if f.stat().st_size > 500],
                key=lambda f: f.stat().st_size,
                reverse=True,
            )[:max_files]
        files = resolved[:max_files]
    else:
        files = sorted(
            [f for f in _iter_python_files(root) if f.stat().st_size > 500],
            key=lambda f: f.stat().st_size,
            reverse=True,
        )[:max_files]

    # Step 2–4: understand each module (always without per-module improvement here)
    if verbose:
        print(f"[intent] step 2-4: understanding {len(files)} modules...")

    module_sources: dict[str, str] = {}
    for f in files:
        try:
            src, _ = _read_truncated(f)
            module_sources[str(f)] = src
            module = extract_file_intent(
                f,
                project_essence=essence,
                model=model,
                api_key=key,
                improve=False,
                verbose=verbose,
            )
            intent.modules.append(module)
        except Exception as exc:
            import warnings

            warnings.warn(
                f"extract_project_intent: skipped {f.name}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            if verbose:
                print(f"  skipped {f.name}: {exc}")

    # Step 5: project-level synthesis (oracle-validated invariants)
    if intent.modules:
        try:
            _project_intent(intent, model, key, verbose, improve=improve)
        except Exception as exc:
            if verbose:
                print(f"  project_intent failed: {exc}")

    # Step 5b: Z3 verification of intent_gap invariants — prune false positives,
    # promote confirmed gaps with concrete counterexamples.
    _verify_intent_gaps(intent, module_sources, model, key, verbose)

    # Step 6: one batch improve pass using the 3 hardest modules
    if improve and intent.modules:
        _batch_improve(intent.modules, module_sources, model, key, verbose)

    if output:
        out_path = output if not output.is_dir() else output / "intent.json"
        out_path.write_text(intent.to_json(), encoding="utf-8")
        if verbose:
            n_inv = sum(len(m.invariants) for m in intent.modules)
            n_viol = sum(len(m.violations) for m in intent.modules)
            avg = intent.avg_prompt_score()
            print(f"\n[intent] wrote {out_path}")
            print(
                f"  modules:{len(intent.modules)}  invariants:{n_inv}  violations:{n_viol}"
            )
            if avg is not None:
                print(f"  avg prompt score: {avg:.2f}")

    return intent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    import argparse

    top = argparse.ArgumentParser(prog="pact intent")
    sub = top.add_subparsers(dest="cmd")

    # --- pact intent analyze ---
    analyze = sub.add_parser(
        "analyze",
        help="Build semantic world model + oracle-validated project invariants",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze.add_argument("path", help="Python file or project directory")
    analyze.add_argument("--out", metavar="PATH", help="write intent.json here")
    analyze.add_argument("--model", default=_DEFAULT_MODEL)
    analyze.add_argument("--max-files", type=int, default=15, metavar="N")
    analyze.add_argument(
        "--improve",
        action="store_true",
        help="rewrite underperforming prompts after run",
    )
    analyze.add_argument("--api-key", metavar="KEY")
    analyze.add_argument("-v", "--verbose", action="store_true")

    # --- pact intent dead-code ---
    dc = sub.add_parser(
        "dead-code",
        help="Identify structurally superseded code given current architecture",
    )
    dc.add_argument("path", help="Project directory")
    dc.add_argument(
        "--intent",
        metavar="PATH",
        required=True,
        help="intent.json from a prior pact intent analyze run",
    )
    dc.add_argument("--out", metavar="PATH", help="write dead_code.json here")
    dc.add_argument("--model", default=_DEFAULT_MODEL)
    dc.add_argument("--api-key", metavar="KEY")
    dc.add_argument("-v", "--verbose", action="store_true")

    # Backward compat: if no subcommand given, treat positional as analyze
    args, remaining = top.parse_known_args(argv)
    if args.cmd is None:
        argv2 = ["analyze"] + (argv or [])
        args = top.parse_args(argv2)

    if args.cmd == "dead-code":
        result = dead_code_audit(
            root=Path(args.path).expanduser().resolve(),
            intent_path=Path(args.intent).expanduser().resolve(),
            model=args.model,
            api_key=args.api_key,
            verbose=args.verbose,
        )
        out_text = json.dumps(result, indent=2)
        if args.out:
            Path(args.out).write_text(out_text, encoding="utf-8")
        else:
            print(out_text)
        return

    # analyze subcommand (or backward compat)
    target = Path(args.path).expanduser().resolve()
    out = Path(args.out).expanduser().resolve() if args.out else None

    if target.is_file():
        result = extract_file_intent(
            target,
            model=args.model,
            api_key=args.api_key,
            improve=args.improve,
            verbose=True,
        )
        print(json.dumps(asdict(result), indent=2))
    else:
        result = extract_project_intent(
            target,
            model=args.model,
            api_key=args.api_key,
            output=out,
            improve=args.improve,
            verbose=True,
            max_files=args.max_files,
        )
        if not out:
            print(result.to_json())


if __name__ == "__main__":
    main()
