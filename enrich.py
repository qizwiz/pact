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

try:
    import networkx as nx

    _NX_AVAILABLE = True
except ImportError:
    _NX_AVAILABLE = False

try:
    from pydriller import Repository as _PyDrillerRepo

    _PYDRILLER_AVAILABLE = True
except ImportError:
    _PYDRILLER_AVAILABLE = False

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
class IntentCoverage:
    """Structural coverage of a file in the intent graph."""

    level: int  # 0=inferred, 1=commit bodies, 2=issue/PR, 3=ADR
    adrs: list[tuple[str, str]] = field(default_factory=list)  # (ref, title)
    issues: list[tuple[int, str]] = field(default_factory=list)  # (number, title)
    prs: list[tuple[int, str]] = field(default_factory=list)  # (number, title)
    # L1.5: test function descriptions extracted by _extract_test_intent
    test_signals: list[str] = field(default_factory=list)

    @property
    def effective_level(self) -> float:
        """Return 1.5 when test coverage exists but no issue/PR/ADR coverage."""
        if self.level >= 2:
            return float(self.level)
        if self.test_signals and self.level < 2:
            return 1.5
        return float(self.level)

    def label(self) -> str:
        if self.level == 3:
            return "ADR-backed"
        if self.level == 2:
            return "issue/PR-referenced"
        if self.test_signals and self.level < 2:
            return "test-covered"
        if self.level == 1:
            return "commit-body-only"
        return "inferred"

    def render(self) -> str:
        eff = self.effective_level
        level_str = "1.5" if eff == 1.5 else str(int(eff))
        lines: list[str] = [f"**Intent coverage: {self.label()} (L{level_str})**"]
        for ref, title in self.adrs[:4]:
            lines.append(f"  ADR: {title} ({ref.split(':')[0]})")
        for num, title in self.issues[:4]:
            lines.append(f"  Issue #{num}: {title}")
        for num, title in self.prs[:3]:
            lines.append(f"  PR #{num}: {title}")
        for desc in self.test_signals[:8]:
            lines.append(f"  test: {desc}")
        if self.level == 0 and not self.test_signals:
            lines.append(
                "  No stated intent found — invariants are inferred from code structure only."
            )
        return "\n".join(lines)


@dataclass
class GithubContext:
    issues: list[IssueEntry]
    prs: list[PrEntry]
    spec_docs: list[tuple[str, str]]  # [(branch/path, content)]
    adr_docs: list[tuple[str, str]]  # [(path, content)]
    # file path fragment → list of (adr_ref, adr_title) covering that file
    adr_coverage: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # NetworkX intent graph (file/issue/pr/adr nodes + edges), None if nx unavailable
    intent_graph: Optional[object] = field(default=None, repr=False)


@dataclass
class TemporalCoupling:
    """Two files that change together more often than chance — hidden logical dependency."""

    file_a: str
    file_b: str
    co_changes: int
    strength: float  # min(pct_a, pct_b): Tornhill weaker-pairing coupling strength

    def render(self) -> str:
        return (
            f"`{Path(self.file_a).name}` ↔ `{Path(self.file_b).name}` "
            f"({self.co_changes}× together, {self.strength:.0%} coupling)"
        )


@dataclass
class Hotspot:
    """Tornhill hotspot: high churn × high complexity = structural risk."""

    file: str
    churn: int
    complexity: int  # latest observed cyclomatic complexity
    complexity_trend: float  # linear slope over commits (+ = worsening, - = improving)
    score: float  # churn × complexity_latest
    bug_density: float = 0.0  # fraction of commits flagged as bug-introducing (SZZ)
    change_entropy: float = 0.0  # Hassan (ICSE 2009) change entropy

    def render(self) -> str:
        name = "/".join(Path(self.file).parts[-3:])
        if self.complexity_trend > 0.05:
            trend = "▲"
        elif self.complexity_trend < -0.05:
            trend = "▼"
        else:
            trend = "—"
        parts = [
            f"`{name}` — score {self.score:.0f} "
            f"(churn={self.churn}, cx={self.complexity}{trend})"
        ]
        if self.bug_density > 0:
            parts.append(f"bug-density={self.bug_density:.0%}")
        if self.change_entropy > 0:
            parts.append(f"entropy={self.change_entropy:.2f}")
        return " ".join(parts)


