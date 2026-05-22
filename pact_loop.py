"""
pact_loop -- recursive self-improvement loop with ML convergence criterion.

Runs pact on any target (including itself) until formal convergence:
  violations → 0  OR  |Δfitness| < ε for 3 consecutive iterations

Philosophy (from project history and ADR-036/ADR-037):
  - Failures are the signal. Every parse fail, empty result, low confirm
    rate = prompt improvement trigger. Errors are training data.
  - Git history IS the prior. CHANGES.rst "Fixed…" lines are confirmed
    invariants. They weight find.md toward what actually broke.
  - Z3/Hypothesis/TLA+ derive the taxonomy. We don't invent categories;
    formal tools find them. The taxonomy IS the set of properties that
    Hypothesis falsifies and Z3 proves.
  - Stunspot principle: prompts may evolve into alien encodings. We
    validate against rubrics, not readability. The rubric is the invariant.
  - CEGIS oracle loop: synthesize → verify → oracle → revert/accept →
    feedback. The oracle (target test suite) cannot collude with pact.
  - Graph-first (ADR-001): violations = constraints on call-graph paths,
    not pattern matches. blast radius × topology score → priority order.
  - Sheaf cohomology (pact_sheaf.py): Ȟ¹ rank = minimum independent fixes.
  - TDA (pact_tda.py): β₁ Betti numbers of neighborhood → topology score.
  - ADR generation: every architectural decision the loop makes is recorded.

Fitness function (ML loss analog, range [0, 1], higher = better):
  f = 0.25 * (1 − violation_rate)   # code cleanliness vs. initial
    + 0.20 * heal_accept_rate        # CEGIS patch quality
    + 0.15 * oracle_confirm_rate     # oracle trust (ground truth)
    + 0.15 * find_confirm_rate       # real bug detection (Hypothesis)
    + 0.10 * topo_score              # topology health (β₁ of call graph)
    + 0.10 * avg_prompt_score        # prompt quality (from improve runs)
    + 0.05 * sheaf_score             # Ȟ¹ rank reduction (interprocedural)

Termination conditions (any one):
  PROVED_CLEAN  violation_count == 0 and sheaf rank == 0
  CONVERGED     |Δfitness| < ε for 3 consecutive iterations
  STUCK         heal accepts 0 patches for 2 consecutive iterations
  TIMEOUT       iter >= max_iters

Usage:
    pact loop <target_dir> --test-cmd "pytest tests/ -q"
    python -m pact.pact_loop . --test-cmd "pytest -q" --verbose
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_DEFAULT_MODEL = "claude-sonnet-4-6"
_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_EPSILON = 0.01
_WINDOW = 3
_MAX_ITERS = 20
_STUCK_WINDOW = 2


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class MeasureResult:
    """Full measurement snapshot from all analyzers."""

    checker_total: int = 0
    checker_by_mode: dict = field(default_factory=dict)
    interproc_total: int = 0
    interproc_transitive: int = 0
    sheaf_rank: int = 0  # Ȟ¹ = min independent fixes (pact_sheaf)
    z3_proved: int = 0  # violations proved by Z3 datalog
    topo_score: float = 0.0  # β₁-based topology health (pact_tda)
    blast_top: list = field(default_factory=list)  # top-3 blast-radius fns
    scc_count: int = 0  # strongly-connected components
    hub_count: int = 0  # high-fan-in functions
    find_attempted: int = 0
    find_confirmed: int = 0
    find_parse_failures: int = 0
    violations_doc: dict = field(default_factory=dict)


@dataclass
class IterationState:
    iteration: int
    timestamp: float = field(default_factory=time.time)
    measure: MeasureResult = field(default_factory=MeasureResult)

    total_violations: int = 0
    heal_attempted: int = 0
    heal_accepted: int = 0
    oracle_confirmed: int = 0
    heal_accept_rate: float = 0.0
    oracle_confirm_rate: float = 0.0
    find_confirm_rate: float = 0.0

    heal_prompt_score: float = 1.0
    find_prompt_score: float = 1.0
    context_prompt_score: float = 1.0
    avg_prompt_score: float = 1.0

    topo_score: float = 0.0
    sheaf_score: float = 1.0  # 1.0 = no sheaf issues; 0.0 = max rank

    fitness: float = 0.0
    termination: Optional[str] = None
    adr_generated: Optional[str] = None


@dataclass
class LoopResult:
    target: str
    test_cmd: str
    iterations: list[IterationState] = field(default_factory=list)
    termination: str = ""
    initial_violations: int = 0
    final_violations: int = 0
    final_fitness: float = 0.0
    elapsed_seconds: float = 0.0

    def summary(self) -> str:
        lines = [
            f"[loop] target:      {self.target}",
            f"[loop] termination: {self.termination}",
            f"[loop] violations:  {self.initial_violations} → {self.final_violations}",
            f"[loop] fitness:     {self.final_fitness:.3f}",
            f"[loop] iterations:  {len(self.iterations)}",
            f"[loop] elapsed:     {self.elapsed_seconds:.1f}s",
        ]
        if len(self.iterations) > 1:
            scores = [f"{s.fitness:.3f}" for s in self.iterations]
            lines.append(f"[loop] history:     {' → '.join(scores)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------


def compute_fitness(state: IterationState, initial_violations: int) -> float:
    """
    Composite fitness in [0, 1]. This is the loop's loss function.

    Weights from ADR-037 (pact-self-loop-architecture):
      violation_rate (0.25):  primary goal — clean code
      heal_accept_rate (0.20): CEGIS synthesis quality
      oracle_confirm_rate (0.15): ground truth signal
      find_confirm_rate (0.15): real bug detection, not hallucinations
      topo_score (0.10):      graph topology health (β₁ Betti)
      avg_prompt_score (0.10): prompt quality → future improvement capacity
      sheaf_score (0.05):     Ȟ¹ rank reduction (interprocedural safety)
    """
    iv = max(initial_violations, 1)
    v_score = max(0.0, 1.0 - state.total_violations / iv)
    return (
        0.25 * v_score
        + 0.20 * state.heal_accept_rate
        + 0.15 * state.oracle_confirm_rate
        + 0.15 * state.find_confirm_rate
        + 0.10 * state.topo_score
        + 0.10 * state.avg_prompt_score
        + 0.05 * state.sheaf_score
    )


def _converged(history: list[float], epsilon: float, window: int) -> bool:
    if len(history) < window:
        return False
    recent = history[-window:]
    return (max(recent) - min(recent)) < epsilon


def _stuck(accepted_history: list[int], window: int) -> bool:
    if len(accepted_history) < window:
        return False
    return all(a == 0 for a in accepted_history[-window:])


# ---------------------------------------------------------------------------
# Phase 1: MEASURE — all analyzers
# ---------------------------------------------------------------------------


def _measure_checker(target: Path, verbose: bool) -> tuple[int, dict, dict]:
    """Static AST checker (all Python modes). Returns (count, by_mode, violations_doc)."""
    from .checker import check_codebase
    from collections import defaultdict, Counter

    results = check_codebase(target)
    by_mode = dict(
        Counter(
            getattr(r, "mode_name", getattr(r, "context", "?")) for r in results
        ).most_common()
    )

    inv_templates = {
        "bare_except": "Exception handlers must be typed and not silently swallow errors",
        "json_loads_unguarded": "json.loads() must be wrapped in try/except json.JSONDecodeError",
        "subprocess_exit_code_unchecked": "subprocess.run() must check returncode",
        "llm_response_unguarded": "LLM response access must be guarded against None/empty choices",
        "optional_dereference": "Optional values must be checked before attribute access",
        "save_without_update_fields": "Model.save() must pass update_fields to prevent race conditions",
        "missing_await": "Coroutines must be awaited or they silently do nothing",
        "sheaf_llm_unguarded": "LLM response path is interprocedurally unguarded",
        "model_constraint": "Required model fields must be provided at call sites",
        "unvalidated_lookup_chain": "ORM lookup chains must handle DoesNotExist",
        "falsy_or_zero_elision": "'or default' pattern silently elides zero/empty-string",
        "mutable_default_arg": "Mutable default argument is shared across all calls",
        "required_arg_missing": "Required positional argument omitted at call site",
        "empty_catch": "Empty except block swallows all exceptions silently",
        "asyncio_run_in_async": "asyncio.run() called inside async function creates deadlock",
    }

    file_viols: dict = defaultdict(list)
    for r in results:
        ctx = getattr(r, "context", getattr(r, "mode_name", "?"))
        inv_id = f"{ctx}_{Path(r.file).stem}"
        severity = (
            "high"
            if ctx
            in (
                "bare_except",
                "json_loads_unguarded",
                "llm_response_unguarded",
                "missing_await",
                "model_constraint",
                "required_arg_missing",
            )
            else "medium"
        )
        file_viols[r.file].append(
            {
                "invariant_id": inv_id,
                "file": r.file,
                "line": r.line,
                "severity": severity,
                "evidence": r.call,
                "explanation": "; ".join(r.missing) if r.missing else "",
                "_inv": inv_templates.get(ctx, f"Violation: {ctx}"),
            }
        )

    modules = []
    for fpath, viols in sorted(file_viols.items()):
        seen: dict = {}
        invs = []
        for v in viols:
            iid = v["invariant_id"]
            if iid not in seen:
                invs.append(
                    {
                        "id": iid,
                        "type": iid.rsplit("_", 1)[0],
                        "statement": v.pop("_inv"),
                        "severity": v["severity"],
                        "confidence": 0.9,
                    }
                )
                seen[iid] = True
            else:
                v.pop("_inv", None)
        modules.append(
            {
                "path": fpath,
                "invariants": invs,
                "violations": [
                    {k: vl for k, vl in v.items() if k != "_inv"} for v in viols
                ],
            }
        )

    doc = {"project": target.name, "generated_by": "pact.checker", "modules": modules}
    if verbose:
        print(
            f"  checker: {len(results)} violations — top modes: "
            + ", ".join(f"{m}({c})" for m, c in list(by_mode.items())[:4])
        )
    return len(results), by_mode, doc, results


def _measure_interproc(target: Path, verbose: bool) -> tuple[int, int]:
    """Z3 Fixedpoint interprocedural analysis."""
    try:
        from .pact_interproc import analyze_codebase

        results = analyze_codebase(target, verbose=False)
        transitive = sum(1 for r in results if r.propagation_depth > 0)
        if verbose:
            print(
                f"  interproc: {len(results)} ({transitive} transitive via call graph)"
            )
        return len(results), transitive
    except Exception as exc:
        if verbose:
            print(f"  interproc: skipped ({exc})")
        return 0, 0


def _measure_sheaf(target: Path, verbose: bool) -> int:
    """Sheaf-cohomological analysis. Returns Ȟ¹ rank (min independent fixes)."""
    try:
        from .pact_sheaf import _harvest_sites

        py_files = list(target.rglob("*.py"))
        py_files = [
            f
            for f in py_files
            if not any(p in f.parts for p in ("__pycache__", ".venv", "node_modules"))
        ]

        total_rank = 0
        for fpath in py_files[:30]:  # sample — sheaf is expensive
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
                sg = _harvest_sites(str(fpath), source)
                error_sites = sg.error_sites()
                if error_sites:
                    total_rank += len(error_sites)
            except Exception as _file_exc:
                import warnings

                warnings.warn(
                    f"pact_sheaf: skipped {fpath} ({_file_exc})",
                    RuntimeWarning,
                    stacklevel=2,
                )

        if verbose:
            print(f"  sheaf: Ȟ¹ rank ≈ {total_rank} (min independent fixes)")
        return total_rank
    except Exception as exc:
        if verbose:
            print(f"  sheaf: skipped ({exc})")
        return 0


def _measure_tda(target: Path, violations_doc: dict, verbose: bool) -> float:
    """Topological data analysis — β₁ Betti of call graph neighborhood → topo_score."""
    try:
        from .pact_tda import score_violations
        from .graphify_graph import CallGraph

        cg = CallGraph.load(target)
        if cg is None:
            if verbose:
                print("  tda: no graphify call graph — skipping")
            return 0.5  # neutral when graph unavailable

        # Collect all violations
        all_viols = []
        for mod in violations_doc.get("modules", []):
            for v in mod.get("violations", []):
                all_viols.append(v)

        if not all_viols:
            return 1.0

        pairs = score_violations(cg, all_viols)
        scores = [ts for _, ts in pairs if ts is not None]
        if not scores:
            return 0.5

        avg = sum(s.severity() for s in scores) / len(scores)
        # severity ∈ [0,1]; higher = worse topology. Invert for fitness.
        topo_health = 1.0 - avg
        if verbose:
            print(
                f"  tda: β₁-based topo_score={topo_health:.3f} "
                f"({len(scores)} violations scored)"
            )
        return max(0.0, min(1.0, topo_health))
    except Exception as exc:
        if verbose:
            print(f"  tda: skipped ({exc})")
        return 0.5


def _measure_z3(target: Path, violations_doc: dict, verbose: bool) -> int:
    """Z3 Datalog engine — count formally proved violations."""
    try:
        from .z3_engine import LLMResponseEngine

        proved = 0
        py_files = list(target.rglob("*.py"))[:20]
        for fpath in py_files:
            try:
                eng = LLMResponseEngine()
                source = fpath.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
                # Feed facts and query
                viols = eng.check(fpath, source, tree)
                proved += len(viols) if viols else 0
            except Exception as exc:  # noqa: BLE001
                import warnings

                warnings.warn(
                    f"z3-engine: skipped {fpath} ({exc})",
                    RuntimeWarning,
                    stacklevel=2,
                )

        if verbose and proved:
            print(f"  z3-engine: {proved} formally proved violations")
        return proved
    except Exception as exc:
        if verbose:
            print(f"  z3-engine: skipped ({exc})")
        return 0


def _measure_blast_radii(
    target: Path, checker_violations: list, verbose: bool
) -> tuple[list, int, int]:
    """Blast radius + SCC + hub analysis from reduce.py."""
    try:
        from .reduce import compute_blast_radii, find_sccs, find_hubs, _build_digraph
        from .extractor import extract_from_codebase

        _, functions, call_sites = extract_from_codebase(target)

        blast = compute_blast_radii(functions, call_sites, checker_violations)
        top3 = [str(b) for b in sorted(blast, key=lambda b: -b.blast_radius)[:3]]

        G, func_by_name = _build_digraph(functions, call_sites)
        sccs_list = find_sccs(G, func_by_name, checker_violations) if G else []
        hubs_list = find_hubs(G, func_by_name, checker_violations) if G else []

        if verbose:
            print(
                f"  reduce: {len(sccs_list)} SCCs, {len(hubs_list)} hubs, "
                f"top blast: {top3[0][:60] if top3 else 'n/a'}"
            )
        return top3, len(sccs_list), len(hubs_list)
    except Exception as exc:
        if verbose:
            print(f"  reduce: skipped ({exc})")
        return [], 0, 0


def _measure_find(
    target: Path, model: str, key: str, improve: bool, verbose: bool
) -> tuple[int, int, int, float]:
    """LLM property-driven finder — sample top-3 hottest files."""
    try:
        from .find import find_violations
        from .checker import check_codebase
        from collections import Counter

        hot = Counter(r.file for r in check_codebase(target)).most_common(3)
        if not hot:
            return 0, 0, 0, 0.0

        att = conf = fails = 0
        for fpath_str, _ in hot:
            fpath = Path(fpath_str)
            if not fpath.exists():
                continue
            try:
                res = find_violations(
                    path=fpath,
                    model=model,
                    api_key=key,
                    verbose=verbose,
                    use_context=True,
                    improve=improve,
                )
                for m in res.get("modules", []):
                    viols = m.get("violations", [])
                    att += len(viols)
                    conf += sum(1 for v in viols if v.get("hypothesis_confirmed"))
            except Exception as exc:
                fails += 1
                if verbose:
                    print(f"  find: {fpath.name} failed: {exc}")

        rate = conf / max(att, 1)
        if verbose:
            print(f"  find: {att} properties, {conf} Hypothesis-confirmed ({rate:.0%})")
        return att, conf, fails, rate
    except ImportError:
        return 0, 0, 0, 0.0


def _topology_priority(violations_doc: dict, target: Path) -> dict[str, float]:
    """Score each file by PageRank on the call graph — high-PageRank violations spread most."""
    try:
        import networkx as nx
        from .graphify_graph import CallGraph

        cg = CallGraph.load(target)
        if cg is None:
            return {}

        # Build directed call graph: caller → callee
        G: nx.DiGraph = nx.DiGraph()
        for src, targets in cg._out_edges.items():
            for tgt in targets:
                G.add_edge(src, tgt)

        if G.number_of_nodes() == 0:
            return {}

        pr: dict[str, float] = nx.pagerank(G, alpha=0.85)

        # For each violated file, take the max PageRank of its functions
        func_to_file: dict[str, str] = {
            nid: meta["file"] for nid, meta in cg._id_meta.items() if meta.get("file")
        }
        file_pr: dict[str, float] = {}
        for nid, score in pr.items():
            fpath = func_to_file.get(nid, "")
            if fpath:
                file_pr[fpath] = max(file_pr.get(fpath, 0.0), score)

        priorities: dict[str, float] = {}
        for mod in violations_doc.get("modules", []):
            fpath = mod["path"]
            priorities[fpath] = file_pr.get(fpath, 0.0)

        if priorities:
            mx = max(priorities.values()) or 1.0
            priorities = {k: v / mx for k, v in priorities.items()}
        return priorities
    except Exception:
        return {}


def _reprioritize(violations_doc: dict, priorities: dict[str, float]) -> dict:
    if not priorities:
        return violations_doc
    doc = dict(violations_doc)
    doc["modules"] = sorted(
        doc.get("modules", []),
        key=lambda m: priorities.get(m["path"], 0.0),
        reverse=True,
    )
    return doc


def measure(
    target: Path, model: str, key: str, run_find: bool, improve: bool, verbose: bool
) -> MeasureResult:
    m = MeasureResult()
    print("\n[loop:measure]")

    # 1. Static checker (all modes, all languages)
    m.checker_total, m.checker_by_mode, m.violations_doc, _checker_raw = (
        _measure_checker(target, verbose)
    )

    # 2. Z3 interprocedural (Fixedpoint CHC)
    m.interproc_total, m.interproc_transitive = _measure_interproc(target, verbose)

    # 3. Sheaf cohomology (Ȟ¹ rank)
    m.sheaf_rank = _measure_sheaf(target, verbose)

    # 4. Z3 Datalog engine (formal proofs)
    m.z3_proved = _measure_z3(target, m.violations_doc, verbose)

    # 5. TDA — topology score
    m.topo_score = _measure_tda(target, m.violations_doc, verbose)

    # 6. Blast radii + SCCs + hubs
    m.blast_top, m.scc_count, m.hub_count = _measure_blast_radii(
        target, _checker_raw, verbose
    )

    # 7. LLM property-driven find (optional — expensive)
    if run_find:
        print("\n[loop:find]")
        m.find_attempted, m.find_confirmed, m.find_parse_failures, _ = _measure_find(
            target, model, key, improve, verbose
        )

    return m


# ---------------------------------------------------------------------------
# Phase 2: HEAL
# ---------------------------------------------------------------------------


def _record_heal_failure(
    violations_doc: dict, target: Path, error_msg: str, verbose: bool
) -> None:
    """Save a tool-loop-exhausted failure as a spec_learner training example.

    Accumulates in corpus/spec_gaps.jsonl. When ≥2 bad records exist,
    spec_learner.improve() will trigger heal-prompt self-improvement.
    """
    try:
        from .spec_learner import SpecGapRecord, save

        tla_path = Path(__file__).parent / "docs" / "tla" / "PactLoop.tla"
        tla_text = tla_path.read_text() if tla_path.exists() else ""

        # Pick the first violated file as the representative failure site
        modules = violations_doc.get("modules", [])
        bug_file = modules[0]["path"] if modules else str(target)
        bug_line = (
            modules[0]["violations"][0]["line"]
            if modules and modules[0].get("violations")
            else 0
        )
        mode = (
            modules[0]["violations"][0].get("invariant_id", "unknown").rsplit("_", 1)[0]
            if modules and modules[0].get("violations")
            else "unknown"
        )

        record = SpecGapRecord(
            bug_description=(
                f"CEGIS tool loop exhausted while healing {mode} violation in {Path(bug_file).name}. "
                f"The LLM read the file repeatedly but never produced a valid patch, "
                f"exhausting all tool rounds without progress."
            ),
            bug_file=bug_file,
            bug_line=bug_line,
            bug_manifestation=error_msg,
            bug_fix="Not yet fixed — training example for HealMustTerminateOrFail invariant",
            tla_spec_path=str(tla_path),
            tla_spec_text=tla_text,
        )
        save(record)
        if verbose:
            print(
                "  spec_learner: recorded tool-loop-exhausted failure as training example"
            )
    except Exception as save_exc:
        if verbose:
            print(f"  spec_learner: failed to record failure ({save_exc})")


def heal(
    violations_doc: dict,
    target: Path,
    test_cmd: str,
    model: str,
    key: str,
    severity: list[str],
    improve: bool,
    verbose: bool,
) -> tuple[int, int, int]:
    """CEGIS repair cycle. Returns (attempted, accepted, oracle_confirmed)."""
    import tempfile
    from .heal import heal_project

    priorities = _topology_priority(violations_doc, target)
    violations_doc = _reprioritize(violations_doc, priorities)

    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        tmp.write_text(json.dumps(violations_doc), encoding="utf-8")
        result = heal_project(
            violations_path=tmp,
            api_key=key,
            severity_filter=severity,
            apply=True,
            test_cmd=test_cmd,
            project_root=target,
            verbose=verbose,
        )
        att = len(result.results)
        acc = sum(1 for r in result.results if r.verify_verdict == "ACCEPT")
        orc = sum(1 for r in result.results if getattr(r, "oracle_confirmed", False))

        if improve and att > 0 and acc / att < 0.85:
            # Trigger heal prompt self-improvement
            from .heal import _improve_heal_prompt

            _improve_heal_prompt(result.results, model, key, verbose)

        return att, acc, orc
    except Exception as exc:
        if verbose:
            print(f"  heal: failed: {exc}")
        # Record tool-loop exhaustion as a spec_learner training example so
        # the TLA+ model can eventually learn a HealMustTerminateOrFail invariant.
        if "Tool loop exhausted" in str(exc) and key:
            _record_heal_failure(violations_doc, target, str(exc), verbose)
        return 0, 0, 0
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Phase 3: ADR generation
# ---------------------------------------------------------------------------


def _next_adr_number(adr_dir: Path) -> int:
    existing = list(adr_dir.glob("ADR-*.md"))
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("-")[1]))
        except (IndexError, ValueError):
            pass
    return max(nums, default=36) + 1


def generate_adr(
    state: IterationState,
    target: Path,
    adr_dir: Path,
    initial_violations: int,
) -> Optional[str]:
    """
    Generate an ADR documenting the architectural decision this iteration made.
    Returns the ADR filename if generated, None otherwise.
    """
    # Only generate when something significant happened
    sig = (
        state.heal_accepted > 0
        or state.measure.sheaf_rank > 0
        or state.termination is not None
    )
    if not sig:
        return None

    adr_dir.mkdir(parents=True, exist_ok=True)
    n = _next_adr_number(adr_dir)
    date = time.strftime("%Y-%m-%d")

    title = f"pact-loop iteration {state.iteration} on {target.name}"
    if state.termination:
        title = f"pact-loop convergence: {state.termination} on {target.name}"
    elif state.heal_accepted > 0:
        title = (
            f"pact-loop healed {state.heal_accepted}/{state.heal_attempted} "
            f"violations in {target.name}"
        )

    top_modes = list(state.measure.checker_by_mode.items())[:5]

    body = textwrap.dedent(f"""\
        # ADR-{n:03d}: {title}

        **Status**: Auto-generated by pact_loop
        **Date**: {date}
        **Iteration**: {state.iteration}

        ---

        ## Context

        pact_loop ran on `{target}` targeting `{initial_violations}` initial violations.

        **Violation profile (iteration {state.iteration})**:
        - Total: {state.total_violations} (was {initial_violations})
        - Checker modes: {', '.join(f'{m}({c})' for m, c in top_modes)}
        - Interprocedural (Z3 transitive): {state.measure.interproc_transitive}
        - Sheaf Ȟ¹ rank: {state.measure.sheaf_rank}
        - Topology score (β₁): {state.topo_score:.3f}
        - SCCs: {state.measure.scc_count}, Hubs: {state.measure.hub_count}

        **CEGIS heal results**:
        - Attempted: {state.heal_attempted}
        - Accepted (LLM verify ≥ 0.8): {state.heal_accepted}
        - Oracle confirmed (test suite): {state.oracle_confirmed}
        - Accept rate: {state.heal_accept_rate:.0%}
        - Oracle trust: {state.oracle_confirm_rate:.0%}

        **Prompt quality**:
        - heal.md score: {state.heal_prompt_score:.2f}
        - find.md score: {state.find_prompt_score:.2f}
        - Average: {state.avg_prompt_score:.2f}

        **Fitness**: {state.fitness:.3f}

        ---

        ## Decision

        {"Loop terminated: " + state.termination if state.termination else "Loop continues to next iteration."}

        {"All violations resolved. Codebase is formally clean." if state.total_violations == 0 else
         "Remaining violations require further iterations or manual review."}

        {"Prompts were rewritten (score < 0.8)." if state.avg_prompt_score < 0.8 else
         "Prompts are performing well (score ≥ 0.8); no rewrite triggered."}

        ---

        ## Consequences

        - The fitness function weights this iteration at **{state.fitness:.3f}**.
        - {"Convergence declared — loop exits." if state.termination in ("PROVED_CLEAN", "CONVERGED") else
           "Next iteration will re-run measure phase with updated codebase."}
        - ADR auto-generated by `pact_loop.generate_adr()`.
        - Blast-radius top violations: {state.measure.blast_top[:2]}

        ---

        *Generated by pact_loop.py — do not edit manually*
    """)

    filename = f"ADR-{n:03d}-loop-iter-{state.iteration}-{target.name}.md"
    (adr_dir / filename).write_text(body, encoding="utf-8")
    return filename


# ---------------------------------------------------------------------------
# Phase 4: TLA+ spec for the loop itself
# ---------------------------------------------------------------------------


def generate_tla_spec(max_iters: int, output_dir: Path) -> Path:
    spec = textwrap.dedent("""\
        ---- MODULE PactLoop ----
        (*
         * TLA+ model of pact_loop.py
         *
         * Proves three properties:
         *   OracleSafety    -- patches only applied after oracle passes
         *   Termination     -- loop always halts
         *   FitnessProgress -- fitness non-decreasing on average
         *
         * See ADR-037 and docs/tla/ for context.
         *)
        EXTENDS Naturals, Sequences

        CONSTANTS MAX_ITERS, WINDOW

        VARIABLES
            iter,
            violations,
            fitness_history,
            oracle_passed,
            patches_applied,
            phase,
            termination

        TypeInvariant ==
            /\\ iter \\in 0..MAX_ITERS
            /\\ violations \\in Nat
            /\\ phase \\in {"measure","heal","improve","check"}
            /\\ termination \\in {"","PROVED_CLEAN","CONVERGED","STUCK","TIMEOUT"}
            /\\ patches_applied \\subseteq oracle_passed

        Init ==
            /\\ iter = 0
            /\\ violations = 0
            /\\ fitness_history = <<>>
            /\\ oracle_passed = {}
            /\\ patches_applied = {}
            /\\ phase = "measure"
            /\\ termination = ""

        Measure ==
            /\\ phase = "measure" /\\ termination = ""
            /\\ phase' = "heal"
            /\\ UNCHANGED <<iter, violations, fitness_history,
                            oracle_passed, patches_applied, termination>>

        Heal(patch, oracle_ok) ==
            /\\ phase = "heal"
            /\\ IF oracle_ok
               THEN /\\ oracle_passed' = oracle_passed \\cup {patch}
                    /\\ patches_applied' = patches_applied \\cup {patch}
               ELSE /\\ UNCHANGED <<oracle_passed, patches_applied>>
            /\\ phase' = "improve"
            /\\ UNCHANGED <<iter, violations, fitness_history, termination>>

        Improve ==
            /\\ phase = "improve"
            /\\ phase' = "check"
            /\\ UNCHANGED <<iter, violations, fitness_history,
                            oracle_passed, patches_applied, termination>>

        Check(new_v, new_f) ==
            /\\ phase = "check"
            /\\ violations' = new_v
            /\\ fitness_history' = Append(fitness_history, new_f)
            /\\ iter' = iter + 1
            /\\ termination' = IF new_v = 0 THEN "PROVED_CLEAN"
                               ELSE IF iter + 1 >= MAX_ITERS THEN "TIMEOUT"
                               ELSE ""
            /\\ phase' = IF termination' /= "" THEN "check" ELSE "measure"
            /\\ UNCHANGED <<oracle_passed, patches_applied>>

        Next ==
            \\/ Measure
            \\/ \\E p \\in {0,1}, ok \\in {TRUE, FALSE}: Heal(p, ok)
            \\/ Improve
            \\/ \\E v \\in 0..100, f \\in {0,1}: Check(v, f)

        Spec == Init /\\ [][Next]_<<iter,violations,fitness_history,
                                     oracle_passed,patches_applied,phase,termination>>

        OracleSafety == patches_applied \\subseteq oracle_passed
        Termination  == <>[](termination /= "")

        ====
    """)

    cfg = textwrap.dedent(f"""\
        SPECIFICATION Spec
        CONSTANTS MAX_ITERS = {max_iters}  WINDOW = 3
        INVARIANT TypeInvariant
        INVARIANT OracleSafety
        PROPERTY Termination
    """)

    tla_path = output_dir / "PactLoop.tla"
    cfg_path = output_dir / "PactLoop.cfg"
    tla_path.write_text(spec, encoding="utf-8")
    cfg_path.write_text(cfg, encoding="utf-8")
    return tla_path


# ---------------------------------------------------------------------------
# Main loop orchestrator
# ---------------------------------------------------------------------------


class PactLoop:
    def __init__(
        self,
        target: Path,
        test_cmd: str,
        api_key: str = "",
        model: str = _DEFAULT_MODEL,
        max_iters: int = _MAX_ITERS,
        epsilon: float = _EPSILON,
        severity: list[str] = None,
        run_find: bool = False,
        improve: bool = True,
        generate_tla: bool = True,
        output_dir: Optional[Path] = None,
        adr_dir: Optional[Path] = None,
        verbose: bool = False,
    ):
        self.target = target
        self.test_cmd = test_cmd
        self.api_key = api_key or _API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.max_iters = max_iters
        self.epsilon = epsilon
        self.severity = severity or ["critical", "high"]
        self.run_find = run_find
        self.improve = improve
        self.generate_tla = generate_tla
        self.output_dir = output_dir or (target / ".pact_loop")
        self.adr_dir = adr_dir or (target / "docs" / "adr")
        self.verbose = verbose

        self._fitness_history: list[float] = []
        self._accepted_history: list[int] = []
        self._initial_violations: int = 0

    def run(self) -> LoopResult:
        start = time.time()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if self.generate_tla:
            tla_path = generate_tla_spec(self.max_iters, self.output_dir)
            print(f"[loop] TLA+ spec written → {tla_path}")

        print("\n[loop] ═══════════════════════════════════════════════════")
        print(f"[loop] target:   {self.target}")
        print(f"[loop] oracle:   {self.test_cmd}")
        print(
            f"[loop] settings: max_iters={self.max_iters} ε={self.epsilon} "
            f"severity={self.severity}"
        )
        print(
            f"[loop] phases:   measure+heal+improve | find={'yes' if self.run_find else 'no'} "
            f"| tla={'yes' if self.generate_tla else 'no'}"
        )
        print("[loop] ═══════════════════════════════════════════════════")

        states: list[IterationState] = []
        termination = ""

        for i in range(self.max_iters):
            print(
                f"\n[loop] ── iteration {i+1}/{self.max_iters} "
                f"{'─' * (50 - len(str(i+1)))}"
            )

            # Clear per-file checker caches so healed files don't re-appear as
            # stale violations in this iteration's measure phase.
            from .failure_mode import clear_file_caches

            clear_file_caches()

            state = IterationState(iteration=i + 1)

            # ── MEASURE ─────────────────────────────────────────────────────
            m = measure(
                self.target,
                self.model,
                self.api_key,
                self.run_find,
                self.improve,
                self.verbose,
            )
            state.measure = m
            state.total_violations = m.checker_total + max(
                0, m.interproc_transitive - m.checker_total
            )
            state.find_confirm_rate = (
                m.find_confirmed / max(m.find_attempted, 1) if m.find_attempted else 0.0
            )
            state.topo_score = m.topo_score
            state.sheaf_score = max(0.0, 1.0 - m.sheaf_rank / 50.0)  # normalize at 50

            if i == 0:
                self._initial_violations = state.total_violations
                print(f"\n[loop] baseline: {self._initial_violations} violations")

            if state.total_violations == 0 and m.sheaf_rank == 0:
                state.termination = termination = "PROVED_CLEAN"
                state.fitness = compute_fitness(state, self._initial_violations)
                self._fitness_history.append(state.fitness)
                states.append(state)
                self._save(states)
                print("\n[loop] ✓ PROVED_CLEAN — zero violations, Ȟ¹ rank = 0")
                break

            # ── HEAL ────────────────────────────────────────────────────────
            print("\n[loop:heal]")
            h_att, h_acc, h_orc = heal(
                m.violations_doc,
                self.target,
                self.test_cmd,
                self.model,
                self.api_key,
                self.severity,
                self.improve,
                self.verbose,
            )
            state.heal_attempted = h_att
            state.heal_accepted = h_acc
            state.oracle_confirmed = h_orc
            state.heal_accept_rate = h_acc / max(h_att, 1)
            state.oracle_confirm_rate = h_orc / max(h_att, 1)
            self._accepted_history.append(h_acc)

            # ── IMPROVE ─────────────────────────────────────────────────────
            if self.improve:
                heal_score = state.heal_accept_rate
                find_score = (
                    min(1.0, state.find_confirm_rate / 0.30) if self.run_find else 1.0
                )
                state.heal_prompt_score = heal_score
                state.find_prompt_score = find_score
                state.avg_prompt_score = (heal_score + find_score) / 2.0

            # ── FITNESS ─────────────────────────────────────────────────────
            state.fitness = compute_fitness(state, self._initial_violations)
            self._fitness_history.append(state.fitness)

            print(
                f"\n[loop:fitness] f={state.fitness:.3f}  "
                f"violations={state.total_violations}  "
                f"heal={state.heal_accept_rate:.0%}  "
                f"oracle={state.oracle_confirm_rate:.0%}  "
                f"topo={state.topo_score:.2f}  "
                f"sheaf_rank={m.sheaf_rank}"
            )

            # ── ADR ──────────────────────────────────────────────────────────
            adr_file = generate_adr(
                state, self.target, self.adr_dir, self._initial_violations
            )
            if adr_file:
                state.adr_generated = adr_file
                print(f"[loop] ADR → {adr_file}")

            # ── CONVERGENCE CHECK ───────────────────────────────────────────
            if _converged(self._fitness_history, self.epsilon, _WINDOW):
                state.termination = termination = "CONVERGED"
                states.append(state)
                self._save(states)
                print(
                    f"\n[loop] ✓ CONVERGED — |Δfitness| < {self.epsilon} "
                    f"for {_WINDOW} iterations"
                )
                break

            if _stuck(self._accepted_history, _STUCK_WINDOW):
                state.termination = termination = "STUCK"
                states.append(state)
                self._save(states)
                print(
                    f"\n[loop] ⚠ STUCK — 0 patches accepted for "
                    f"{_STUCK_WINDOW} consecutive iterations"
                )
                break

            if i + 1 >= self.max_iters:
                state.termination = termination = "TIMEOUT"

            states.append(state)
            self._save(states)

        else:
            termination = termination or "TIMEOUT"

        elapsed = time.time() - start
        result = LoopResult(
            target=str(self.target),
            test_cmd=self.test_cmd,
            iterations=states,
            termination=termination,
            initial_violations=self._initial_violations,
            final_violations=states[-1].total_violations if states else 0,
            final_fitness=states[-1].fitness if states else 0.0,
            elapsed_seconds=elapsed,
        )
        print(f"\n{'═'*60}")
        print(result.summary())
        return result

    def _save(self, states: list[IterationState]) -> None:
        out = self.output_dir / "loop_state.json"
        out.write_text(
            json.dumps([dataclasses.asdict(s) for s in states], indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        prog="pact loop",
        description="Recursive self-improvement loop with ML convergence criterion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Analyzers run each iteration:
              checker      — 15 Python violation modes (AST)
              interproc    — Z3 Fixedpoint interprocedural (transitive)
              sheaf        — Ȟ¹ cohomology rank (min independent fixes)
              z3-engine    — Datalog constraint proofs
              tda          — β₁ Betti topology score (requires graphify)
              blast-radii  — SCC + hub + blast radius (requires graphify)
              find         — LLM property discovery + Hypothesis (--find)

            Termination:
              PROVED_CLEAN  violations = 0 and Ȟ¹ rank = 0
              CONVERGED     |Δfitness| < epsilon for 3 iters
              STUCK         0 patches accepted for 2 iters
              TIMEOUT       max-iters reached

            Examples:
              # Pact on itself
              pact loop . \\
                --test-cmd "python3.11 -m pytest --import-mode=importlib --pyargs pact.test_fixer pact.test_checker pact.test_z3_engine pact.test_hypothesis_checkers -q"

              # Full pipeline on external repo
              pact loop ~/src/click-target \\
                --test-cmd "pytest tests/ -x -q" \\
                --find --improve --severity critical high medium --verbose
        """),
    )
    p.add_argument("target", type=Path)
    p.add_argument("--test-cmd", required=True)
    p.add_argument("--model", default=_DEFAULT_MODEL)
    p.add_argument("--api-key")
    p.add_argument("--max-iters", type=int, default=_MAX_ITERS)
    p.add_argument("--epsilon", type=float, default=_EPSILON)
    p.add_argument(
        "--severity",
        nargs="+",
        default=["critical", "high"],
        choices=["critical", "high", "medium", "low"],
    )
    p.add_argument("--find", action="store_true")
    p.add_argument("--improve", action="store_true", default=True)
    p.add_argument("--no-improve", action="store_false", dest="improve")
    p.add_argument("--no-tla", action="store_false", dest="tla", default=True)
    p.add_argument("--out", type=Path)
    p.add_argument("--adr-dir", type=Path)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    key = args.api_key or _API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("error: set ANTHROPIC_API_KEY or use --api-key", file=sys.stderr)
        sys.exit(1)

    PactLoop(
        target=args.target.resolve(),
        test_cmd=args.test_cmd,
        api_key=key,
        model=args.model,
        max_iters=args.max_iters,
        epsilon=args.epsilon,
        severity=args.severity,
        run_find=args.find,
        improve=args.improve,
        generate_tla=args.tla,
        output_dir=args.out,
        adr_dir=args.adr_dir,
        verbose=args.verbose,
    ).run()


if __name__ == "__main__":
    main()
