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
import textwrap
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
class ModuleIntent:
    path: str
    understanding: ProjectUnderstanding
    invariants: list[Invariant] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    prompt_score: Optional[ImprovementScore] = None


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
    text = response.content[0].text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response: {text[:300]}") from exc


# ---------------------------------------------------------------------------
# Step 1: Triage
# ---------------------------------------------------------------------------


def _triage(root: Path, model: str, key: str, verbose: bool) -> tuple[str, list[str]]:
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

    raw = _call(prompt, model, key, max_tokens=2048)
    essence = raw.get("project_essence", "")
    key_files = [f["path"] for f in raw.get("key_files", [])]

    if verbose and essence:
        print(f"  essence: {essence[:180].replace(chr(10), ' ')}...")
        print(
            f"  key files ({len(key_files)}): {', '.join(key_files[:5])}{'...' if len(key_files) > 5 else ''}"
        )

    return essence, key_files


# ---------------------------------------------------------------------------
# Step 2–4: Module understanding + invariants + violations
# ---------------------------------------------------------------------------


def _understand_module(
    path: Path,
    project_essence: str,
    model: str,
    key: str,
    verbose: bool,
) -> ModuleIntent:
    source, truncated = _read_truncated(path)
    trunc_note = (
        "\n[FILE TRUNCATED — remaining signatures:]\n" + _signature_summary(source)
        if truncated
        else ""
    )

    if verbose:
        size = f"{len(source):,} bytes{', truncated' if truncated else ''}"
        print(f"  → {path.name} ({size})")

    template = _load_prompt("understand")
    prompt = _render(
        template,
        project_essence=project_essence,
        filename=path.name,
        source=source,
        truncation_note=trunc_note,
    )

    raw = _call(prompt, model, key)

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

    return ModuleIntent(
        path=str(path),
        understanding=understanding,
        invariants=invariants,
        violations=violations,
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
        raw = _call(prompt, model, key, max_tokens=4096)
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

    # Step 2–4: understand each module
    if verbose:
        print(f"[intent] step 2-4: understanding {len(files)} modules...")

    for f in files:
        try:
            module = extract_file_intent(
                f,
                project_essence=essence,
                model=model,
                api_key=key,
                improve=improve,
                verbose=verbose,
            )
            intent.modules.append(module)
        except Exception as exc:
            if verbose:
                print(f"  skipped {f.name}: {exc}")

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

    p = argparse.ArgumentParser(
        prog="pact intent",
        description="Build a semantic world model of a Python project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Prompts live in prompts/*.md and self-improve with --improve.
            Each run scores output quality and rewrites underperforming prompts.
        """),
    )
    p.add_argument("path", help="Python file or project directory")
    p.add_argument("--out", metavar="PATH", help="write intent.json here")
    p.add_argument(
        "--model",
        default=_DEFAULT_MODEL,
        help=f"Claude model (default: {_DEFAULT_MODEL})",
    )
    p.add_argument("--max-files", type=int, default=15, metavar="N")
    p.add_argument(
        "--improve",
        action="store_true",
        help="score output and rewrite underperforming prompts after each module",
    )
    p.add_argument("--api-key", metavar="KEY")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

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
