"""
pact intent -- LLM-powered semantic world-model extraction.

Stage 1 of the intent-first verification pipeline:

  1. UNDERSTAND the project holistically — what it is, why it exists,
     what problems it solves, what design decisions were made and why,
     what the key abstractions are and how they relate.

  2. EXTRACT invariants that must hold for the project to work correctly —
     derived from the project's own stated and implied intent, not from
     external coding conventions.

  3. IDENTIFY violations — places where the code appears to contradict
     its own intent.

The output (intent.json) is the "world model" that feeds pact's verify
pipeline. Nothing downstream should run until this exists.

Usage:
    # Analyse a single file
    pact intent <file.py>

    # Analyse a whole project (top N files by substance)
    pact intent <directory> --out intent.json

    # Use a specific model
    pact intent <directory> --model claude-opus-4-7 --out intent.json
"""

from __future__ import annotations

import ast
import json
import os
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Schema — world model first, invariants second
# ---------------------------------------------------------------------------


@dataclass
class ProjectUnderstanding:
    """
    Rich natural-language understanding of what a project IS and WHY it exists.
    This is the world model. Everything else is derived from this.
    """

    purpose: str  # What problem does this project/module solve?
    design_intent: str  # What key design decisions were made, and why?
    key_abstractions: str  # What are the main concepts/types and how do they relate?
    behavioral_contract: str  # How is it supposed to behave? What must always happen?
    failure_modes: str  # What can go wrong, and how is it supposed to handle that?
    assumptions: str  # What does this code assume about its inputs/environment?


@dataclass
class Invariant:
    id: str
    type: str  # nullable_contract | async_contract | error_contract |
    # guard_requirement | data_flow | uniqueness | other
    statement: str  # plain English — what must always be true
    applies_to: list[str]  # function/class names this applies to
    formal: str  # semi-formal (∀, →, ≠ None, always/never notation)
    derived_from: str  # which part of the project understanding implies this
    confidence: float  # 0.0–1.0


@dataclass
class Violation:
    invariant_id: str
    file: str
    line: int
    evidence: str  # specific code that contradicts the invariant
    severity: str  # critical | high | medium | low
    explanation: str  # why this matters given the project's intent


@dataclass
class ModuleIntent:
    path: str
    understanding: ProjectUnderstanding
    invariants: list[Invariant] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)


@dataclass
class ProjectIntent:
    project: str
    generated_at: str
    source_model: str
    # Project-level understanding (from README/entry points/key modules)
    project_summary: str = ""
    modules: list[ModuleIntent] = field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent)

    def violations_by_severity(self) -> dict[str, list[dict]]:
        """All violations across modules, grouped by severity."""
        result: dict[str, list] = {"critical": [], "high": [], "medium": [], "low": []}
        for m in self.modules:
            for v in m.violations:
                result.setdefault(v.severity, []).append(
                    {
                        "file": v.file,
                        "line": v.line,
                        "invariant": v.invariant_id,
                        "evidence": v.evidence,
                        "explanation": v.explanation,
                    }
                )
        return result


# ---------------------------------------------------------------------------
# File selection
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
_MAX_FILE_BYTES = 40_000  # ~500 lines; truncate larger files with signature summary


def _iter_python_files(root: Path) -> list[Path]:
    """Return .py files under root, skipping caches/test files/vendored dirs."""
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
    """Compact signature+docstring summary for oversized files."""
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


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM = textwrap.dedent("""\
    You are performing semantic intent extraction for a formal verification pipeline.

    Your PRIMARY task is to deeply understand what the code IS and WHY it exists —
    its purpose, design intent, key abstractions, behavioral contracts, and
    assumptions. This understanding is the "world model" that everything else
    derives from.

    Only AFTER establishing that understanding should you extract invariants
    and violations — and those must be derived from the code's OWN intent,
    not from external coding conventions.

    Return ONLY valid JSON. No markdown fences, no explanation outside the JSON.
""")

_MODULE_SCHEMA = textwrap.dedent("""\
    {
      "understanding": {
        "purpose": "Multi-paragraph description of what problem this module solves and why it exists",
        "design_intent": "What key design decisions were made (e.g. why AST not regex, why LRU cache, why dataclass not dict) and the reasoning behind them",
        "key_abstractions": "The main types/concepts (e.g. FailureEvidence, FailureMode, FIX_MODES) and how they relate to each other",
        "behavioral_contract": "How this module is supposed to behave — what it must always do, what it must never do, what callers can rely on",
        "failure_modes": "What can go wrong, how the code is supposed to handle it, and what failure modes are intentionally unhandled",
        "assumptions": "What this code assumes about its inputs, environment, and callers"
      },
      "invariants": [
        {
          "id": "inv_001",
          "type": "nullable_contract | async_contract | error_contract | guard_requirement | data_flow | uniqueness | other",
          "statement": "Plain English: what must always be true",
          "applies_to": ["function_name", "ClassName.method"],
          "formal": "Semi-formal: ∀ calls to f. result ≠ None  or  always: x is checked before y",
          "derived_from": "Which part of the understanding implies this invariant",
          "confidence": 0.9
        }
      ],
      "violations": [
        {
          "invariant_id": "inv_001",
          "file": "module_name.py",
          "line": 42,
          "evidence": "Specific code that contradicts the invariant",
          "severity": "critical | high | medium | low",
          "explanation": "Why this violation matters given the module's intent"
        }
      ]
    }
""")

