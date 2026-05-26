"""
enrich.py — "What did the developers INTEND to build?"

Gathers stated intent from every source except code:
  - git commit messages and bodies (the WHY behind every change)
  - documentation files (README, CONTRIBUTING, CHANGELOG, ADRs, etc.)
  - commit patterns: high-churn areas, recurring fixes, architectural decisions

This runs BEFORE the LLM sees any code. It answers the foundational question
that makes pact meaningful: what were these people trying to build, and why
did they build it this way?
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_DOC_EXTENSIONS = {".md", ".rst", ".txt"}
_DOC_STEMS = {
    "README",
    "CHANGELOG",
    "CHANGES",
    "CONTRIBUTING",
    "ARCHITECTURE",
    "DESIGN",
    "OVERVIEW",
    "SECURITY",
    "NOTES",
    "DECISIONS",
    "ADR",
    "CLAUDE",
    "AGENTS",
}
_DOC_DIRS = {"docs", "doc", "documentation", "wiki", "adrs", "decisions", "rfcs", "rfc"}
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    "coverage",
    ".mypy_cache",
}
_MAX_DOC_CHARS = 8_000
_FIX_RE = re.compile(r"\b(fix|fixes|fixed|repair|bug|revert)\b", re.IGNORECASE)
_ARCH_RE = re.compile(
    r"\b(refactor|redesign|rewrite|migrate|extract|split|merge|introduce|add|feat)\b",
    re.IGNORECASE,
)


@dataclass
class CommitEntry:
    sha: str
    author: str
    date: str
    subject: str
    body: str
    files: list[str] = field(default_factory=list)

    def render(self, include_body: bool = True) -> str:
        parts = [f"[{self.date}] {self.subject}"]
        if include_body and self.body.strip():
            # Indent body for readability
            indented = "\n".join(f"  {ln}" for ln in self.body.strip().splitlines())
            parts.append(indented)
        return "\n".join(parts)


@dataclass
class IntentContext:
    project_docs: list[tuple[str, str]]  # [(rel_path, content)]
    commit_log: list[CommitEntry]  # all fetched commits, newest first
    churn_map: dict[str, int]  # file → commit count
    file_commits: dict[str, list[CommitEntry]]  # file → commits touching it


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def gather(root: Path, max_commits: int = 400) -> IntentContext:
    """
    Gather all available stated-intent context for a project.
    Single entry point — call once at the start of intent analysis.
    """
    docs = _gather_docs(root)
    commits, file_commits = _gather_commits(root, max_commits)
    churn = {f: len(cs) for f, cs in file_commits.items()}
    return IntentContext(
        project_docs=docs,
        commit_log=commits,
        churn_map=churn,
        file_commits=file_commits,
    )


def render_project_context(ctx: IntentContext, char_budget: int = 10_000) -> str:
    """
    Render project-level intent context as markdown for the triage LLM prompt.
    Covers: documentation, commit narrative, architectural decisions, pain points.
    """
    sections: list[str] = []
    remaining = char_budget

    # --- Documentation ---
    if ctx.project_docs:
        doc_parts: list[str] = []
        for rel_path, content in ctx.project_docs:
            # README gets more space; others get less
            cap = 4_000 if "readme" in rel_path.lower() else 1_500
            chunk = content[:cap].strip()
            if chunk:
                doc_parts.append(f"### {rel_path}\n{chunk}")
                remaining -= len(chunk)
            if remaining <= 0:
                break
        if doc_parts:
            sections.append("## Project Documentation\n\n" + "\n\n".join(doc_parts))

    # --- Commit narrative ---
    if ctx.commit_log:
        total = len(ctx.commit_log)

        # Architectural decisions: commits with substantive bodies
        arch = [
            c for c in ctx.commit_log if c.body.strip() and len(c.body.strip()) > 80
        ]
        # Pain points: commits whose subject contains fix/bug/revert
        fixes = [c for c in ctx.commit_log if _FIX_RE.search(c.subject)]
        # High-churn files
        top_churn = sorted(ctx.churn_map.items(), key=lambda x: x[1], reverse=True)[:10]

        parts: list[str] = [f"Total commits: {total}"]

        if arch:
            decision_lines: list[str] = []
            budget = min(3_000, remaining // 3)
            for c in arch[:15]:
                t = c.render(include_body=True)
                decision_lines.append(t)
                budget -= len(t)
                if budget <= 0:
                    break
            parts.append(
                "### Architectural decisions (commits with substantive rationale)\n\n"
                + "\n\n---\n\n".join(decision_lines)
            )

        if top_churn:
            churn_lines = [f"- `{f}` — {n} commits" for f, n in top_churn]
            parts.append(
                "### High-churn areas (likely complex or fragile)\n\n"
                + "\n".join(churn_lines)
            )

        if fixes:
            fix_subjects = [f"- [{c.date}] {c.subject}" for c in fixes[:20]]
            parts.append(
                "### Recurring fixes (what kept breaking)\n\n" + "\n".join(fix_subjects)
            )

        sections.append("## Git History\n\n" + "\n\n".join(parts))

    return "\n\n---\n\n".join(sections)


def render_file_context(
    ctx: IntentContext, file_path: str, char_budget: int = 2_500
) -> str:
    """
    Render file-specific intent context for the understand LLM prompt.
    Returns commit bodies (the WHY) for this specific file.
    """
    # Normalise path — try both absolute-relative and basename matches
    commits = ctx.file_commits.get(file_path) or ctx.file_commits.get(
        Path(file_path).name, []
    )
    churn = ctx.churn_map.get(file_path, 0)

    parts: list[str] = []

    if churn:
        label = ""
        if churn > 30:
            label = " ⚠️ HIGH CHURN — likely complex or fragile"
        elif churn > 10:
            label = " (moderately active)"
        parts.append(f"**{churn} commits** have touched this file.{label}")

    if commits:
        lines: list[str] = []
        remaining = char_budget
        for c in commits[:25]:
            t = c.render(include_body=True)
            lines.append(t)
            remaining -= len(t)
            if remaining <= 0:
                break
        parts.append(
            "**Commit history (with rationale):**\n\n" + "\n\n---\n\n".join(lines)
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------


def _gather_docs(root: Path) -> list[tuple[str, str]]:
    """Collect documentation files, prioritised: top-level known names first, then doc dirs."""
    seen: set[Path] = set()
    result: list[tuple[str, str]] = []

    def _add(p: Path) -> None:
        if p in seen or not p.is_file():
            return
        seen.add(p)
        try:
            content = p.read_text(encoding="utf-8", errors="replace")[:_MAX_DOC_CHARS]
            if content.strip():
                result.append((str(p.relative_to(root)), content))
        except OSError:
            pass

    # Priority 1: top-level well-known docs
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix.lower() in _DOC_EXTENSIONS:
            if p.stem.upper() in _DOC_STEMS or any(
                s in p.stem.upper() for s in _DOC_STEMS
            ):
                _add(p)

    # Priority 2: named doc directories
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name.lower() in _DOC_DIRS and d.name not in _SKIP_DIRS:
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix.lower() in _DOC_EXTENSIONS:
                    _add(p)

    return result


def _gather_commits(
    root: Path, max_commits: int
) -> tuple[list[CommitEntry], dict[str, list[CommitEntry]]]:
    """
    Two git calls, one parse pass each:
      1. Commit metadata + bodies (the WHY)
      2. Per-commit file lists (for churn + file-level enrichment)
    Returns (all_commits, file→commits_map).
    """
    commits = _fetch_commit_metadata(root, max_commits)
    if not commits:
        return [], {}

    sha_to_commit = {c.sha: c for c in commits}
    file_commits: dict[str, list[CommitEntry]] = {}

    # Fetch file lists — one call, parse into sha→files
    try:
        result = subprocess.run(
            ["git", "log", f"-{max_commits}", "--name-only", "--format=__SHA__%H"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        current_sha: str | None = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("__SHA__"):
                current_sha = line[7:]
            elif line and current_sha:
                # Associate file with this commit
                if current_sha in sha_to_commit:
                    sha_to_commit[current_sha].files.append(line)
                file_commits.setdefault(line, [])
                if current_sha in sha_to_commit:
                    file_commits[line].append(sha_to_commit[current_sha])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return commits, file_commits


def _fetch_commit_metadata(root: Path, max_commits: int) -> list[CommitEntry]:
    """Fetch sha, author, date, subject, body for recent commits."""
    # Use \x00 as record separator, \x1f as field separator within header
    fmt = "%x00%H%x1f%an%x1f%ad%x1f%s%x1f%b"
    try:
        result = subprocess.run(
            ["git", "log", f"-{max_commits}", f"--format={fmt}", "--date=short"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits: list[CommitEntry] = []
    for block in result.stdout.split("\x00"):
        block = block.strip()
        if not block:
            continue
        parts = block.split("\x1f", 4)
        if len(parts) < 4:
            continue
        sha, author, date, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4].strip() if len(parts) > 4 else ""
        if sha.strip():
            commits.append(
                CommitEntry(
                    sha=sha.strip(),
                    author=author.strip(),
                    date=date.strip(),
                    subject=subject.strip(),
                    body=body,
                )
            )

    return commits
