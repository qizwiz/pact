"""adrs.py — prospective ADR drafting from structural evidence.

Signal priority:
  1. Cut vertices (NetworkX articulation points) — load-bearing joints
  2. Tornhill hotspots (churn × complexity × bug density) — change-risk files
  3. Z3 / intent violations (from violations JSON) — formally proven gaps

For each signal cluster the LLM writes one ADR in the standard docs/adr/ format.
"""

from __future__ import annotations

import json
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Evidence dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FunctionTopology:
    name: str
    callers: list[str]  # functions that call this one
    callees: list[str]  # functions this one calls
    betweenness: float = 0.0


@dataclass
class CutVertexEvidence:
    file: str
    functions: list[str]
    topology: list[FunctionTopology] = field(default_factory=list)
    betweenness_max: float = 0.0
    has_contract: bool = False
    contract: str = ""
    violations: list[str] = field(default_factory=list)


@dataclass
class HotspotEvidence:
    file: str
    churn: int
    complexity: float
    score: float
    bug_density: float = 0.0
    change_entropy: float = 0.0
    top_author_ownership: float = 0.0


@dataclass
class ViolationEvidence:
    file: str
    severity: str
    invariant: str
    mode: str
    z3_counterexample: str = ""


# ---------------------------------------------------------------------------
# ADR template skeleton (filled in by LLM)
# ---------------------------------------------------------------------------

_ADR_SYSTEM = """\
You are a software architect writing Architecture Decision Records (ADRs).
ADRs are factual, concise, and decision-focused — not essays.
Write in the style: Context (why the situation exists), Decision (what we decide),
Rationale (why this decision), Consequences (what changes, what stays the same).
Use markdown. No fluff, no preamble. Start directly with the `# ADR-NNN:` heading.
"""

_ADR_PROMPT = """\
Draft an ADR for the following structural finding.

## Finding type: {kind}

{evidence_block}

## Instructions
- ADR number: {n}
- Status: Proposed
- Date: {date}
- Title: derive a short, specific title from the evidence (e.g. "Isolate ...")
- Context: explain WHY this structural finding matters (use the numbers)
- Decision: state a concrete architectural decision that addresses the risk
- Rationale: cite the structural evidence (cut vertex, churn score, etc.)
- Consequences: list 2-4 concrete consequences (what gets better, what constraint is added)

Output ONLY the ADR markdown, starting with `# ADR-{n}:`.
"""


def _next_adr_number(adr_dir: Path) -> int:
    existing = list(adr_dir.glob("ADR-*.md"))
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("-")[1]))
        except (IndexError, ValueError):
            pass
    return max(nums, default=0) + 1


def _slug(title: str) -> str:
    """Convert ADR title to filename-safe slug."""
    import re

    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:60]


def _call_llm(prompt: str, model: str, key: str) -> str:
    from .llm import make_client

    client = make_client(key)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_ADR_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.content:
        raise RuntimeError("LLM returned empty response")
    return response.content[0].text.strip()


def _write_adr(adr_dir: Path, n: int, markdown: str) -> Path:
    """Write ADR file and return the path. Derives slug from H1 title line."""
    import re

    # Extract title from first # heading for the filename slug
    m = re.search(r"^#\s+ADR-\d+[:\s]+(.+)$", markdown, re.MULTILINE)
    title_slug = _slug(m.group(1)) if m else f"adr-{n:03d}"
    fname = f"ADR-{n:03d}-{title_slug}.md"
    path = adr_dir / fname
    path.write_text(markdown + "\n")
    return path


# ---------------------------------------------------------------------------
# Evidence collectors
# ---------------------------------------------------------------------------