@dataclass
class KnowledgeSilo:
    """File with high churn but concentrated ownership — bus-factor / drive-by risk."""

    file: str
    churn: int
    n_authors: int
    authors: list[str]
    top_author_ownership: float = 0.0  # fraction of commits by the dominant author
    minor_contributor_count: int = 0  # authors with < 5% ownership

    def render(self) -> str:
        name = "/".join(Path(self.file).parts[-3:])
        return (
            f"`{name}` — {self.churn} commits, {self.n_authors} author(s), "
            f"top-owner={self.top_author_ownership:.0%}, "
            f"drive-bys={self.minor_contributor_count}"
        )


@dataclass
class TornhillMetrics:
    """Tornhill-style git archaeology metrics for a repository."""

    hotspots: list[Hotspot]
    temporal_coupling: list[TemporalCoupling]
    knowledge_silos: list[KnowledgeSilo]


@dataclass
class IntentContext:
    project_docs: list[tuple[str, str]]  # [(rel_path, content)]
    commit_log: list[CommitEntry]  # all fetched commits, newest first
    churn_map: dict[str, int]  # file → commit count
    file_commits: dict[str, list[CommitEntry]]  # file → commits touching it
    github: Optional[GithubContext] = None
    tornhill: Optional[TornhillMetrics] = None  # hotspots, coupling, silos
    root: Optional[Path] = None  # project root for test-file discovery (L1.5)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def mine_tornhill(
    root: Path,
    max_commits: int = 400,
    coupling_min_pct: float = 0.30,
    coupling_min_cochanges: int = 3,
    silo_max_authors: int = 2,
    silo_min_churn: int = 4,
) -> Optional[TornhillMetrics]:
    """
    Mine Tornhill-style signals from git history via PyDriller.
    Returns None if PyDriller is not installed.

    - Hotspots: churn × complexity (files that are both complex AND frequently changed)
    - Temporal coupling: files that change together (hidden logical dependencies)
    - Knowledge silos: high-churn files with very few authors (orphaned knowledge risk)
    """
    if not _PYDRILLER_AVAILABLE:
        return None
    return _mine_pydriller(
        root,
        max_commits=max_commits,
        coupling_min_pct=coupling_min_pct,
        coupling_min_cochanges=coupling_min_cochanges,
        silo_max_authors=silo_max_authors,
        silo_min_churn=silo_min_churn,
    )


def _is_git_root(root: Path) -> bool:
    """Return True only if root has its own .git directory (is a project root, not a subdir of an unrelated repo)."""
    return (root / ".git").is_dir()


