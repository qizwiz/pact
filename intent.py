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
import os as _os
import re
import sys as _sys
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .enrich import IntentContext as _IntentContext
from .enrich import gather as _enrich_gather
from .enrich import render_file_context as _enrich_file_ctx
from .enrich import render_project_context as _enrich_project_ctx
from .llm import resolve_model as _resolve_model

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
    resource_obligations: str = (
        ""  # cross-call temporal contracts: processes, handles, ordering, accumulation
    )


@dataclass
class Invariant:
    id: str
    type: str
    statement: str
    applies_to: list[str]
    formal: str
    derived_from: str
    confidence: float
    # Contract IR — populated by Z3 oracle during intent analysis.
    # Consumed by the pipeline to skip LLM re-encoding at verification time.
    contract_kind: str = (
        ""  # "ordering"|"resource_lifecycle"|"accumulation"|"flag_invariant"|"subset_relation"|"behavioral"
    )
    z3_encoding: str = ""  # z3_script that confirmed/refuted this invariant
    tla_template: str = ""  # TLA+ template name matching this kind


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


def _module_intent_from_dict(d: dict) -> "ModuleIntent":
    u = d.get("understanding", {})
    understanding = ProjectUnderstanding(
        purpose=u.get("purpose", ""),
        design_intent=u.get("design_intent", ""),
        key_abstractions=u.get("key_abstractions", ""),
        behavioral_contract=u.get("behavioral_contract", ""),
        failure_modes=u.get("failure_modes", ""),
        assumptions=u.get("assumptions", ""),
        resource_obligations=u.get("resource_obligations", ""),
    )
    invariants = [
        Invariant(
            id=i.get("id", ""),
            type=i.get("type", ""),
            statement=i.get("statement", ""),
            applies_to=i.get("applies_to", []),
            formal=i.get("formal", ""),
            derived_from=i.get("derived_from", ""),
            confidence=float(i.get("confidence", 0.0)),
            contract_kind=i.get("contract_kind", ""),
            z3_encoding=i.get("z3_encoding", ""),
            tla_template=i.get("tla_template", ""),
        )
        for i in d.get("invariants", [])
    ]
    violations = [
        Violation(
            invariant_id=v.get("invariant_id", ""),
            file=v.get("file", ""),
            line=int(v.get("line", 0)),
            evidence=v.get("evidence", ""),
            severity=v.get("severity", "low"),
            explanation=v.get("explanation", ""),
        )
        for v in d.get("violations", [])
    ]
    return ModuleIntent(
        path=d.get("path", ""),
        understanding=understanding,
        invariants=invariants,
        violations=violations,
    )


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

    def to_markdown(self) -> str:
        """
        Render the full intent analysis as a markdown document for LLM consumption.
        This is a first-class artifact, not a pretty-print of the JSON.
        It tells a story: what was intended, what was found, what needs attention.
        """
        lines: list[str] = []
        ts = self.generated_at[:10] if self.generated_at else "unknown"
        lines += [
            f"# Structural Intent Analysis: `{Path(self.project).name}`",
            "",
            f"**Generated:** {ts}  **Model:** {self.source_model}  "
            f"**Modules:** {len(self.modules)}",
            "",
        ]

        if self.project_summary:
            lines += ["## Project Summary", "", self.project_summary, ""]

        # Violation summary by severity
        by_sev = self.violations_by_severity()
        total = sum(len(v) for v in by_sev.values())
        if total:
            crit = len(by_sev.get("critical", []))
            high = len(by_sev.get("high", []))
            med = len(by_sev.get("medium", []))
            low = len(by_sev.get("low", []))
            sev_parts = []
            if crit:
                sev_parts.append(f"🔴 {crit} critical")
            if high:
                sev_parts.append(f"🟠 {high} high")
            if med:
                sev_parts.append(f"🟡 {med} medium")
            if low:
                sev_parts.append(f"⚪ {low} low")
            lines += [
                "## Violations",
                "",
                f"**{total} total** — " + ", ".join(sev_parts),
                "",
            ]

        # Per-module sections — only modules with violations
        for m in self.modules:
            if not m.violations:
                continue
            mod_name = Path(m.path).name if m.path else "unknown"
            lines += [f"### `{mod_name}`", ""]

            # Module understanding
            u = m.understanding
            if hasattr(u, "purpose") and u.purpose:
                lines += [f"**Purpose:** {u.purpose}", ""]

            # Build invariant lookup
            inv_map = {i.id: i for i in m.invariants}

            for v in sorted(
                m.violations,
                key=lambda x: (
                    {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x.severity, 9)
                ),
            ):
                inv = inv_map.get(v.invariant_id)
                sev_icon = {
                    "critical": "🔴",
                    "high": "🟠",
                    "medium": "🟡",
                    "low": "⚪",
                }.get(v.severity, "•")
                file_ref = f"`{Path(v.file).name}:{v.line}`" if v.file else ""
                lines += [f"#### {sev_icon} {file_ref}"]

                if inv:
                    lines += [f"> {inv.statement}", ""]

                if v.explanation:
                    lines += [v.explanation, ""]

                if v.evidence and v.evidence.strip():
                    snippet = v.evidence.strip()[:400]
                    lines += ["```python", snippet, "```", ""]

        return "\n".join(lines)

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
        # Use relative parts so that analysis of packages inside .venv isn't blocked
        # by the .venv directory appearing in the absolute path.
        rel_parts = p.relative_to(root).parts
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in rel_parts):
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