def _collect_cut_vertices(
    root: Path,
    intent_json: Optional[Path],
    verbose: bool,
) -> list[CutVertexEvidence]:
    """Run extract_from_codebase + cut_vertex_files and return evidence list."""
    try:
        from .extractor import extract_from_codebase
        from .reduce import cut_vertex_files
        from .enrich import gather as enrich_gather
    except ImportError as exc:
        warnings.warn(f"adrs: cannot import reduce/extractor ({exc})", RuntimeWarning)
        return []

    if verbose:
        print("  [adrs] extracting call graph…")
    _, functions, call_sites = extract_from_codebase(root)

    coupling: list = []
    try:
        ctx = enrich_gather(root, max_commits=300, github=False)
        if ctx and ctx.tornhill:
            coupling = ctx.tornhill.temporal_coupling
    except Exception:
        pass

    cv_map = cut_vertex_files(functions, call_sites, coupling_edges=coupling or None)
    if not cv_map:
        return []

    # Load intent data for contract annotations
    contracts: dict[str, tuple[str, list[str]]] = {}
    if intent_json and intent_json.exists():
        try:
            data = json.loads(intent_json.read_text())
            for module in data.get("modules", []):
                path = module.get("path", "")
                contract = (module.get("understanding") or {}).get(
                    "behavioral_contract", ""
                )
                gaps = [
                    inv.get("statement", "")
                    for inv in module.get("invariants", [])
                    if inv.get("type") == "intent_gap"
                ]
                if path:
                    contracts[path] = (contract, gaps)
                    contracts[Path(path).name] = (contract, gaps)
        except Exception:
            pass

    # Build call graph and compute betweenness + topology
    betweenness: dict[str, float] = {}
    G = None
    try:
        import networkx as nx

        G = nx.DiGraph()
        for cs in call_sites:
            if cs.caller_name and cs.callee_name:
                G.add_edge(cs.caller_name, cs.callee_name)
        betweenness = nx.betweenness_centrality(G)
    except Exception:
        pass

    def _short(name: str) -> str:
        # Drop module prefix (e.g. "pact.reduce.cut_vertex_files" → "cut_vertex_files")
        return name.split(".")[-1] if "." in name else name

    def _topology(fn: str) -> FunctionTopology:
        if G is None or fn not in G:
            return FunctionTopology(name=_short(fn), callers=[], callees=[])
        callers = [_short(p) for p in G.predecessors(fn)][:5]
        callees = [_short(s) for s in G.successors(fn)][:5]
        return FunctionTopology(
            name=_short(fn),
            callers=callers,
            callees=callees,
            betweenness=betweenness.get(fn, 0.0),
        )

    evidence = []
    for file_path, func_names in cv_map.items():
        contract, gaps = contracts.get(
            file_path, contracts.get(Path(file_path).name, ("", []))
        )
        topo = [_topology(fn) for fn in func_names[:8]]
        bmax = max((t.betweenness for t in topo), default=0.0)
        evidence.append(
            CutVertexEvidence(
                file=file_path,
                functions=func_names,
                topology=topo,
                betweenness_max=bmax,
                has_contract=bool(contract),
                contract=contract,
                violations=gaps,
            )
        )
    # Rank: cut vertices without contracts first, then by betweenness
    evidence.sort(key=lambda e: (int(e.has_contract), -e.betweenness_max))
    return evidence


def _collect_hotspots(root: Path, verbose: bool) -> list[HotspotEvidence]:
    """Return Tornhill hotspot evidence, sorted by risk score."""
    try:
        from .enrich import gather as enrich_gather
    except ImportError:
        return []

    if verbose:
        print("  [adrs] mining git history for hotspots…")
    try:
        ctx = enrich_gather(root, max_commits=500, github=False)
    except Exception as exc:
        warnings.warn(f"adrs: enrich failed ({exc})", RuntimeWarning)
        return []

    if not ctx or not ctx.tornhill:
        return []

    evidence = []
    for h in ctx.tornhill.hotspots:
        evidence.append(
            HotspotEvidence(
                file=h.file,
                churn=h.churn,
                complexity=h.complexity,
                score=h.score,
                bug_density=getattr(h, "bug_density", 0.0) or 0.0,
                change_entropy=getattr(h, "change_entropy", 0.0) or 0.0,
            )
        )

    # Attach silo signals
    silo_files = {s.file: s.top_author_ownership for s in ctx.tornhill.knowledge_silos}
    for e in evidence:
        e.top_author_ownership = silo_files.get(e.file, 0.0)

    evidence.sort(key=lambda e: -e.score)
    return evidence


def _collect_violations(violations_json: Path) -> list[ViolationEvidence]:
    """Parse a violations JSON (intent_pact_self.json format) for ADR candidates."""
    if not violations_json or not violations_json.exists():
        return []
    try:
        data = json.loads(violations_json.read_text())
    except Exception:
        return []

    seen: set[tuple[str, str]] = set()
    evidence = []
    for module in data.get("modules", []):
        path = module.get("path", "")
        for inv in module.get("invariants", []):
            if inv.get("type") != "intent_gap":
                continue
            sev = inv.get("severity", "medium")
            if sev not in ("high", "critical"):
                continue
            stmt = inv.get("statement", "")
            key = (path, stmt[:80])
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                ViolationEvidence(
                    file=path,
                    severity=sev,
                    invariant=stmt,
                    mode=inv.get("id", ""),
                    z3_counterexample=inv.get("z3_counterexample", ""),
                )
            )
    evidence.sort(key=lambda e: (0 if e.severity == "critical" else 1))
    return evidence


# ---------------------------------------------------------------------------
# Evidence → ADR markdown via LLM
# ---------------------------------------------------------------------------


def _cv_evidence_block(ev: CutVertexEvidence) -> str:
    file_short = Path(ev.file).name
    lines = [
        f"**File**: `{file_short}`",
        f"**Behavioral contract**: {'present' if ev.contract else 'MISSING'}",
    ]
    if ev.contract:
        lines.append(f"**Contract summary**: {ev.contract[:250]}")

    lines.append("")
    lines.append(
        "**Cut-vertex functions** — each is an articulation point whose removal "
        "disconnects the call graph. Callers / callees show what breaks:"
    )
    for t in ev.topology:
        b = f"  betweenness={t.betweenness:.4f}" if t.betweenness > 0 else ""
        lines.append(f"  - `{t.name}`{b}")
        if t.callers:
            lines.append(f"      called by: {', '.join(f'`{c}`' for c in t.callers)}")
        if t.callees:
            lines.append(f"      calls:     {', '.join(f'`{c}`' for c in t.callees)}")

    if ev.violations:
        lines.append("")
        lines.append("**Known intent gaps** (from pact intent analysis):")
        for v in ev.violations[:3]:
            lines.append(f"  - {v[:250]}")
    return "\n".join(lines)


