# ADR-002: Mermaid PR Comments over SARIF for Violation Reporting

**Status**: Accepted  
**Date**: 2026-05-15  

---

## Context

When pact runs on a pull request, it needs to communicate findings to the
developer. There are two main formats:

**SARIF** (Static Analysis Results Interchange Format) is the GitHub-native
format for code scanning alerts. It integrates with GitHub's Security tab,
shows inline annotations, and is supported by hundreds of tools.

**Mermaid** is a Markdown-native diagram format that renders directly in
GitHub PR comments, issues, and READMEs without any tooling installation.

The question: which format better serves pact's architectural goals?

---

## Decision

**pact outputs Mermaid call graph diagrams as PR comments, not SARIF.**

The `--pr-comment` flag produces a GitHub PR comment body containing:
1. A Mermaid flowchart of the call graph with violation-colored nodes
   (green = clean, yellow = warning, orange = hot, red = fire)
2. A reduction sequence showing the minimal subgraph needed to reach the
   violation
3. Inline fix suggestions (`--suggest`)

The GitHub Actions workflow (`.github/workflows/pr-graph.yml`) posts this
as a comment, deleting and recreating on each push to avoid accumulation.

---

## The core reason

SARIF encodes: `file` + `line` + `col` + `message`. It cannot encode a path.

pact's violations are path properties: "there exists a call chain from
`fetch_user()` to `response.choices[0]` with no None-guard." The bug is
not *at* a line — it is *along* a path. SARIF would report the endpoint,
losing the chain that explains why it matters.

A Mermaid diagram shows exactly the call graph that contains the violation.
The developer sees which functions are involved, which edges cross file
boundaries, and which node is the entry point. This is the information
needed to fix the bug, not just locate it.

---

## Alternatives Considered

**SARIF + GitHub Code Scanning** would get pact into the Security tab and
enable enforcement of "no violations before merge." However:
- It requires uploading a SARIF artifact via `upload-sarif` action
- It needs a GitHub Advanced Security license for private repos
- It communicates `(file, line)` not `(path, constraint)` — the graph is lost
- It cannot render the reduction sequence or annotated call graph

**Both SARIF and Mermaid** (dual output) was considered. Rejected for now:
the maintenance overhead of two output formats is not worth it until there is
concrete demand. The `--graph` flag writes the graph in DOT format, which
third-party tools can consume if needed.

**Inline annotations only** (GitHub annotations via `::error file=...::`)
have a 10-annotation limit per step and lose multi-file path context.

---

## Consequences

- The PR comment is the primary UX surface for pact findings.
- `visualize.py` owns the rendering: `render_mermaid()`, `format_pr_comment()`,
  `render_reduction_sequence()`. Any change to violation presentation goes here.
- The Mermaid diagram is intentionally not a complete-codebase visualization —
  it is diff-scoped (violations in changed files only, via `--diff main`).
  This keeps comments tractable and avoids alerting on pre-existing issues.
- Node coloring uses a 4-level severity scale tied to violation count:
  clean (0) → warn (1-2) → hot (3-5) → fire (>5).