_DEFAULT_MODEL = _resolve_model()
_SYSTEM = (
    "You are performing semantic analysis for a formal verification pipeline. "
    "Return ONLY valid JSON. No markdown fences, no explanation outside the JSON."
)


def _get_key(api_key: Optional[str] = None) -> str:
    from .llm import resolve_key

    return resolve_key(api_key)


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
    if start >= 0:
        candidate = text[start:]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # Trailing text after closing brace — scan for balanced end
        depth = 0
        end = -1
        for i, ch in enumerate(candidate):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            try:
                return json.loads(candidate[:end])
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


_DEBUG = _os.environ.get("PACT_DEBUG", "").strip() not in ("", "0")


def _dbg(msg: str) -> None:
    if _DEBUG:
        _sys.stderr.write(f"[pact:debug] {msg}\n")
        _sys.stderr.flush()


def _call(prompt: str, model: str, key: str, max_tokens: int = 4096) -> dict:
    from .llm import make_client

    client = make_client(key)
    _dbg(f"_call prompt={len(prompt):,}ch max_tokens={max_tokens}")
    t0 = _time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = _time.monotonic() - t0
    usage = getattr(response, "usage", None)
    in_tok = getattr(usage, "input_tokens", "?")
    out_tok = getattr(usage, "output_tokens", "?")
    _dbg(
        f"_call done {elapsed:.1f}s stop={response.stop_reason} "
        f"in={in_tok} out={out_tok}"
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
    from .llm import make_client

    client = make_client(key)
    messages: list[dict] = [{"role": "user", "content": prompt}]
    _dbg(f"_call_with_tools prompt={len(prompt):,}ch max_tokens={max_tokens}")
    t_total = _time.monotonic()

    for rnd in range(max_tool_rounds):
        t0 = _time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            tools=[_READ_FILE_TOOL],
            messages=messages,
        )
        elapsed = _time.monotonic() - t0
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "input_tokens", "?")
        out_tok = getattr(usage, "output_tokens", "?")
        _dbg(
            f"  round {rnd + 1} {elapsed:.1f}s stop={response.stop_reason} "
            f"in={in_tok} out={out_tok}"
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _execute_read_file(block.input)
                    _dbg(
                        f"  tool read_file path={block.input.get('path','?')} "
                        f"lines={block.input.get('start_line','?')}-{block.input.get('end_line','?')} "
                        f"=> {len(result):,}ch"
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        elif response.stop_reason in ("end_turn", "stop_sequence", "max_tokens", None):
            text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            _dbg(f"_call_with_tools total {_time.monotonic() - t_total:.1f}s")
            if response.stop_reason == "max_tokens":
                import warnings

                warnings.warn(
                    f"_call_with_tools: max_tokens reached after round {rnd + 1} — "
                    "output may be truncated; attempting partial parse",
                    RuntimeWarning,
                    stacklevel=2,
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
    enrich_ctx: Optional[_IntentContext] = None,
) -> tuple[str, list[str]]:
    """Return (project_essence, ordered_key_files)."""
    if verbose:
        print("[intent] step 1: triage — identifying key files...")

    historical_context = (
        _enrich_project_ctx(enrich_ctx) if enrich_ctx else _collect_readme(root)
    )

    template = _load_prompt("triage")
    prompt = _render(
        template,
        project_name=root.name,
        file_listing=_file_listing(root),
        readme_excerpt=historical_context,
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
    """Git history of intent: dated log + pattern analysis (reverts, repeated fixes, density)."""
    import subprocess
    import re as _re
    from datetime import date, timedelta

    cwd = str(path.parent)

    def _run(*args: str, timeout: int = 5) -> str:
        try:
            r = subprocess.run(
                list(args), capture_output=True, text=True, timeout=timeout, cwd=cwd
            )
            return r.stdout.strip()
        except Exception:
            return ""

    # Dated subjects + author: "<hash> <YYYY-MM-DD> <author> <subject>"
    raw = _run(
        "git", "log", "--follow", "--format=%h %as %an\t%s", "-40", "--", str(path)
    )
    if not raw:
        return "(no git history for this file)"

    # Parse into structured records
    record_re = _re.compile(r"^([0-9a-f]+)\s+(\d{4}-\d{2}-\d{2})\s+([^\t]+)\t(.*)$")
    records: list[tuple[str, str, str, str]] = []  # (hash, date, author, subject)
    for ln in raw.splitlines():
        m = record_re.match(ln)
        if m:
            records.append((m.group(1), m.group(2), m.group(3), m.group(4)))

    if not records:
        return "(no git history for this file)"

    # --- Temporal density ---
    today = date.today()
    recent_cutoff = (today - timedelta(days=90)).isoformat()
    recent = [r for r in records if r[1] >= recent_cutoff]
    span_days = max(
        1,
        (date.fromisoformat(records[0][1]) - date.fromisoformat(records[-1][1])).days,
    )
    commits_per_week = len(records) / max(1, span_days / 7)

    # --- Pattern analysis ---
    patterns: list[str] = []
    revert_re = _re.compile(r"\brevert\b", _re.IGNORECASE)
    readd_re = _re.compile(
        r"\b(re-add|readd|re-implement|reimplement|restore|re-introduce)\b",
        _re.IGNORECASE,
    )
    fix_re = _re.compile(r"\b(fix|fixes|fixed|repair|correct|bug)\b", _re.IGNORECASE)
    ensure_re = _re.compile(
        r"\b(ensure|guarantee|always|enforce|must)\b", _re.IGNORECASE
    )
    verify_re = _re.compile(
        r"\b(test|verify|assert|check|spec|proof)\b", _re.IGNORECASE
    )
    # Stopwords for theme extraction — strip these before clustering
    _STOP = frozenset(
        "fix fixes fixed the a an and or of in for with to from that this "
        "is was are were be been has have had it its also now only not "
        "feat chore refactor update add remove when if".split()
    )

    def _themes(subj: str) -> list[str]:
        """Extract content words from a commit subject for theme clustering."""
        # Strip conventional prefix like "fix(intent+heal):" → "intent heal"
        subj = _re.sub(
            r"^[a-z]+\(([^)]+)\):",
            lambda m: m.group(1).replace("+", " ").replace(",", " "),
            subj,
        )
        words = _re.split(r"[\s\-_/+,.:]+", subj.lower())
        return [w for w in words if w and w not in _STOP and len(w) > 2]

    # Reverts — intent attempted and pulled back
    revert_records = [r for r in records if revert_re.search(r[3])]
    readd_records = [r for r in records if readd_re.search(r[3])]
    if revert_records:
        bodies: list[str] = []
        for rhash, rdate, _author, rsubj in revert_records[:3]:
            body = _run("git", "show", "--format=%b", "--no-patch", rhash)
            bodies.append(
                f"  {rdate} {rsubj}"
                + (f"\n    [{body[:200].strip()}]" if body.strip() else "")
            )
        label = "REVERT+READD" if readd_records else "REVERT"
        note = (
            " — intent attempted, pulled back, then re-attempted"
            if readd_records
            else " — intent attempted and pulled back"
        )
        patterns.append(
            f"{label} ({len(revert_records)}x{note}):\n" + "\n".join(bodies)
        )
        if readd_records:
            patterns[-1] += "\n  Re-add attempts:\n" + "\n".join(
                f"  {r[1]} {r[3]}" for r in readd_records[:3]
            )

    # Repeated fixes — clustered by theme, bodies for dominant cluster
    fix_records = [r for r in records if fix_re.search(r[3])]
    if len(fix_records) >= 3:
        # Cluster by theme: count word co-occurrence across fix subjects
        theme_count: dict[str, int] = {}
        theme_commits: dict[str, list[tuple]] = {}
        for rec in fix_records:
            for word in _themes(rec[3]):
                theme_count[word] = theme_count.get(word, 0) + 1
                theme_commits.setdefault(word, []).append(rec)
        # Top themes appearing in ≥2 fixes
        top_themes = sorted(
            [(w, c) for w, c in theme_count.items() if c >= 2],
            key=lambda x: -x[1],
        )[:4]

        fix_recent = [r for r in fix_records if r[1] >= recent_cutoff]
        accel = (
            f" — {len(fix_recent)} in last 90 days, accelerating"
            if fix_recent and len(fix_recent) >= len(fix_records) // 2
            else ""
        )
        fix_section = (
            f"REPEATED FIXES ({len(fix_records)}x — recurring instability{accel}):\n"
        )
        if top_themes:
            fix_section += "  Recurring themes:\n"
            for word, count in top_themes:
                # Fetch body for the most recent fix in this theme cluster
                cluster = sorted(theme_commits[word], key=lambda r: r[1], reverse=True)
                rhash, rdate, _author, rsubj = cluster[0]
                body = _run("git", "show", "--format=%b", "--no-patch", rhash)
                body_note = f"\n      [{body[:150].strip()}]" if body.strip() else ""
                fix_section += f"    '{word}' ×{count}: {rdate} {rsubj}{body_note}\n"
        else:
            fix_section += "\n".join(f"  {r[1]} {r[3]}" for r in fix_records[:6])
        patterns.append(fix_section.rstrip())

    # Unverified assertions — check window of ±2 commits
    unverified = []
    for idx, r in enumerate(records):
        if not ensure_re.search(r[3]):
            continue
        window = records[max(0, idx - 2) : idx + 3]
        if not any(verify_re.search(w[3]) for w in window):
            unverified.append(r)
    if unverified:
        patterns.append(
            f"UNVERIFIED ASSERTIONS ({len(unverified)}x — 'ensure/always' with no nearby test/verify commit):\n"
            + "\n".join(f"  {r[1]} {r[3]}" for r in unverified[:4])
        )

    # Author diversity — ownership signal
    authors = [r[2] for r in records]
    unique_authors = sorted(set(authors))
    if len(unique_authors) >= 3:
        from collections import Counter

        author_counts = Counter(authors).most_common(5)
        patterns.append(
            f"AUTHOR DIVERSITY ({len(unique_authors)} authors — unclear ownership or coordination overhead):\n"
            + "\n".join(f"  {a} ({c}x)" for a, c in author_counts)
        )
    elif len(unique_authors) == 1:
        patterns.append(f"SINGLE AUTHOR ({unique_authors[0]}) — knowledge silo risk")

    # Build output
    density_line = ""
    if commits_per_week >= 2:
        density_line = (
            f"HIGH COMMIT DENSITY ({commits_per_week:.1f}/week over {span_days}d"
            + (f", {len(recent)} in last 90 days" if recent else "")
            + " — load-bearing or poorly bounded)"
        )
    elif len(records) >= 20:
        density_line = (
            f"COMMIT DENSITY: {len(records)} commits over {span_days}d "
            f"({commits_per_week:.1f}/week)"
        )

    sections = [
        f"Recent commits ({len(records)} shown, {records[0][1]} → {records[-1][1]}):\n"
        + "\n".join(f"  {r[1]} {r[3]}" for r in records[:10])
        + (f"\n  ... ({len(records) - 10} more)" if len(records) > 10 else "")
    ]
    if patterns:
        sections.append(
            "=== INTENT PATTERNS (first-class signals) ===\n" + "\n\n".join(patterns)
        )
    if density_line:
        sections.append(density_line)

    return "\n\n".join(sections)


def _find_test_files(root: Path) -> list[Path]:
    """Return all test_*.py and *_test.py files under *root*, skipping venv dirs."""
    results = []
    for p in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            results.append(p)
    return results


def _extract_test_intent(test_files: list[Path]) -> list[dict]:
    """AST-parse test files and extract L1.5 intent signals from test function names.

    For each test function whose name starts with ``test_``:
    - strips the ``test_`` prefix and converts underscores to spaces for the description
    - records the first ``assert`` statement body as an assertion pattern
    - records the enclosing class name when the class has at least one base
      (proxy for TestCase subclass without executing code)

    Returns a list of dicts with keys:
        file, test_name, description, assertion_pattern, class_name, confidence (0.75)

    SyntaxError in any file is caught and skipped with a RuntimeWarning.
    Functions whose names do not start with ``test_`` are ignored.
    """
    import warnings

    results: list[dict] = []
    for tf in test_files:
        try:
            source = tf.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(tf))
        except SyntaxError as exc:
            warnings.warn(
                f"_extract_test_intent: skipped {tf} (SyntaxError: {exc})",
                RuntimeWarning,
                stacklevel=2,
            )
            continue

        def _first_assert(body: list) -> str:
            for stmt in body:
                if isinstance(stmt, ast.Assert):
                    try:
                        return ast.unparse(stmt.test)[:200]
                    except Exception:
                        return ""
            return ""

        def _make_signal(node, cls_name: str) -> dict:
            description = node.name[len("test_") :].replace("_", " ")
            return {
                "file": str(tf),
                "test_name": node.name,
                "description": description,
                "assertion_pattern": _first_assert(node.body),
                "class_name": cls_name,
                "confidence": 0.75,
            }

        # Collect class-level test methods (with base classes → TestCase proxy)
        seen: set[int] = set()
        for cls_node in ast.walk(tree):
            if not isinstance(cls_node, ast.ClassDef):
                continue
            if not cls_node.bases:
                continue
            for item in cls_node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not item.name.startswith("test_"):
                    continue
                seen.add(id(item))
                results.append(_make_signal(item, cls_node.name))

        # Top-level test functions not already collected via a class
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if id(node) in seen:
                continue
            results.append(_make_signal(node, ""))

    return results


def _match_tests_for_module(source_path: Path, test_signals: list[dict]) -> list[dict]:
    """Return test signals whose file name matches the stem of *source_path*.

    e.g. ``intent.py`` matches ``test_intent.py`` or ``intent_test.py``.
    The match is intentionally loose (stem substring) so ``test_intent_extra.py``
    still counts as coverage for ``intent.py``.
    """
    stem = source_path.stem
    matched = []
    seen: set[str] = set()
    for sig in test_signals:
        tf_stem = Path(sig["file"]).stem
        tf_module = tf_stem
        if tf_module.startswith("test_"):
            tf_module = tf_module[len("test_") :]
        if tf_module.endswith("_test"):
            tf_module = tf_module[: -len("_test")]
        if stem == tf_module or stem in tf_module:
            key = f"{sig['file']}::{sig['test_name']}"
            if key not in seen:
                seen.add(key)
                matched.append(sig)
    return matched


def _extract_intent_signals(source: str, source_path: Optional[Path] = None) -> str:
    """Module docstring + per-function docstrings (first line) + TODO/FIXME/HACK/BUG comments.

    When *source_path* is supplied, also scans sibling test files for L1.5 intent
    signals: test function names encode behavioural intent more precisely than most
    docstrings (confidence 0.75 — stronger than commit messages, weaker than ADRs).
    """
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

    # --- L1.5: test-name intent signals ---
    if source_path is not None:
        test_files = _find_test_files(source_path.parent)
        all_test_signals = _extract_test_intent(test_files)
        matched = _match_tests_for_module(source_path, all_test_signals)
        if matched:
            n_files = len({s["file"] for s in matched})
            signals.append(
                f"[TEST COVERAGE — L1.5 ({len(matched)} test functions "
                f"in {n_files} file(s))]"
            )
            for sig in matched[:20]:  # cap to avoid prompt bloat
                prefix = f"  [{sig['class_name']}] " if sig["class_name"] else "  "
                entry = f"{prefix}{sig['description']}"
                if sig["assertion_pattern"]:
                    entry += f"  → assert {sig['assertion_pattern'][:100]}"
                signals.append(entry)

    return "\n".join(signals) if signals else "(none found)"


_SIGNATURE_ONLY_BYTES = 30_000  # files larger than this get sig-summary + tool calls


def _understand_module(
    path: Path,
    project_essence: str,
    model: str,
    key: str,
    verbose: bool,
    graphify_rationale: str = "",
    enrich_ctx: Optional[_IntentContext] = None,
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
    if enrich_ctx:
        enriched = _enrich_file_ctx(enrich_ctx, str(path))
        if enriched:
            git_log = enriched + "\n\n---\n\n" + git_log
    intent_signals = _extract_intent_signals(full_source, source_path=path)

    template = _load_prompt("understand")
    graphify_section = (
        f"### Graphify community rationale\n{graphify_rationale}\n"
        if graphify_rationale
        else ""
    )
    prompt = _render(
        template,
        project_essence=project_essence,
        filename=path.name,
        file_path=str(path.resolve()),
        source=source,
        truncation_note=trunc_note,
        git_log=git_log,
        intent_signals=intent_signals,
        graphify_rationale=graphify_section,
    )

    # Use tool-enabled call — model reads specific function bodies on demand.
    raw = _call_with_tools(prompt, model, key, max_tokens=8192)

    u = raw.get("understanding", {})
    understanding = ProjectUnderstanding(
        purpose=u.get("purpose", ""),
        design_intent=u.get("design_intent", ""),
        key_abstractions=u.get("key_abstractions", ""),
        behavioral_contract=u.get("behavioral_contract", ""),
        failure_modes=u.get("failure_modes", ""),
        assumptions=u.get("assumptions", ""),
        resource_obligations=u.get("resource_obligations", ""),
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
        if v.get("invariant_id") or v.get("explanation")  # drop hollow shells
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

_IMPROVE_PROMPT_RE = re.compile(
    r"---BEGIN IMPROVED PROMPT---\n(.*?)\n---END IMPROVED PROMPT---",
    re.DOTALL,
)


def _parse_improve_response(raw_text: str) -> tuple[dict, str]:
    """Parse the two-part improve.md response into (scores_dict, improved_prompt_text)."""
    # Extract the improved prompt from its delimited section first (avoids JSON escaping issues)
    improved = ""
    m = _IMPROVE_PROMPT_RE.search(raw_text)
    if m:
        improved = m.group(1).strip()

    # Parse the JSON scores block — strip the improved-prompt section before parsing
    scores_text = raw_text
    if m:
        scores_text = raw_text[: m.start()] + raw_text[m.end() :]
    try:
        scores_dict = _parse_text(scores_text.strip())
    except RuntimeError:
        scores_dict = {}

    return scores_dict, improved


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
        from .llm import make_client

        client = make_client(key)
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text if response.content else ""
    except Exception as exc:
        if verbose:
            print(f"    improvement scoring failed: {exc}")
        return None

    raw, improved = _parse_improve_response(raw_text)
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
        from .llm import make_client

        client = make_client(key)
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text if response.content else ""
        _, improved = _parse_improve_response(raw_text)
        if improved:
            _save_prompt("understand", improved)
            if verbose:
                print(
                    f"  ✓ understand prompt rewritten (batch, worst score {worst[0][0]:.2f})"
                )
        elif verbose:
            print("  batch improve: no improved prompt extracted from response")
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
# ADR → import-linter contract synthesis
# ---------------------------------------------------------------------------

_ADR_RULE_PROMPT = """\
You are a structural analysis tool. Read the ADR text below and determine whether
it describes a constraint on which Python modules may import which other modules.

If yes, extract exactly:
- rule_type: "forbidden_import", "required_dependency", or "layer_ordering"
- source_module: the module/package that is the subject of the constraint (short name, e.g. "api")
- forbidden_modules: list of module/package names that source_module must NOT import (for forbidden_import/layer_ordering)
- required_modules: list of module/package names that source_module MUST import (for required_dependency)
- rationale: one sentence explaining the constraint

Return JSON like:
{{"rule_type": "forbidden_import", "source_module": "api", "forbidden_modules": ["db", "models"], "required_modules": [], "rationale": "API layer must not depend on DB layer directly"}}

If no structural import constraint exists in the ADR, return exactly:
{{"rule_type": null}}

ADR text:
{adr_text}
"""


def _extract_adr_rule(adr_text: str, key: str, model: str) -> "dict | None":
    """
    Use the LLM to extract a structural import constraint from ADR text.

    Returns a dict with rule_type, source_module, forbidden_modules, rationale
    if the ADR describes an import constraint, or None if no constraint is found.
    Guards gracefully if key is empty.
    """
    if not key or not adr_text.strip():
        return None

    prompt = _ADR_RULE_PROMPT.format(adr_text=adr_text[:3000])
    try:
        raw = _call(prompt, model, key, max_tokens=512)
    except Exception:
        return None

    rule_type = raw.get("rule_type")
    if not rule_type:
        return None

    source = raw.get("source_module", "").strip()
    if not source:
        return None

    return {
        "rule_type": rule_type,
        "source_module": source,
        "forbidden_modules": raw.get("forbidden_modules") or [],
        "required_modules": raw.get("required_modules") or [],
        "rationale": raw.get("rationale", ""),
    }


def _contract_id(rule: dict) -> str:
    """Build a stable contract identifier from a rule dict."""
    src = re.sub(r"[^a-z0-9]+", "-", rule["source_module"].lower()).strip("-")
    targets = rule.get("forbidden_modules") or rule.get("required_modules") or []
    tgt = re.sub(r"[^a-z0-9]+", "-", (targets[0] if targets else "none").lower()).strip(
        "-"
    )
    rtype = re.sub(r"[^a-z0-9]+", "-", rule["rule_type"].lower()).strip("-")
    return f"adr-{src}-{rtype}-{tgt}"


def _emit_importlinter_contract(rule: dict, project_root: Path) -> str:
    """
    Generate an import-linter contract block in .importlinter format.
    Returns the contract text as a string (does not write to disk).
    """
    contract_id = _contract_id(rule)
    rationale = rule.get("rationale", "")
    source = rule["source_module"]
    rule_type = rule["rule_type"]

    # Map rule_type to import-linter contract type
    if rule_type in ("forbidden_import", "layer_ordering"):
        contract_type = "forbidden"
    elif rule_type == "required_dependency":
        contract_type = "layers"
    else:
        contract_type = "forbidden"

    lines = [
        f"[importlinter:contract:{contract_id}]",
        f"name = {rationale or f'{source} import constraint (from ADR)'}",
        f"type = {contract_type}",
        "source_modules =",
        f"    {source}",
    ]

    forbidden = rule.get("forbidden_modules") or []
    required = rule.get("required_modules") or []

    if forbidden:
        lines.append("forbidden_modules =")
        for m in forbidden:
            lines.append(f"    {m}")
    elif required and contract_type == "layers":
        lines.append("layers =")
        for m in required:
            lines.append(f"    {m}")

    return "\n".join(lines) + "\n"


_IMPORTLINTER_HEADER = "[importlinter]\nroot_package = .\n\n"


def _write_importlinter_contract(
    rule: dict, project_root: Path, verbose: bool = False
) -> bool:
    """
    Append an import-linter contract to .importlinter in project_root.
    Skips silently if the contract id is already present.
    Returns True if written, False if skipped.
    """
    contract_id = _contract_id(rule)
    dotfile = project_root / ".importlinter"

    existing = dotfile.read_text(encoding="utf-8") if dotfile.exists() else ""

    # Skip if contract already present
    if f"[importlinter:contract:{contract_id}]" in existing:
        return False

    contract_text = _emit_importlinter_contract(rule, project_root)

    if not existing:
        dotfile.write_text(_IMPORTLINTER_HEADER + contract_text, encoding="utf-8")
    else:
        # Ensure a blank line separator
        sep = "\n" if existing.endswith("\n") else "\n\n"
        with dotfile.open("a", encoding="utf-8") as fh:
            fh.write(sep + contract_text)

    if verbose:
        print(f"  [adr-contract] wrote contract '{contract_id}' to {dotfile}")

    return True


def _synthesize_adr_contracts(
    enrich_ctx: "_IntentContext",
    project_root: Path,
    key: str,
    model: str,
    verbose: bool,
) -> list[dict]:
    """
    Process all ADR docs in enrich_ctx, extract import rules via LLM, and
    write matching import-linter contracts to .importlinter.

    Returns list of extracted rule dicts (empty if no LLM key or no ADRs).
    Guards silently: any individual ADR failure is skipped.
    """
    if not key:
        return []

    gh = getattr(enrich_ctx, "github", None)
    if gh is None:
        return []

    adr_docs: list[tuple[str, str]] = getattr(gh, "adr_docs", []) or []
    if not adr_docs:
        return []

    if verbose:
        print(
            f"[intent] synthesizing import-linter contracts from {len(adr_docs)} ADR(s)..."
        )

    rules: list[dict] = []
    for ref_key, content in adr_docs:
        try:
            rule = _extract_adr_rule(content, key, model)
        except Exception:
            rule = None

        if rule is None:
            continue

        # Attach ADR provenance
        rule["adr_ref"] = ref_key

        written = _write_importlinter_contract(rule, project_root, verbose=verbose)
        if verbose and not written:
            contract_id = _contract_id(rule)
            print(f"  [adr-contract] skipped duplicate contract '{contract_id}'")

        rules.append(rule)

    if verbose and rules:
        print(f"  [adr-contract] {len(rules)} rule(s) extracted from ADRs")

    return rules


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
    graphify_rationale: str = "",
    enrich_ctx: Optional[_IntentContext] = None,
) -> ModuleIntent:
    """Extract world model + invariants + violations from one Python file."""
    key = _get_key(api_key)
    source, _ = _read_truncated(path)

    module = _understand_module(
        path, project_essence, model, key, verbose, graphify_rationale, enrich_ctx
    )

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


def _classify_contract_kind(
    encoding_approach: str,
    statement: str,
) -> tuple[str, str]:
    """Map Z3 encoding description to (contract_kind, tla_template)."""
    text = (encoding_approach + " " + statement).lower()
    if any(
        k in text
        for k in (
            "order",
            "before",
            "after",
            "sequence",
            "precede",
            "phase",
            "guard",
            "gated on",
            "must be checked",
            "guard_requirement",
        )
    ):
        return "ordering", "ordering"
    if any(
        k in text
        for k in (
            "resource",
            "lifecycle",
            "open",
            "close",
            "acquire",
            "release",
            "allocat",
            "free",
        )
    ):
        return "resource_lifecycle", "resource_lifecycle"
    if any(
        k in text
        for k in (
            "accumulate",
            "grow",
            "unbounded",
            "leak",
            "never clear",
            "state size",
        )
    ):
        return "accumulation", "accumulation"
    if any(
        k in text
        for k in (
            "catches",
            "except ",
            "silently return",
            "swallow",
            "returns none",
            "error_contract",
            "syntaxerror",
            "oserror",
            "valueerror",
        )
    ):
        return "error_contract", "liveness"
    if any(
        k in text
        for k in ("flag", "boolean", "_has_", "enabled", "disabled", "silently")
    ):
        return "flag_invariant", "liveness"
    if any(k in text for k in ("subset", "must include", "required_args", "missing")):
        return "subset_relation", "liveness"
    return "behavioral", "liveness"


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

            # Populate contract IR fields — pipeline reuses these to skip LLM re-encoding
            if result.z3_script:
                kind, tla = _classify_contract_kind(
                    result.encoding_approach, inv.statement
                )
                inv.contract_kind = kind
                inv.z3_encoding = result.z3_script
                inv.tla_template = tla

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
    max_files: int = 0,
) -> ProjectIntent:
    """
    Full pipeline: triage → understand each key module → (optionally) improve prompts.

    max_files: cap on modules to analyze. 0 = unlimited (triage decides).
    """
    import datetime

    key = _get_key(api_key)

    # Gather stated intent before any LLM call: docs + git history with bodies
    if verbose:
        print("[intent] gathering stated intent (docs, git history)...")
    enrich_ctx = _enrich_gather(root)
    if verbose:
        ndocs = len(enrich_ctx.project_docs)
        ncommits = len(enrich_ctx.commit_log)
        nfiles = len(enrich_ctx.file_commits)
        print(f"  {ndocs} doc files, {ncommits} commits, {nfiles} files with history")

    # ADR → import-linter contract synthesis (LLM-optional, silently skipped if no key)
    try:
        adr_rules = _synthesize_adr_contracts(enrich_ctx, root, key, model, verbose)
    except Exception:
        adr_rules = []

    intent = ProjectIntent(
        project=str(root.resolve()),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        source_model=model,
    )
    # Store ADR-derived structural rules on intent for downstream consumers
    if adr_rules:
        intent.__dict__["adr_import_rules"] = adr_rules

    # Step 1: triage — cached by file listing + model so re-runs skip the 60s LLM call
    import hashlib as _hl

    _triage_stages = root / ".pact_stages"
    _triage_stages.mkdir(parents=True, exist_ok=True)
    _triage_key = _hl.sha256((_file_listing(root) + model).encode()).hexdigest()[:16]
    _triage_cache = _triage_stages / f"triage_{_triage_key}.json"
    _triage_hit = False
    try:
        if _triage_cache.exists():
            _tc = json.loads(_triage_cache.read_text())
            essence = _tc.get("essence", "")
            key_files = _tc.get("key_files", [])
            intent.project_summary = essence
            intent.key_files = key_files
            _triage_hit = True
            if verbose:
                print("[intent] step 1: triage — (cached)")
                print(f"  essence: {essence[:180].replace(chr(10), ' ')}...")
                print(
                    f"  key files ({len(key_files)}): {', '.join(key_files[:5])}{'...' if len(key_files) > 5 else ''}"
                )
        if not _triage_hit:
            essence, key_files = _triage(
                root, model, key, verbose, enrich_ctx=enrich_ctx
            )
            intent.project_summary = essence
            intent.key_files = key_files
            _triage_cache.write_text(
                json.dumps({"essence": essence, "key_files": key_files}, indent=2)
            )
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

    # Resolve key files to paths; fall back to top-N by size.
    # Small files (<_SIGNATURE_ONLY_BYTES) are analyzed before large ones so
    # violations appear quickly rather than waiting on God-class timeouts.
    def _analysis_order(p: Path) -> tuple[int, int]:
        sz = p.stat().st_size
        return (1 if sz > _SIGNATURE_ONLY_BYTES else 0, sz)

    if key_files:
        resolved = []
        for rel in key_files:
            p = root / rel
            if p.exists():
                resolved.append(p)
        if not resolved:
            resolved = sorted(
                [f for f in _iter_python_files(root) if f.stat().st_size > 500],
                key=_analysis_order,
            )
        else:
            resolved.sort(key=_analysis_order)
        files = resolved[:max_files] if max_files > 0 else resolved
    else:
        all_py = sorted(
            [f for f in _iter_python_files(root) if f.stat().st_size > 500],
            key=_analysis_order,
        )
        files = all_py[:max_files] if max_files > 0 else all_py

    # Step 2–4: understand each module (always without per-module improvement here)
    if verbose:
        print(f"[intent] step 2-4: understanding {len(files)} modules...")

    # Load Graphify call graph for rationale enrichment (optional — silently absent)
    try:
        from .graphify_graph import CallGraph as _CG

        _call_graph = _CG.load(root)
    except Exception:
        _call_graph = None

    _MODULE_TIMEOUT = 120  # seconds per module
    _MAX_WORKERS = 5  # concurrent LLM calls; bounded by API rate limits

    def _build_task(f: Path) -> tuple[Path, str, str]:
        src, _ = _read_truncated(f)
        rat = ""
        if _call_graph is not None:
            comm_id = _call_graph.community_of(func_name="", source_file=f.name)
            if comm_id is not None:
                rat = _call_graph.community_label_for(comm_id)
        return f, src, rat

    import concurrent.futures as _cf
    import hashlib as _hashlib

    # Staged artifact cache — write each module result to disk as it completes.
    # Lives in the target repo so cache persists across runs regardless of --out path.
    # Key: sha256(file_bytes + essence + model). On re-run, hit = skip LLM call.
    _stages_dir = root / ".pact_stages"
    _stages_dir.mkdir(parents=True, exist_ok=True)
    # Silently add to .gitignore if the target is a git repo and doesn't already ignore it
    _gi = root / ".gitignore"
    try:
        _gi_text = _gi.read_text() if _gi.exists() else ""
        if ".pact_stages" not in _gi_text:
            with _gi.open("a") as _fh:
                _fh.write("\n.pact_stages/\n")
    except Exception:
        pass

    def _cache_key(f: Path, src: str) -> str:
        # Keyed on file content + model only — not essence, which is non-deterministic
        # across triage runs. Cache invalidates when the file itself changes.
        raw = f"{src}{model}".encode()
        return _hashlib.sha256(raw).hexdigest()[:16]

    def _cache_load(key: str) -> Optional[ModuleIntent]:
        p = _stages_dir / f"{key}.json"
        if not p.exists():
            return None
        try:
            return _module_intent_from_dict(json.loads(p.read_text()))
        except Exception:
            return None

    def _cache_save(key: str, module: ModuleIntent) -> None:
        p = _stages_dir / f"{key}.json"
        try:
            p.write_text(json.dumps(asdict(module), indent=2))
        except Exception:
            pass

    tasks = [_build_task(f) for f in files]
    module_sources: dict[str, str] = {str(f): src for f, src, _ in tasks}

    def _analyze_module(f: Path, src: str, rat: str) -> ModuleIntent:
        ck = _cache_key(f, src)
        cached = _cache_load(ck)
        if cached is not None:
            if verbose:
                print(f"  → {f.name} (cached)")
            return cached
        module = extract_file_intent(
            f,
            project_essence=essence,
            model=model,
            api_key=key,
            improve=False,
            verbose=verbose,
            graphify_rationale=rat,
            enrich_ctx=enrich_ctx,
        )
        _cache_save(ck, module)
        return module

    future_to_file: dict = {}
    # Use a thread pool; verbose printing from within threads is thread-safe on CPython.
    with _cf.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        for f, src, rat in tasks:
            fut = pool.submit(_analyze_module, f, src, rat)
            future_to_file[fut] = f

        for fut in _cf.as_completed(
            future_to_file, timeout=_MODULE_TIMEOUT * len(tasks)
        ):
            f = future_to_file[fut]
            try:
                module = fut.result(timeout=0)  # already done — no wait
                for inv in module.invariants:
                    if not inv.contract_kind:
                        kind, tla = _classify_contract_kind("", inv.statement)
                        inv.contract_kind = kind
                        inv.tla_template = tla
                intent.modules.append(module)
            except _cf.TimeoutError:
                import warnings

                warnings.warn(
                    f"extract_project_intent: skipped {f.name}: module analysis timed out after {_MODULE_TIMEOUT}s",
                    RuntimeWarning,
                    stacklevel=2,
                )
                if verbose:
                    print(f"  skipped {f.name}: timed out after {_MODULE_TIMEOUT}s")
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
    analyze.add_argument(
        "--md-out",
        metavar="PATH",
        help="write intent.md here (LLM-consumable narrative)",
    )
    analyze.add_argument(
        "--format",
        choices=["json", "markdown", "both"],
        default="json",
        help="stdout format when --out not given (default: json)",
    )
    analyze.add_argument("--model", default=_DEFAULT_MODEL)
    analyze.add_argument(
        "--max-files",
        type=int,
        default=0,
        metavar="N",
        help="max modules to analyze (0 = unlimited, triage decides)",
    )
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
    md_out = (
        Path(args.md_out).expanduser().resolve()
        if getattr(args, "md_out", None)
        else None
    )
    fmt = getattr(args, "format", "json")

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
        # Always write markdown alongside JSON when --out is given
        if out and isinstance(result, ProjectIntent):
            md_path = md_out or out.with_suffix(".md")
            md_path.write_text(result.to_markdown(), encoding="utf-8")
            if getattr(args, "verbose", False):
                print(f"[intent] wrote {md_path}")
        elif md_out and isinstance(result, ProjectIntent):
            md_out.write_text(result.to_markdown(), encoding="utf-8")
        # stdout: honour --format
        if not out:
            if fmt == "markdown":
                print(
                    result.to_markdown()
                    if isinstance(result, ProjectIntent)
                    else result.to_json()
                )
            elif fmt == "both":
                print(result.to_markdown() if isinstance(result, ProjectIntent) else "")
                print("\n---\n")
                print(result.to_json() if isinstance(result, ProjectIntent) else "")
            else:
                print(result.to_json() if isinstance(result, ProjectIntent) else result)


if __name__ == "__main__":
    main()
