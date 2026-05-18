"""pact CLI — run constraint analysis on a codebase."""

import argparse
import json
import sys
from pathlib import Path

from .checker import check_codebase, check_codebase_incremental
from .extractor import extract_from_codebase
from .reduce import (
    analyze_graph_reduction,
    apply_full_reduction,
    compute_blast_radii,
    compute_fitness,
)
from .refactor import suggest_refactors
from .specgen import spec_gen
from .speccomplete import spec_complete
from .visualize import (
    format_pr_comment,
    render_mermaid,
    render_test_coverage_mermaid,
)


class DiffResolutionError(RuntimeError):
    """Raised when diff-scoped analysis cannot resolve the changed file set."""


def _changed_files_on_branch(base: str = "main", cwd: Path = None) -> set[str]:
    """Return absolute paths of files changed vs base branch."""
    import subprocess

    cwd = cwd or Path(".").resolve()
    try:
        git_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
        ).stdout.strip()
        git_root_path = Path(git_root)
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=git_root_path,
        )
        return {
            str(git_root_path / p.strip())
            for p in result.stdout.splitlines()
            if p.strip().endswith(".py")
        }
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = (getattr(exc, "stderr", None) or "").strip()
        stdout = (getattr(exc, "stdout", None) or "").strip()
        detail = stderr or stdout or str(exc)
        raise DiffResolutionError(detail) from exc


def _spec_cmd(argv) -> int:
    """Entry point for `pact spec {gen,complete} <file>`."""
    p = argparse.ArgumentParser(
        prog="pact spec",
        description="Synthesize or complete a TLA+ specification from Python source.",
    )
    sub = p.add_subparsers(dest="spec_cmd", required=True)

    gen_p = sub.add_parser("gen", help="Generate a TLA+ skeleton from a Python file")
    gen_p.add_argument("file", metavar="FILE", help="Python source file to analyze")
    gen_p.add_argument(
        "--output",
        "-o",
        metavar="OUT",
        default=None,
        help="Write spec to OUT instead of stdout",
    )

    cmp_p = sub.add_parser(
        "complete",
        help="Fill in TODO stubs using an LLM (requires ANTHROPIC_API_KEY)",
    )
    cmp_p.add_argument("file", metavar="FILE", help="Python source file to analyze")
    cmp_p.add_argument(
        "--output",
        "-o",
        metavar="OUT",
        default=None,
        help="Write completed spec to OUT instead of stdout",
    )
    cmp_p.add_argument(
        "--model",
        default="claude-haiku-4-5-20251001",
        metavar="MODEL",
        help="Claude model to use (default: claude-haiku-4-5-20251001)",
    )
    cmp_p.add_argument(
        "--api-key",
        default=None,
        metavar="KEY",
        help="Anthropic API key (default: $ANTHROPIC_API_KEY)",
    )

    args = p.parse_args(argv)

    src = Path(args.file).resolve()
    if not src.is_file():
        print(f"error: {src} is not a file", file=sys.stderr)
        return 2

    out_path = Path(args.output).resolve() if args.output else None

    if args.spec_cmd == "gen":
        spec = spec_gen(src, out_path)
        if out_path:
            print(f"✓  pact spec gen: wrote {out_path}")
        else:
            print(spec)
    else:  # complete
        try:
            spec = spec_complete(src, out_path, model=args.model, api_key=args.api_key)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if out_path:
            print(f"✓  pact spec complete: wrote {out_path}")
        else:
            print(spec)
    return 0


