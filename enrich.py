"""
enrich.py — "What did the developers INTEND to build?"

Gathers stated intent from every source except code:
  - git commit messages and bodies (the WHY behind every change)
  - documentation files (README, CONTRIBUTING, CHANGELOG, ADRs, etc.)
  - commit patterns: high-churn areas, recurring fixes, architectural decisions
  - GitHub issues and comments (what the team KNOWS is broken)
  - GitHub PRs and reviews (what was attempted, what was contested)
  - Spec branches and ADRs (architectural decisions in git)
  - All refs including forks (abandoned attempts, experiments)

This runs BEFORE the LLM sees any code. It answers the foundational question
that makes pact meaningful: what were these people trying to build, and why
did they build it this way?
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
class IssueEntry:
    number: int
    title: str
    state: str  # OPEN | CLOSED
    labels: list[str]
    body: str
    comments: list[str]  # comment bodies
    closed_at: str  # "" if open

    def is_wont_fix(self) -> bool:
        wf = {
            "wontfix",
            "wont-fix",
            "by-design",
            "bydesign",
            "intentional",
            "not-a-bug",
        }
        return any(lb.lower().replace(" ", "-") in wf for lb in self.labels)

    def render(self, include_comments: bool = True) -> str:
        status = f"[{self.state}]"
        if self.is_wont_fix():
            status += " [WON'T FIX]"
        if self.labels:
            status += f" [{', '.join(self.labels)}]"
        parts = [f"#{self.number} {status}: {self.title}", self.body[:1000].strip()]
        if include_comments and self.comments:
            for c in self.comments[:3]:
                parts.append(f"  > {c[:400].strip()}")
        return "\n".join(p for p in parts if p)


@dataclass
class PrEntry:
    number: int
    title: str
    state: str  # OPEN | CLOSED | MERGED
    body: str
    review_comments: list[str]
    files: list[str]
    merged_at: str

    def render(self) -> str:
        status = f"[{self.state}]"
        parts = [f"PR #{self.number} {status}: {self.title}"]
        if self.body.strip():
            parts.append(self.body[:800].strip())
        if self.files:
            parts.append(f"  Files: {', '.join(self.files[:8])}")
        for rc in self.review_comments[:2]:
            parts.append(f"  Review: {rc[:300].strip()}")
        return "\n".join(parts)


@dataclass
class GithubContext:
    issues: list[IssueEntry]
    prs: list[PrEntry]
    spec_docs: list[tuple[str, str]]  # [(branch/path, content)]
    adr_docs: list[tuple[str, str]]  # [(path, content)]
    # file path fragment → list of (adr_ref, adr_title) covering that file
    adr_coverage: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


@dataclass
class IntentContext:
    project_docs: list[tuple[str, str]]  # [(rel_path, content)]
    commit_log: list[CommitEntry]  # all fetched commits, newest first
    churn_map: dict[str, int]  # file → commit count
    file_commits: dict[str, list[CommitEntry]]  # file → commits touching it
    github: Optional[GithubContext] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def gather(root: Path, max_commits: int = 400, github: bool = True) -> IntentContext:
    """
    Gather all available stated-intent context for a project.
    Single entry point — call once at the start of intent analysis.
    """
    docs = _gather_docs(root)
    commits, file_commits = _gather_commits(root, max_commits)
    churn = {f: len(cs) for f, cs in file_commits.items()}
    gh_ctx = gather_github(root) if github else None
    return IntentContext(
        project_docs=docs,
        commit_log=commits,
        churn_map=churn,
        file_commits=file_commits,
        github=gh_ctx,
    )


def gather_github(
    root: Path, max_issues: int = 200, max_prs: int = 100
) -> Optional[GithubContext]:
    """Fetch GitHub issues, PRs, spec branch ADRs via gh CLI. Returns None if gh unavailable."""
    issues = _fetch_issues(root, max_issues)
    prs = _fetch_prs(root, max_prs)
    spec_docs, adr_docs = _fetch_spec_branches(root)
    if not (issues or prs or spec_docs or adr_docs):
        return None
    coverage = _build_adr_coverage(adr_docs)
    return GithubContext(
        issues=issues,
        prs=prs,
        spec_docs=spec_docs,
        adr_docs=adr_docs,
        adr_coverage=coverage,
    )


def get_file_adr_coverage(ctx: GithubContext, file_path: str) -> list[tuple[str, str]]:
    """Return list of (adr_ref, adr_title) whose Evidence lines cite this file.

    Uses the longest matching path fragment to avoid false positives when two
    files with the same basename exist (e.g. evaluations/engine/registry.py vs
    tfc/temporal/common/registry.py — only the former has ADR coverage).
    """
    results: list[tuple[str, str]] = []
    parts = Path(file_path).parts
    # Try suffixes from longest to shortest, stopping at the first match.
    # Require at least 2 components (dir/file.py) to avoid basename collisions
    # when multiple files share the same name (e.g. two different registry.py).
    for i in range(max(0, len(parts) - 6), len(parts) - 1):
        fragment = "/".join(parts[i:])
        if fragment in ctx.adr_coverage:
            for entry in ctx.adr_coverage[fragment]:
                if entry not in results:
                    results.append(entry)
            return results  # stop at the most-specific match
    return results


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

    # --- GitHub context ---
    if ctx.github:
        gh_block = render_github_context(ctx.github, char_budget=min(5_000, remaining))
        if gh_block.strip():
            sections.append(gh_block)

    return "\n\n---\n\n".join(sections)


def render_file_context(
    ctx: IntentContext, file_path: str, char_budget: int = 2_500
) -> str:
    """
    Render file-specific intent context for the understand LLM prompt.
    Returns ADR coverage (explicit intent), then commit bodies (the WHY).
    Files with no ADR coverage are flagged so the LLM knows intent is inferred.
    """
    # Normalise path — try both absolute-relative and basename matches
    commits = ctx.file_commits.get(file_path) or ctx.file_commits.get(
        Path(file_path).name, []
    )
    churn = ctx.churn_map.get(file_path, 0)

    parts: list[str] = []

    # --- ADR coverage signal (explicit intent vs inferred) ---
    if ctx.github:
        adr_hits = get_file_adr_coverage(ctx.github, file_path)
        if adr_hits:
            adr_lines = [f"- [{title}]({ref})" for ref, title in adr_hits]
            parts.append(
                "**ADR coverage (explicit architectural intent):**\n"
                + "\n".join(adr_lines)
            )
        else:
            parts.append(
                "**No ADR coverage** — invariants for this file are inferred "
                "from commit history and docstrings only; treat with lower confidence."
            )

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
      1. Commit metadata + bodies (the WHY) — across ALL refs
      2. Per-commit file lists (for churn + file-level enrichment)
    Returns (all_commits, file→commits_map).
    """
    commits = _fetch_commit_metadata(root, max_commits)
    if not commits:
        return [], {}

    sha_to_commit = {c.sha: c for c in commits}
    file_commits: dict[str, list[CommitEntry]] = {}

    # Fetch file lists across all refs — one call, parse into sha→files
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--all",
                f"-{max_commits}",
                "--name-only",
                "--format=__SHA__%H",
            ],
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
    """Fetch sha, author, date, subject, body for recent commits across all refs."""
    # Use \x00 as record separator, \x1f as field separator within header
    fmt = "%x00%H%x1f%an%x1f%ad%x1f%s%x1f%b"
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--all",
                f"-{max_commits}",
                f"--format={fmt}",
                "--date=short",
            ],
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