def gather(root: Path, max_commits: int = 400, github: bool = True) -> IntentContext:
    """
    Gather all available stated-intent context for a project.
    Single entry point — call once at the start of intent analysis.
    """
    docs = _gather_docs(root)
    _has_git = _is_git_root(root)
    commits, file_commits = _gather_commits(root, max_commits) if _has_git else ([], {})
    churn = {f: len(cs) for f, cs in file_commits.items()}
    gh_ctx = gather_github(root) if (github and _has_git) else None
    tornhill = mine_tornhill(root, max_commits=max_commits) if _has_git else None
    # Rebuild intent graph with temporal coupling edges now that tornhill is available
    if gh_ctx is not None and tornhill is not None:
        gh_ctx.intent_graph = build_intent_graph(gh_ctx, tornhill=tornhill)
    return IntentContext(
        project_docs=docs,
        commit_log=commits,
        churn_map=churn,
        file_commits=file_commits,
        github=gh_ctx,
        tornhill=tornhill,
        root=root,
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
    coverage = _build_adr_coverage(adr_docs, root=root)
    gh_ctx = GithubContext(
        issues=issues,
        prs=prs,
        spec_docs=spec_docs,
        adr_docs=adr_docs,
        adr_coverage=coverage,
    )
    gh_ctx.intent_graph = build_intent_graph(gh_ctx)  # tornhill added after gather()
    return gh_ctx


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

    # --- Tornhill signals ---
    if ctx.tornhill and remaining > 500:
        t = ctx.tornhill
        parts: list[str] = []
        if t.hotspots:
            hs_lines = [h.render() for h in t.hotspots[:8]]
            parts.append(
                "### Hotspots (churn × complexity — structural risk)\n\n"
                + "\n".join(f"- {line}" for line in hs_lines)
            )
        if t.temporal_coupling:
            tc_lines = [tc.render() for tc in t.temporal_coupling[:10]]
            parts.append(
                "### Temporal coupling (hidden logical dependencies)\n\n"
                + "\n".join(f"- {line}" for line in tc_lines)
            )
        if t.knowledge_silos:
            silo_lines = [s.render() for s in t.knowledge_silos[:6]]
            parts.append(
                "### Knowledge silos (orphaned knowledge risk)\n\n"
                + "\n".join(f"- {line}" for line in silo_lines)
            )
        if parts:
            sections.append("## Structural Risk (Tornhill)\n\n" + "\n\n".join(parts))

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

    # --- Structural intent coverage (graph-derived, works without ADRs) ---
    if ctx.github:
        coverage = get_file_intent_coverage(ctx.github, file_path, root=ctx.root)
        # Upgrade to L1 if commit bodies exist
        if coverage.level == 0 and churn > 0:
            coverage.level = 1
        parts.append(coverage.render())

    # --- Tornhill signals for this file ---
    if ctx.tornhill:
        basename = Path(file_path).name
        file_parts = Path(file_path).parts

        # Hotspot?
        for h in ctx.tornhill.hotspots[:20]:
            if Path(h.file).name == basename or any(
                h.file.endswith("/".join(file_parts[i:]))
                for i in range(max(0, len(file_parts) - 3), len(file_parts) - 1)
            ):
                if h.complexity_trend > 0.05:
                    trend_str = "▲ getting worse"
                elif h.complexity_trend < -0.05:
                    trend_str = "▼ improving"
                else:
                    trend_str = "— stable"
                extra = ""
                if h.bug_density > 0:
                    extra += f", bug-density={h.bug_density:.0%}"
                if h.change_entropy > 0:
                    extra += f", entropy={h.change_entropy:.2f}"
                parts.append(
                    f"**Hotspot score: {h.score:.0f}** (churn={h.churn}, "
                    f"cx={h.complexity} {trend_str}{extra}) — high structural risk"
                )
                break

        # Temporal coupling partners?
        coupled: list[TemporalCoupling] = []
        for tc in ctx.tornhill.temporal_coupling:
            a_match = Path(tc.file_a).name == basename
            b_match = Path(tc.file_b).name == basename
            if a_match or b_match:
                coupled.append(tc)
        if coupled:
            coupled_lines = [tc.render() for tc in coupled[:5]]
            parts.append(
                "**Temporally coupled** (changes together with — hidden logical dependencies):\n"
                + "\n".join(f"- {line}" for line in coupled_lines)
            )

        # Knowledge silo?
        for s in ctx.tornhill.knowledge_silos:
            if Path(s.file).name == basename:
                parts.append(
                    f"**Knowledge silo**: {s.n_authors} author(s) across {s.churn} commits "
                    f"(top-owner={s.top_author_ownership:.0%}, "
                    f"drive-bys={s.minor_contributor_count}) — "
                    "changes here carry orphaned-knowledge risk"
                )
                break

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


def _mine_pydriller(
    root: Path,
    max_commits: int,
    coupling_min_pct: float,
    coupling_min_cochanges: int,
    silo_max_authors: int,
    silo_min_churn: int,
) -> TornhillMetrics:
    """PyDriller-based git archaeology — hotspots, temporal coupling, knowledge silos."""
    import math
    from collections import Counter, defaultdict
    from itertools import combinations

    churn: dict[str, int] = defaultdict(int)
    # complexity_series[path] = list of (commit_index, complexity) in traversal order
    complexity_series: dict[str, list[tuple[int, int]]] = defaultdict(list)
    co_changes: Counter = Counter()
    authors: dict[str, set] = defaultdict(set)
    # author_commits[path][email] = count of commits by that author touching path
    author_commits: dict[str, Counter] = defaultdict(Counter)
    # commit_file_counts[commit_hash] = number of py files in that commit (for entropy)
    commit_file_counts: dict[str, int] = {}
    # file_commit_hashes[path] = list of commit hashes in order (for entropy sum)
    file_commit_hashes: dict[str, list[str]] = defaultdict(list)

    commit_index = 0
    all_commits_ordered: list[object] = []  # store for SZZ pass

    try:
        _repo = _PyDrillerRepo(str(root), num_workers=4)
        _all = _repo.traverse_commits()
        _commits_iter = []
        for _c in _all:
            _commits_iter.append(_c)
            if len(_commits_iter) >= max_commits:
                break
    except Exception:
        # Not a git repository (e.g. installed package directory) — return empty metrics.
        return TornhillMetrics(hotspots=[], temporal_coupling=[], knowledge_silos=[])

    for commit in _commits_iter:
        try:
            _mod_files = commit.modified_files
        except Exception:
            # Shallow clones or corrupt commits may fail to produce diffs; skip.
            continue
        py_files = [f for f in _mod_files if f.new_path and f.new_path.endswith(".py")]
        paths = [f.new_path for f in py_files]
        commit_file_counts[commit.hash] = len(paths)
        all_commits_ordered.append(commit)
        for f in py_files:
            p = f.new_path
            churn[p] += 1
            authors[p].add(commit.author.email)
            author_commits[p][commit.author.email] += 1
            file_commit_hashes[p].append(commit.hash)
            if f.complexity is not None:
                complexity_series[p].append((commit_index, f.complexity))
        for a, b in combinations(sorted(paths), 2):
            co_changes[(a, b)] += 1
        commit_index += 1

    # Hassan (ICSE 2009) change entropy per file
    # entropy(f) = -sum_c( p_c(f) * log2(p_c(f)) )
    # where p_c(f) = 1 / |files_in_commit_c| for each commit c touching f
    change_entropy: dict[str, float] = {}
    for path, hashes in file_commit_hashes.items():
        h = 0.0
        for commit_hash in hashes:
            n = commit_file_counts.get(commit_hash, 1)
            p = 1.0 / max(1, n)
            h -= p * math.log2(p) if p > 0 else 0.0
        change_entropy[path] = round(h, 4)

    # Hotspots — use latest complexity + linear trend
    hotspots: list[Hotspot] = []
    for f, series in complexity_series.items():
        if not series:
            continue
        # series is in traversal order; latest = last element
        cx_latest = series[-1][1]
        if cx_latest <= 0:
            continue
        # Linear trend: (last - first) / max(1, n-1)
        cx_first = series[0][1]
        n_cx = len(series)
        trend = round((cx_latest - cx_first) / max(1, n_cx - 1), 2)
        ch = churn[f]
        hotspots.append(
            Hotspot(
                file=f,
                churn=ch,
                complexity=cx_latest,
                complexity_trend=trend,
                score=float(ch * cx_latest),
                change_entropy=change_entropy.get(f, 0.0),
            )
        )
    hotspots.sort(key=lambda h: -h.score)

    # Temporal coupling — use min (Tornhill: weaker pairing avoids overestimation)
    coupling: list[TemporalCoupling] = []
    for (a, b), cnt in co_changes.items():
        if cnt < coupling_min_cochanges:
            continue
        pct_a = cnt / churn[a] if churn[a] else 0.0
        pct_b = cnt / churn[b] if churn[b] else 0.0
        strength = min(pct_a, pct_b)
        if strength >= coupling_min_pct:
            coupling.append(
                TemporalCoupling(file_a=a, file_b=b, co_changes=cnt, strength=strength)
            )
    coupling.sort(key=lambda c: -c.strength)

    # Knowledge silos — Bird et al. (ESEC/FSE 2011) ownership-fraction model
    silos: list[KnowledgeSilo] = []
    for f, auth_set in authors.items():
        total = churn[f]
        if total < silo_min_churn:
            continue
        ac = author_commits[f]
        ownership = {email: ac[email] / total for email in ac}
        top_ownership = max(ownership.values()) if ownership else 0.0
        minor_count = sum(1 for v in ownership.values() if v < 0.05)
        is_silo = (top_ownership > 0.80) or (minor_count >= 3 and total >= 5)
        if is_silo:
            silos.append(
                KnowledgeSilo(
                    file=f,
                    churn=total,
                    n_authors=len(auth_set),
                    authors=list(auth_set),
                    top_author_ownership=round(top_ownership, 4),
                    minor_contributor_count=minor_count,
                )
            )
    silos.sort(key=lambda s: (-s.churn, s.n_authors))

    # SZZ bug-introducing density — wrap in try/except (git blame can fail/timeout)
    try:
        from pydriller import Git as _PyDrillerGit

        git_obj = _PyDrillerGit(str(root))
        # bug_introducing_commits[path] = set of commit hashes flagged as introducing
        bug_introducing: dict[str, set[str]] = defaultdict(set)
        for commit in all_commits_ordered:
            if not _FIX_RE.search(getattr(commit, "msg", "") or ""):
                continue
            try:
                introducing = git_obj.get_commits_last_modified_lines(commit)
                # introducing is dict[str, set[str]] mapping file_path → set of hashes
                for path, intro_hashes in introducing.items():
                    if path.endswith(".py"):
                        bug_introducing[path].update(intro_hashes)
            except Exception:  # noqa: BLE001
                pass
        # Attach bug_density to hotspots
        hotspot_map = {h.file: h for h in hotspots}
        for path, intro_set in bug_introducing.items():
            if path in hotspot_map:
                total = churn[path]
                hotspot_map[path].bug_density = round(len(intro_set) / max(1, total), 4)
    except Exception:  # noqa: BLE001
        pass

    return TornhillMetrics(
        hotspots=hotspots, temporal_coupling=coupling, knowledge_silos=silos
    )


_EVIDENCE_RE = re.compile(r"`([^`]*\.py[^`]*)`", re.IGNORECASE)
# Matches "# ADR N — title" headings
_ADR_TITLE_RE = re.compile(r"^#\s+ADR\s+\d+\s*[—\-–]\s*(.+)", re.MULTILINE)
# Matches YAML frontmatter title: "..." (used when there's no # ADR heading)
_ADR_FM_TITLE_RE = re.compile(r'^title:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)
# CamelCase class/function names cited in ADR text (e.g. CanManageTargetUser)
_SYMBOL_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]{3,})\b")