def _fix_cmd(argv) -> int:
    """Entry point for `pact fix [DIR] [--apply] [--mode MODE] [--write-tests]`."""
    from .fixer import FIX_MODES, apply_fixes, diff_text
    from .checker import check_codebase

    p = argparse.ArgumentParser(
        prog="pact fix",
        description=(
            "Generate patches for fixable violations. "
            "Prints unified diffs by default; use --apply to write files."
        ),
    )
    p.add_argument(
        "root",
        nargs="?",
        default=".",
        metavar="DIR",
        help="Root directory to analyze (default: current directory)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write patches to files in-place (default: dry-run, print diffs)",
    )
    p.add_argument(
        "--mode",
        metavar="MODE",
        default=None,
        help=(
            f"Only fix this violation mode (fixable modes: {', '.join(sorted(FIX_MODES))})"
        ),
    )
    p.add_argument(
        "--write-tests",
        action="store_true",
        help=(
            "Generate a regression test file alongside each patched file "
            "proving each guard fires correctly on empty LLM responses."
        ),
    )
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    mode_filter = frozenset({args.mode}) if args.mode else None
    if mode_filter and not mode_filter <= FIX_MODES:
        unknown = mode_filter - FIX_MODES
        print(
            f"error: mode(s) not fixable by pact fix: {', '.join(unknown)}\n"
            f"  fixable: {', '.join(sorted(FIX_MODES))}",
            file=sys.stderr,
        )
        return 2

    violations = check_codebase(root)
    results = apply_fixes(violations, dry_run=not args.apply, mode_filter=mode_filter)

    changed = [r for r in results if r.changed]
    if not changed:
        print("✓  pact fix: nothing to fix")
        return 0

    total_applied = sum(len(r.applied) for r in changed)
    action = "Applied" if args.apply else "Would fix"
    print(
        f"{'✓' if args.apply else '⚡'}  pact fix: {action} {total_applied} violation(s)"
        f" across {len(changed)} file(s)"
        + (" (dry-run — use --apply to write)" if not args.apply else "")
    )
    print()

    for r in changed:
        print(diff_text(r.path, r.original, r.patched), end="")

    if args.write_tests:
        from .test_writer import generate_test_file

        test_files_written: list[str] = []
        for r in changed:
            test_src = generate_test_file(r.path, r.applied, r.patched)
            if not test_src:
                continue
            stem = Path(r.path).stem
            test_path = Path(r.path).parent / f"test_pact_guards_{stem}.py"
            if args.apply:
                test_path.write_text(test_src, encoding="utf-8")
                test_files_written.append(str(test_path))
            else:
                print(f"# --- would write {test_path} ---")
                print(test_src)

        if args.apply and test_files_written:
            print(
                f"✓  pact fix: wrote {len(test_files_written)} regression test file(s):"
            )
            for tf in test_files_written:
                print(f"   {tf}")

    return 0


# Corpus-derived leverage data: packages imported by corpus repos that themselves have violations.
# Sorted by leverage score (violations × log1p(stars) × (1 + downstream_repos)).
KNOWN_UPSTREAM_VIOLATIONS: dict[str, dict] = {
    "openai": {
        "repo": "openai/openai-python",
        "violations": 114,
        "stars": 30767,
        "downstream_repos": 75,
        "leverage": 89535,
    },
    "langchain": {
        "repo": "langchain-ai/langchain",
        "violations": 17,
        "stars": 136637,
        "downstream_repos": 48,
        "leverage": 9850,
    },
    "langchain_core": {
        "repo": "langchain-ai/langchain",
        "violations": 17,
        "stars": 136637,
        "downstream_repos": 48,
        "leverage": 9850,
    },
    "langchain_community": {
        "repo": "langchain-ai/langchain",
        "violations": 17,
        "stars": 136637,
        "downstream_repos": 48,
        "leverage": 9850,
    },
    "langchain_openai": {
        "repo": "langchain-ai/langchain",
        "violations": 17,
        "stars": 136637,
        "downstream_repos": 48,
        "leverage": 9850,
    },
    "langchain_anthropic": {
        "repo": "langchain-ai/langchain",
        "violations": 17,
        "stars": 136637,
        "downstream_repos": 48,
        "leverage": 9850,
    },
    "langchain_google_genai": {
        "repo": "langchain-ai/langchain",
        "violations": 17,
        "stars": 136637,
        "downstream_repos": 48,
        "leverage": 9850,
    },
    "langgraph": {
        "repo": "langchain-ai/langgraph",
        "violations": 60,
        "stars": 32219,
        "downstream_repos": 9,
        "leverage": 6228,
    },
    "litellm": {
        "repo": "BerriAI/litellm",
        "violations": 19,
        "stars": 46818,
        "downstream_repos": 6,
        "leverage": 1430,
    },
    "llama_index": {
        "repo": "run-llama/llama_index",
        "violations": 12,
        "stars": 41180,
        "downstream_repos": 11,
        "leverage": 1416,
    },
    "crewai": {
        "repo": "crewAIInc/crewAI",
        "violations": 8,
        "stars": 30231,
        "downstream_repos": 4,
        "leverage": 514,
    },
    "agentops": {
        "repo": "AgentOps-AI/agentops",
        "violations": 6,
        "stars": 3205,
        "downstream_repos": 3,
        "leverage": 98,
    },
    "dspy": {
        "repo": "stanfordnlp/dspy",
        "violations": 5,
        "stars": 24009,
        "downstream_repos": 2,
        "leverage": 93,
    },
    "anthropic": {
        "repo": "anthropics/anthropic-sdk-python",
        "violations": 3,
        "stars": 3640,
        "downstream_repos": 1,
        "leverage": 24,
    },
}