# ---------------------------------------------------------------------------
# GitHub enrichment
# ---------------------------------------------------------------------------


_EVIDENCE_RE = re.compile(r"`([^`]*\.py[^`]*)`", re.IGNORECASE)
_ADR_TITLE_RE = re.compile(r"^#\s+ADR\s+\d+\s*[—\-–]\s*(.+)", re.MULTILINE)


def _build_adr_coverage(
    adr_docs: list[tuple[str, str]],
) -> dict[str, list[tuple[str, str]]]:
    """
    Parse Evidence lines in ADR docs to build a file → [(adr_ref, title)] map.
    ADRs use "**Evidence**: `path:line`" conventions.
    Coverage is partial — only files Jonathan wrote ADRs for are covered.
    """
    coverage: dict[str, list[tuple[str, str]]] = {}
    for ref_key, content in adr_docs:
        title_m = _ADR_TITLE_RE.search(content)
        title = title_m.group(1).strip() if title_m else Path(ref_key).stem
        for match in _EVIDENCE_RE.finditer(content):
            raw = match.group(1).strip()
            # strip line numbers (e.g. "runner.py:71-74" → "runner.py")
            path_part = raw.split(":")[0].strip()
            if not path_part.endswith(".py"):
                continue
            # index by progressively shorter suffixes for flexible lookup
            parts = Path(path_part).parts
            for i in range(len(parts)):
                fragment = "/".join(parts[i:])
                coverage.setdefault(fragment, [])
                entry = (ref_key, title)
                if entry not in coverage[fragment]:
                    coverage[fragment].append(entry)
    return coverage