_PROJECT_SCHEMA = textwrap.dedent("""\
    {
      "project_summary": "Multi-paragraph description of the whole project — what it is, why it exists, what problems it solves, who uses it and how, what the key architectural decisions are",
      "key_files": ["list of the most important files for understanding this project"]
    }
""")


def _build_module_prompt(path: Path, source: str, truncated: bool) -> str:
    suffix = ""
    if truncated:
        suffix = (
            "\n\n[FILE TRUNCATED — signature summary follows:]\n"
            + _signature_summary(source)
        )
    return (
        f"## Module: {path.name}\n\n"
        "```python\n" + source + suffix + "\n```\n\n"
        "Extract the semantic intent. Return JSON matching this schema exactly:\n\n"
        + _MODULE_SCHEMA
    )


def _build_project_prompt(readme: str, entry_points: str) -> str:
    parts = []
    if readme:
        parts.append(f"## README\n\n{readme[:6000]}")
    if entry_points:
        parts.append(f"## Entry point signatures\n\n```python\n{entry_points}\n```")
    parts.append(
        "\nExtract the project-level understanding. Return JSON matching:\n\n"
        + _PROJECT_SCHEMA
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-sonnet-4-6"


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
            "  Or pass --api-key <key> on the command line."
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
    # Strip markdown fences if model wraps output
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if "```" in text:
            text = text[: text.rfind("```")]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Non-JSON response (first 300 chars): {text[:300]}"
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_file_intent(
    path: Path,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    verbose: bool = False,
) -> ModuleIntent:
    """Extract semantic intent from a single Python file."""
    key = _get_key(api_key)
    source, truncated = _read_truncated(path)
    if verbose:
        size_note = f"{len(source)} bytes{', truncated' if truncated else ''}"
        print(f"  → {path.name} ({size_note})")

    raw = _call(_build_module_prompt(path, source, truncated), model, key)

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


def _collect_project_context(root: Path) -> tuple[str, str]:
    """Return (readme_text, entry_point_signatures)."""
    readme = ""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = root / name
        if p.exists():
            readme = p.read_text(encoding="utf-8", errors="replace")[:8000]
            break

    # Pull signatures from likely entry points
    entry_sigs = []
    for name in ("__main__.py", "cli.py", "main.py", "__init__.py"):
        p = root / name
        if p.exists():
            src = p.read_text(encoding="utf-8", errors="replace")
            entry_sigs.append(f"# {name}\n" + _signature_summary(src))
    return readme, "\n\n".join(entry_sigs[:3])


def extract_project_intent(
    root: Path,
    model: str = _DEFAULT_MODEL,
    api_key: Optional[str] = None,
    output: Optional[Path] = None,
    verbose: bool = False,
    max_files: int = 15,
) -> ProjectIntent:
    """
    Extract intent from a whole project directory.

    Step 1: build project-level summary from README + entry points.
    Step 2: extract per-module intent from the top N files by size.
    """
    import datetime

    key = _get_key(api_key)
    intent = ProjectIntent(
        project=root.name,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        source_model=model,
    )

    # Step 1 — project summary
    if verbose:
        print(f"[pact intent] extracting project summary for '{root.name}'...")
    readme, entry_sigs = _collect_project_context(root)
    if readme or entry_sigs:
        try:
            raw = _call(
                _build_project_prompt(readme, entry_sigs), model, key, max_tokens=2048
            )
            intent.project_summary = raw.get("project_summary", "")
            if verbose and intent.project_summary:
                preview = intent.project_summary[:200].replace("\n", " ")
                print(f"  project: {preview}...")
        except Exception as exc:
            if verbose:
                print(f"  project summary failed: {exc}")

    # Step 2 — per-module intent
    files = _iter_python_files(root)
    # Prefer larger files (more substance); skip trivial ones < 500 bytes
    files = [f for f in files if f.stat().st_size > 500]
    files = sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:max_files]

    if verbose:
        print(f"[pact intent] analysing {len(files)} modules...")

    for f in files:
        try:
            module = extract_file_intent(f, model=model, api_key=key, verbose=verbose)
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
            print(f"\n[pact intent] wrote {out_path}")
            print(
                f"  modules: {len(intent.modules)}, invariants: {n_inv}, violations: {n_viol}"
            )

    return intent


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="pact intent",
        description=(
            "Extract semantic intent, invariants, and violations from Python source.\n"
            "Builds a world model of the project before running any verification."
        ),
    )
    p.add_argument("path", help="Python file or project directory")
    p.add_argument("--out", metavar="PATH", help="write intent.json here")
    p.add_argument(
        "--model",
        default=_DEFAULT_MODEL,
        help=f"Claude model (default: {_DEFAULT_MODEL})",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=15,
        metavar="N",
        help="max modules to analyse when given a directory (default: 15)",
    )
    p.add_argument("--api-key", metavar="KEY", help="Anthropic API key")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    target = Path(args.path).expanduser().resolve()
    output = Path(args.out).expanduser().resolve() if args.out else None

    if target.is_file():
        result = extract_file_intent(
            target, model=args.model, api_key=args.api_key, verbose=True
        )
        print(json.dumps(asdict(result), indent=2))
    else:
        result = extract_project_intent(
            target,
            model=args.model,
            api_key=args.api_key,
            output=output,
            verbose=True,
            max_files=args.max_files,
        )
        if not output:
            print(result.to_json())


if __name__ == "__main__":
    main()