def _extract_top_level_imports(root: Path) -> dict[str, str]:
    """Return {package_name: first_file_that_imports_it} for all .py files under root."""
    import ast as _ast

    found: dict[str, str] = {}
    for py_file in sorted(root.rglob("*.py")):
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = _ast.parse(source, filename=str(py_file))
        except (SyntaxError, OSError):
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    pkg = alias.name.split(".")[0]
                    if pkg not in found:
                        found[pkg] = str(py_file.relative_to(root))
            elif isinstance(node, _ast.ImportFrom):
                if node.module and node.level == 0:
                    pkg = node.module.split(".")[0]
                    if pkg not in found:
                        found[pkg] = str(py_file.relative_to(root))
    return found


def main(argv=None) -> int:
    # Top-level: if first arg is "spec" or "fix", delegate to subcommand
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "spec":
        return _spec_cmd(argv[1:])
    if argv and argv[0] == "fix":
        return _fix_cmd(argv[1:])

    p = argparse.ArgumentParser(
        prog="pact",
        description="Python AST Constraint Tool — verify constraints across a codebase using Z3.",
    )
    p.add_argument(
        "root",
        nargs="?",
        default=".",
        metavar="DIR",
        help="Root directory to analyze (default: current directory)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="Emit results as JSON array",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any violation found",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Print extraction statistics before results",
    )
    p.add_argument(
        "--diff",
        metavar="BASE",
        nargs="?",
        const="main",
        help="Only report violations in files changed vs BASE branch (default: main)",
    )
    p.add_argument(
        "--incremental",
        metavar="BASE",
        nargs="?",
        const="main",
        help=(
            "Analyze only the dirty subgraph: files changed vs BASE plus their "
            "transitive callers (default BASE: main). Faster than --diff because "
            "unchanged graph nodes are never analyzed."
        ),
    )
    p.add_argument(
        "--suggest",
        action="store_true",
        help="Suggest safe refactor targets: functions with high violation density and low coupling",
    )
    p.add_argument(
        "--suggest-min",
        type=int,
        default=1,
        metavar="N",
        help="Minimum violations to include in refactor suggestions (default: 1)",
    )
    p.add_argument(
        "--reduce",
        action="store_true",
        help=(
            "Graph reduction analysis: find call cycles (SCCs), pass-through nodes, "
            "and fan-out hubs — the structural moving pieces that drive fragility"
        ),
    )
    p.add_argument(
        "--hub-threshold",
        type=int,
        default=8,
        metavar="N",
        help="Fan-out threshold for hub detection in --reduce (default: 8)",
    )
    p.add_argument(
        "--reduce-limit",
        type=int,
        default=20,
        metavar="N",
        help="Max simplification targets to show in --reduce output (default: 20)",
    )
    p.add_argument(
        "--reduce-apply",
        action="store_true",
        help=(
            "Apply the full three-stage reduction pipeline (SCC contraction → "
            "dead-node pruning → transitive reduction) and show before/after stats"
        ),
    )
    p.add_argument(
        "--fitness",
        action="store_true",
        help=(
            "Structural fitness score: ratio of actual call graph size to its "
            "minimum equivalent (transitive reduction of the condensation DAG). "
            "1.0 = optimal structure; lower = excess nodes/edges beyond the minimum."
        ),
    )
    p.add_argument(
        "--blast-radius",
        action="store_true",
        help=(
            "Re-rank violations by call-graph blast radius: the number of distinct "
            "functions that can transitively reach the function containing each "
            "violation.  Higher blast radius = higher actual exposure.  This is a "
            "verifiable graph-theoretic upper bound, not a heuristic severity label."
        ),
    )
    p.add_argument(
        "--graph",
        action="store_true",
        help="Print the violation call graph as a Mermaid flowchart",
    )
    p.add_argument(
        "--graph-tests",
        action="store_true",
        help="Print a separate test coverage graph (test→production edges)",
    )
    p.add_argument(
        "--pr-comment",
        action="store_true",
        help="Print a full GitHub PR comment body (call graph + reduction sequence + test coverage)",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Run Z3 Fixedpoint proof for llm_response_unguarded. "
            "UNSAT → formal certificate of absence; SAT → violation witnesses. "
            "Requires z3-solver."
        ),
    )
    p.add_argument(
        "--upstream",
        action="store_true",
        help=(
            "Show which of your imported packages have known violations in the pact corpus. "
            "Ranked by leverage score (violations × log(stars) × downstream fan-out)."
        ),
    )
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    extracted = extract_from_codebase(root)
    models, functions, call_sites = extracted

    # When --json is active, stats must go to stderr so stdout stays pure JSON.
    stats_file = sys.stderr if args.json_mode else sys.stdout
    if args.stats:
        print(
            f"models: {len(models)}  functions: {len(functions)}  call sites: {len(call_sites)}",
            file=stats_file,
        )

    incremental_stats: dict = {}
    if args.incremental is not None:
        changed = _changed_files_on_branch(args.incremental, cwd=root)
        violations, incremental_stats = check_codebase_incremental(
            root,
            changed,
            _extracted=extracted,
        )
        if args.stats:
            s = incremental_stats
            skipped_pct = int(s["skip_ratio"] * 100)
            print(
                f"incremental vs {args.incremental}: "
                f"{s['dirty_files']}/{s['total_files']} files dirty, "
                f"{s['dirty_call_sites']}/{s['total_call_sites']} call sites analyzed "
                f"({skipped_pct}% skipped)",
                file=stats_file,
            )
    else:
        violations = check_codebase(root, _extracted=extracted)

    if args.diff is not None:
        changed = _changed_files_on_branch(args.diff, cwd=root)
        violations = [v for v in violations if v.file in changed]
        if args.stats:
            print(f"files changed vs {args.diff}: {len(changed)}", file=stats_file)

    if args.json_mode:
        print(
            json.dumps(
                [
                    {
                        "file": v.file,
                        "line": v.line,
                        "call": v.call,
                        "missing": v.missing,
                        "context": v.context,
                    }
                    for v in violations
                ],
                indent=2,
            )
        )
    elif args.blast_radius:
        if not violations:
            label = f"(diff vs {args.diff})" if args.diff else ""
            print(f"✓  pact: no constraint violations {label}".strip())
        else:
            ranked = compute_blast_radii(functions, call_sites, violations)
            label = f"(diff vs {args.diff})" if args.diff else ""
            print(
                f"✗  pact: {len(ranked)} violation(s) ranked by blast radius"
                f" {label}\n".strip()
            )
            for r in ranked:
                print(r.summary())
                print()
    else:
        if not violations:
            label = f"(diff vs {args.diff})" if args.diff else ""
            print(f"✓  pact: no constraint violations {label}".strip())
        else:
            label = f"(diff vs {args.diff})" if args.diff else ""
            print(f"✗  pact: {len(violations)} violation(s) {label}\n".strip())
            for v in violations:
                print(f"  {v}")
            print()

    suggestions: list = []
    if args.suggest or args.pr_comment:
        suggestions = suggest_refactors(
            violations,
            functions,
            call_sites,
            min_violations=args.suggest_min,
        )
        if args.suggest and not args.json_mode:
            if suggestions:
                print(f"\n⚡ pact: {len(suggestions)} refactor suggestion(s)\n")
                for s in suggestions:
                    print(s.summary())
                    print()
            else:
                print("\n✓  pact: no refactor targets above threshold")

    if args.reduce and not args.json_mode:
        reduction = analyze_graph_reduction(
            functions,
            call_sites,
            violations,
            hub_threshold=args.hub_threshold,
        )
        if reduction:
            shown = reduction[: args.reduce_limit]
            print(
                f"\n⬡ pact --reduce: {len(reduction)} simplification target(s)"
                f"  (showing top {len(shown)})\n"
            )
            for c in shown:
                print(c.summary())
                print()
        else:
            print(
                "\n✓  pact --reduce: call graph has no detected tangles, pass-throughs, or hubs"
            )

    if args.reduce_apply and not args.json_mode:
        result = apply_full_reduction(functions, call_sites, violations)
        print("\n⬡ pact --reduce-apply: three-stage graph reduction pipeline\n")
        print(result.summary())
        print()

    if args.fitness and not args.json_mode:
        fitness = compute_fitness(functions, call_sites)
        print("\n⬡ pact --fitness: structural fitness of call graph\n")
        print(fitness.summary())
        print()

    if args.graph:
        # Use suggestion data for accurate attribution if available; else compute
        if not suggestions and violations:
            _graph_suggestions = suggest_refactors(
                violations, functions, call_sites, verify=False
            )
        else:
            _graph_suggestions = suggestions
        vcounts = {s.func_name: s.violation_count for s in _graph_suggestions}
        diagram = render_mermaid(
            violations,
            functions,
            call_sites,
            highlight={s.func_name for s in _graph_suggestions},
            violation_counts=vcounts,
        )
        print("\n```mermaid")
        print(diagram)
        print("```\n")

    if args.graph_tests:
        diagram = render_test_coverage_mermaid(functions, call_sites)
        print("\n```mermaid")
        print(diagram)
        print("```\n")

    if args.pr_comment:
        print(format_pr_comment(suggestions, violations, functions, call_sites))

    if args.verify and not args.json_mode:
        try:
            from .z3_engine import run_llm

            proof = run_llm(root)
            if proof.proved_safe:
                print(
                    f"✓  pact --verify: SAFE — Z3 proved llm_response_unguarded absent "
                    f"({proof.scopes_analyzed} scope(s) analyzed)"
                )
            else:
                print(
                    f"⚡ pact --verify: UNSAFE — Z3 witness confirms "
                    f"{len(proof.violations)} llm_response_unguarded violation(s)"
                )
                for v in proof.violations:
                    print(f"  {v.file}  {v.call}")
        except ImportError:
            print(
                "  pact --verify: z3-solver not installed; skipping proof",
                file=sys.stderr,
            )

    if args.upstream and not args.json_mode:
        imports = _extract_top_level_imports(root)
        hits = []
        seen_repos: set[str] = set()
        for pkg, info in KNOWN_UPSTREAM_VIOLATIONS.items():
            repo = info["repo"]
            if pkg in imports and repo not in seen_repos:
                seen_repos.add(repo)
                hits.append((pkg, info, imports[pkg]))
        hits.sort(key=lambda x: -x[1]["leverage"])
        if not hits:
            print(
                "\n✓  pact --upstream: none of your imports match the corpus violation list"
            )
        else:
            print(
                f"\n⬡ pact --upstream: {len(hits)} imported package(s) with corpus violations\n"
            )
            for pkg, info, first_file in hits:
                repo = info["repo"]
                viol = info["violations"]
                stars = info["stars"]
                dnstrm = info["downstream_repos"]
                lev = info["leverage"]
                print(
                    f"  import {pkg:<28}  → {repo:<40}"
                    f"  {viol:>4} violations  {stars:>7,}★  {dnstrm:>3} downstream  leverage {lev:>8,.0f}"
                )
                print(f"    first seen: {first_file}")
            print()
            print("  Fixing upstream = propagating the fix to all downstream users.")
            print(
                "  Run `pact <path/to/site-packages/PACKAGE>` to check your local copy."
            )
            print()

    return 1 if (violations and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