def _gh_available(root: Path) -> bool:
    """Return True if `gh` CLI is installed and the repo has a GitHub remote."""
    try:
        r = subprocess.run(
            ["gh", "repo", "view", "--json", "name"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _fetch_issues(root: Path, limit: int) -> list[IssueEntry]:
    """Fetch issues via gh CLI (all states, including comments)."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "all",
                "--limit",
                str(limit),
                "--json",
                "number,title,state,body,labels,comments,closedAt",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env={**__import__("os").environ, "GITHUB_TOKEN": _gh_token()},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    entries: list[IssueEntry] = []
    for item in raw:
        labels = [lb.get("name", "") for lb in item.get("labels", [])]
        comments = [c.get("body", "") for c in item.get("comments", [])]
        entries.append(
            IssueEntry(
                number=item["number"],
                title=item.get("title", ""),
                state=item.get("state", "").upper(),
                labels=labels,
                body=item.get("body", "") or "",
                comments=comments,
                closed_at=item.get("closedAt", "") or "",
            )
        )
    return entries


def _fetch_prs(root: Path, limit: int) -> list[PrEntry]:
    """Fetch PRs via gh CLI including review comments and changed files."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "all",
                "--limit",
                str(limit),
                "--json",
                "number,title,state,body,reviews,files,mergedAt,closedAt",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env={**__import__("os").environ, "GITHUB_TOKEN": _gh_token()},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    entries: list[PrEntry] = []
    for item in raw:
        review_bodies = [
            r.get("body", "")
            for r in item.get("reviews", [])
            if r.get("body", "").strip()
        ]
        files = [f.get("path", "") for f in item.get("files", [])]
        entries.append(
            PrEntry(
                number=item["number"],
                title=item.get("title", ""),
                state=item.get("state", "").upper(),
                body=item.get("body", "") or "",
                review_comments=review_bodies,
                files=files,
                merged_at=item.get("mergedAt", "") or "",
            )
        )
    return entries


def _fetch_spec_branches(
    root: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Look for ADR / spec documents in remote branches whose names suggest
    architecture, spec, or docs work (e.g. fork/docs/*, adr/*, spec/*).
    Returns (spec_docs, adr_docs) as lists of (ref:path, content).
    """
    spec_docs: list[tuple[str, str]] = []
    adr_docs: list[tuple[str, str]] = []

    try:
        result = subprocess.run(
            ["git", "branch", "-r"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        branches = [b.strip() for b in result.stdout.splitlines() if b.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return spec_docs, adr_docs

    _SPEC_PATTERNS = re.compile(
        r"(fork/docs|adr|spec|design|rfc|architecture|decision)", re.IGNORECASE
    )
    candidate_branches = [b for b in branches if _SPEC_PATTERNS.search(b)]

    for branch in candidate_branches[:10]:  # cap to avoid runaway
        try:
            ls = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", branch],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            for filepath in ls.stdout.splitlines():
                filepath = filepath.strip()
                if not filepath:
                    continue
                ext = Path(filepath).suffix.lower()
                if ext not in _DOC_EXTENSIONS:
                    continue
                try:
                    show = subprocess.run(
                        ["git", "show", f"{branch}:{filepath}"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    content = show.stdout[:_MAX_DOC_CHARS]
                    if not content.strip():
                        continue
                    ref_key = f"{branch}:{filepath}"
                    stem = Path(filepath).stem.upper()
                    parts_upper = [p.upper() for p in Path(filepath).parts]
                    is_adr = (
                        "ADR" in stem
                        or "DECISION" in stem
                        or "ARCHITECTURE" in stem
                        or any(p in ("ADR", "ADRS", "DECISIONS") for p in parts_upper)
                        or "SPEC" in stem
                    )
                    if is_adr:
                        adr_docs.append((ref_key, content))
                    else:
                        spec_docs.append((ref_key, content))
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    continue
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return spec_docs, adr_docs


def _gh_token() -> str:
    """Retrieve GitHub token from gh auth — never via --token flag."""
    try:
        r = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def render_github_context(ctx: GithubContext, char_budget: int = 5_000) -> str:
    """Render GitHub issues, PRs, and spec/ADR docs for the LLM intent prompt."""
    sections: list[str] = []
    remaining = char_budget

    # --- ADR docs (highest signal: architectural decisions) ---
    if ctx.adr_docs:
        adr_parts: list[str] = []
        for ref_key, content in ctx.adr_docs[:6]:
            chunk = content[:1_500].strip()
            if chunk:
                adr_parts.append(f"### {ref_key}\n{chunk}")
                remaining -= len(chunk)
            if remaining <= 0:
                break
        if adr_parts:
            sections.append(
                "## Architecture Decision Records (from spec branches)\n\n"
                + "\n\n".join(adr_parts)
            )

    # --- Spec docs ---
    if ctx.spec_docs and remaining > 500:
        spec_parts: list[str] = []
        for ref_key, content in ctx.spec_docs[:4]:
            chunk = content[:1_000].strip()
            if chunk:
                spec_parts.append(f"### {ref_key}\n{chunk}")
                remaining -= len(chunk)
            if remaining <= 0:
                break
        if spec_parts:
            sections.append(
                "## Spec Docs (from branches)\n\n" + "\n\n".join(spec_parts)
            )

    # --- Open issues (known problems the team is aware of) ---
    open_issues = [i for i in ctx.issues if i.state == "OPEN"]
    if open_issues and remaining > 500:
        lines = [i.render(include_comments=False) for i in open_issues[:15]]
        block = "\n\n".join(lines)
        sections.append(
            f"## Open Issues ({len(open_issues)} total)\n\n{block[:remaining]}"
        )
        remaining -= len(block)

    # --- Won't-fix issues (intentional behavior) ---
    wont = [i for i in ctx.issues if i.is_wont_fix()]
    if wont and remaining > 300:
        lines = [f"- #{i.number}: {i.title}" for i in wont]
        sections.append("## Won't-Fix / By-Design Issues\n\n" + "\n".join(lines))

    # --- Recent PRs (attempted changes, contested decisions) ---
    if ctx.prs and remaining > 500:
        pr_lines = [p.render() for p in ctx.prs[:10]]
        block = "\n\n".join(pr_lines)
        sections.append(f"## Recent Pull Requests\n\n{block[:remaining]}")

    return "\n\n---\n\n".join(sections)