def _hotspot_evidence_block(ev: HotspotEvidence) -> str:
    file_short = Path(ev.file).name
    lines = [
        f"**File**: `{file_short}`",
        f"**Churn** (commits): {ev.churn}",
        f"**Cyclomatic complexity**: {ev.complexity}",
        f"**Hotspot score** (churn × complexity): {ev.score:.1f}",
    ]
    if ev.bug_density > 0:
        lines.append(f"**SZZ bug-introducing density**: {ev.bug_density:.3f}")
    if ev.change_entropy > 0:
        lines.append(f"**Hassan change entropy**: {ev.change_entropy:.3f}")
    if ev.top_author_ownership > 0.8:
        lines.append(
            f"**Knowledge silo**: top author owns {ev.top_author_ownership:.0%} of commits"
        )
    return "\n".join(lines)


def _violation_evidence_block(ev: ViolationEvidence) -> str:
    file_short = Path(ev.file).name
    lines = [
        f"**File**: `{file_short}`",
        f"**Severity**: {ev.severity}",
        f"**Invariant violated**: {ev.invariant[:400]}",
    ]
    if ev.mode:
        lines.append(f"**Mode**: `{ev.mode}`")
    if ev.z3_counterexample:
        lines.append(f"**Z3 counterexample**: {ev.z3_counterexample[:300]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def draft_adrs(
    root: Path,
    violations_json: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    verbose: bool = False,
    top_n: int = 10,
) -> list[Path]:
    """Draft prospective ADRs from structural evidence.

    Returns list of written ADR file paths.
    """
    from .llm import resolve_key, resolve_model

    model = resolve_model(model)

    key = resolve_key(api_key)

    adr_dir = out_dir or (root / "docs" / "adr")
    adr_dir.mkdir(parents=True, exist_ok=True)

    date = time.strftime("%Y-%m-%d")

    # --- Collect evidence ---
    intent_json = violations_json or (root / "intent_pact_self.json")
    cv_evidence = _collect_cut_vertices(root, intent_json, verbose)
    hotspot_evidence = _collect_hotspots(root, verbose)
    violation_evidence = _collect_violations(intent_json)

    if verbose:
        print(
            f"  [adrs] evidence: {len(cv_evidence)} cut-vertex files, "
            f"{len(hotspot_evidence)} hotspots, "
            f"{len(violation_evidence)} critical violations"
        )

    # --- Build candidate list (de-dup by file) ---
    drafted_files: set[str] = set()
    written: list[Path] = []

    def _draft_one(kind: str, evidence_block: str) -> Optional[Path]:
        nonlocal drafted_files
        n = _next_adr_number(adr_dir)
        prompt = _ADR_PROMPT.format(
            kind=kind,
            evidence_block=evidence_block,
            n=f"{n:03d}",
            date=date,
        )
        if verbose:
            print(f"  [adrs] drafting ADR-{n:03d} ({kind})…")
        try:
            markdown = _call_llm(prompt, model, key)
            path = _write_adr(adr_dir, n, markdown)
            written.append(path)
            if verbose:
                print(f"  [adrs] wrote {path.name}")
            return path
        except Exception as exc:
            warnings.warn(f"adrs: LLM call failed ({exc})", RuntimeWarning)
            return None

    count = 0

    # Priority 1: cut vertices without contracts
    for ev in cv_evidence:
        if count >= top_n:
            break
        file_key = Path(ev.file).name
        if file_key in drafted_files:
            continue
        drafted_files.add(file_key)
        p = _draft_one(
            "Cut vertex (structural load-bearing joint)", _cv_evidence_block(ev)
        )
        count += 1  # count attempt regardless of LLM success

    # Priority 2: critical violations
    for ev in violation_evidence:
        if count >= top_n:
            break
        file_key = Path(ev.file).name
        if file_key in drafted_files:
            continue
        drafted_files.add(file_key)
        p = _draft_one(
            f"Formally violated contract ({ev.severity})",
            _violation_evidence_block(ev),
        )
        if p:
            count += 1

    # Priority 3: top hotspots not already covered
    for ev in hotspot_evidence:
        if count >= top_n:
            break
        file_key = Path(ev.file).name
        if file_key in drafted_files:
            continue
        # Only draft for files with meaningful signal
        if ev.score < 20 and ev.bug_density < 0.1:
            continue
        drafted_files.add(file_key)
        p = _draft_one(
            "Tornhill hotspot (churn × complexity)", _hotspot_evidence_block(ev)
        )
        if p:
            count += 1

    return written