_FILE_REF_RE = re.compile(r"\b([\w/.-]+\.py)(?::\d+)?", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def build_intent_graph(
    ctx: GithubContext, tornhill: Optional[TornhillMetrics] = None
) -> "object | None":
    """
    Build a NetworkX DiGraph of the intent layer.

    Nodes:
      file:<fragment>  — source file
      issue:<n>        — GitHub issue
      pr:<n>           — pull request
      adr:<ref>        — architectural decision record

    Edges (directed, from source of intent to subject):
      adr → file    COVERS
      issue → file  MENTIONS
      pr → file     MODIFIES  (from pr.files list)
      pr → issue    REFERENCES (from #123 in pr body)
      issue → issue REFERENCES
    """
    if not _NX_AVAILABLE:
        return None

    G = nx.DiGraph()

    # --- ADR → file edges ---
    for ref_key, content in ctx.adr_docs:
        adr_node = f"adr:{ref_key}"
        G.add_node(adr_node, kind="adr", ref=ref_key)
        for match in _EVIDENCE_RE.finditer(content):
            raw = match.group(1).strip().split(":")[0].strip()
            if raw.endswith(".py"):
                file_node = f"file:{raw}"
                G.add_node(file_node, kind="file")
                G.add_edge(adr_node, file_node, rel="COVERS")

    # --- PR → file edges (from pr.files — already exact paths) ---
    for pr in ctx.prs:
        pr_node = f"pr:{pr.number}"
        G.add_node(pr_node, kind="pr", number=pr.number, title=pr.title, state=pr.state)
        for f in pr.files:
            if f.endswith(".py"):
                file_node = f"file:{f}"
                G.add_node(file_node, kind="file")
                G.add_edge(pr_node, file_node, rel="MODIFIES")
        # PR → issue references
        for m in _ISSUE_REF_RE.finditer(pr.body or ""):
            G.add_edge(pr_node, f"issue:{m.group(1)}", rel="REFERENCES")

    # --- Temporal coupling edges (from Tornhill mining) ---
    # These are hidden logical dependencies not visible in import graphs
    if tornhill:
        for tc in tornhill.temporal_coupling:
            for path in (tc.file_a, tc.file_b):
                G.add_node(f"file:{path}", kind="file")
            G.add_edge(
                f"file:{tc.file_a}",
                f"file:{tc.file_b}",
                rel="COUPLED_WITH",
                strength=tc.strength,
                co_changes=tc.co_changes,
            )
            G.add_edge(
                f"file:{tc.file_b}",
                f"file:{tc.file_a}",
                rel="COUPLED_WITH",
                strength=tc.strength,
                co_changes=tc.co_changes,
            )

    # --- Issue → file edges (from file mentions in body) ---
    for issue in ctx.issues:
        issue_node = f"issue:{issue.number}"
        G.add_node(
            issue_node,
            kind="issue",
            number=issue.number,
            title=issue.title,
            state=issue.state,
        )
        text = (issue.body or "") + " ".join(issue.comments or [])
        for m in _FILE_REF_RE.finditer(text):
            frag = m.group(1)
            file_node = f"file:{frag}"
            G.add_node(file_node, kind="file")
            G.add_edge(issue_node, file_node, rel="MENTIONS")

    return G


def get_file_intent_coverage(
    ctx: GithubContext,
    file_path: str,
    root: Optional[Path] = None,
) -> IntentCoverage:
    """Return the structural intent coverage for a file.

    Uses the NetworkX intent graph when available; falls back to ADR lookup only.
    Works for any project — ADRs are just one source.

    When *root* is provided, also scans for test files to add L1.5 coverage signals.
    """
    # --- ADR coverage (L3) ---
    adrs = get_file_adr_coverage(ctx, file_path)

    issues_found: list[tuple[int, str]] = []
    prs_found: list[tuple[int, str]] = []

    if _NX_AVAILABLE and ctx.intent_graph is not None:
        G = ctx.intent_graph
        # Find file nodes that correspond to this path (suffix matching)
        file_parts = Path(file_path).parts
        candidates = {
            "/".join(file_parts[i:])
            for i in range(max(0, len(file_parts) - 5), len(file_parts))
        }
        # also try just the filename fragment as stored in PR files lists
        # (PR files are typically repo-relative paths like "futureagi/evaluations/engine/runner.py")
        basename = Path(file_path).name
        candidates.add(basename)

        matched_file_nodes: set[str] = set()
        for node, data in G.nodes(data=True):
            if data.get("kind") != "file":
                continue
            frag = node[len("file:") :]
            if frag in candidates or any(
                frag.endswith(c) for c in candidates if len(c) > len(basename)
            ):
                matched_file_nodes.add(node)

        # Walk predecessors: what intent nodes point at this file?
        for file_node in matched_file_nodes:
            for pred in G.predecessors(file_node):
                data = G.nodes[pred]
                if data.get("kind") == "issue":
                    entry = (data["number"], data.get("title", ""))
                    if entry not in issues_found:
                        issues_found.append(entry)
                elif data.get("kind") == "pr":
                    entry = (data["number"], data.get("title", ""))
                    if entry not in prs_found:
                        prs_found.append(entry)

    # Determine level
    if adrs:
        level = 3
    elif issues_found or prs_found:
        level = 2
    else:
        level = 0  # caller sets L1 if commit bodies exist (enrich knows churn)

    # --- L1.5: test-name intent signals ---
    test_sigs: list[str] = []
    if root is not None and level < 2:
        try:
            from .intent import (
                _extract_test_intent,
                _find_test_files,
                _match_tests_for_module,
            )

            test_files = _find_test_files(root)
            all_signals = _extract_test_intent(test_files)
            matched = _match_tests_for_module(Path(file_path), all_signals)
            test_sigs = [s["description"] for s in matched]
        except Exception:
            pass

    return IntentCoverage(
        level=level,
        adrs=adrs,
        issues=issues_found,
        prs=prs_found,
        test_signals=test_sigs,
    )


def _build_adr_coverage(
    adr_docs: list[tuple[str, str]],
    root: Optional[Path] = None,
) -> dict[str, list[tuple[str, str]]]:
    """
    Parse Evidence lines and symbol names in ADR docs to build a
    file → [(adr_ref, title)] map.

    Two strategies:
    1. Backtick-quoted .py paths in Evidence sections (primary)
    2. CamelCase class/function names in ADR title + decision text →
       resolved to files via `git grep -l` (catches cases where the ADR
       describes a class but doesn't cite its implementation file)
    """
    coverage: dict[str, list[tuple[str, str]]] = {}

    def _add(path_part: str, ref_key: str, title: str) -> None:
        parts = Path(path_part).parts
        for i in range(len(parts)):
            fragment = "/".join(parts[i:])
            coverage.setdefault(fragment, [])
            entry = (ref_key, title)
            if entry not in coverage[fragment]:
                coverage[fragment].append(entry)

    for ref_key, content in adr_docs:
        title_m = _ADR_TITLE_RE.search(content) or _ADR_FM_TITLE_RE.search(content)
        title = title_m.group(1).strip() if title_m else Path(ref_key).stem

        # Strategy 1: backtick-quoted .py paths
        for match in _EVIDENCE_RE.finditer(content):
            raw = match.group(1).strip().split(":")[0].strip()
            if raw.endswith(".py"):
                _add(raw, ref_key, title)

        # Strategy 2: CamelCase symbols → git grep to find defining files
        if root is not None:
            symbols = {
                m.group(1)
                for m in _SYMBOL_RE.finditer(title_m.group(1) if title_m else "")
            }
            for sym in list(symbols)[:5]:  # cap to avoid runaway
                try:
                    r = subprocess.run(
                        ["git", "grep", "-l", f"class {sym}\\|def {sym}"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    for line in r.stdout.splitlines():
                        line = line.strip()
                        if line.endswith(".py"):
                            _add(line, ref_key, title)
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass

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
