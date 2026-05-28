"""
pact MCP server -- exposes pact as MCP tools over stdio.

Tools:
  pact_context(file_path)              → git/changelog intent signals
  pact_find(file_path)                 → property violations + counterexamples
  pact_heal(violations_json, test_cmd) → patch + oracle result
  pact_check(path)                     → fast static violations
  pact_loop(target)                    → full autonomous convergence loop
  pact_tda(violations_json, root)      → β₁ topology scoring
  pact_sheaf(file_path)                → Ȟ¹ cohomological LLM-guard check
  pact_spec_learn(mode, ...)           → TLA+ spec gap corpus management

Usage:
    python -m pact.mcp_server
    # or via pyproject.toml entry point: pact-mcp
"""

from __future__ import annotations

import contextlib
import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# ---------------------------------------------------------------------------
# Minimal MCP protocol (JSON-RPC 2.0 over stdio)
# ---------------------------------------------------------------------------


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _respond(id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": id, "result": result})


def _error(id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


# ---------------------------------------------------------------------------
# MCP sampling — delegate LLM calls to the host instead of calling API directly
# ---------------------------------------------------------------------------

_queued_requests: list[dict] = []


def _to_sampling_messages(messages: list[dict]) -> list[dict]:
    """Convert pact's internal message list to MCP sampling format."""
    out = []
    for m in messages:
        role = m.get("role", "user")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            text = "\n".join(parts)
        else:
            text = str(content)
        if text.strip():
            out.append({"role": role, "content": {"type": "text", "text": text}})
    return out


def _sample(messages: list[dict], max_tokens: int = 8192, system: str = "") -> str:
    """Send sampling/createMessage to the host LLM; block until response arrives."""
    sample_id = f"s-{uuid.uuid4().hex[:12]}"
    params: dict = {
        "messages": _to_sampling_messages(messages),
        "maxTokens": max_tokens,
    }
    if system:
        params["systemPrompt"] = system
    _send(
        {
            "jsonrpc": "2.0",
            "id": sample_id,
            "method": "sampling/createMessage",
            "params": params,
        }
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == sample_id:
            if "result" in msg:
                content = msg["result"].get("content", {})
                if isinstance(content, dict):
                    return content.get("text", "")
                return str(content)
            raise RuntimeError(f"Sampling error: {msg.get('error', '?')}")
        _queued_requests.append(msg)
    raise RuntimeError("stdin closed while waiting for sampling response")


@contextlib.contextmanager
def _sampling_backend():
    """Context manager: install _sample as the LLM backend for pact modules."""
    from . import llm as _llm

    _llm.set_sampling_backend(_sample)
    try:
        yield
    finally:
        _llm.clear_sampling_backend()


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "pact_topology",
        "description": (
            "Structural topology of a project's call graph — no LLM required. "
            "Returns cut vertices (articulation points whose removal disconnects the "
            "graph), betweenness centrality, strongly-connected components (call cycles), "
            "and module-level R.C. Martin instability/abstractness metrics. "
            "Use this first: cut_vertices are the load-bearing joints where contracts "
            "matter most. Feed cut_vertex names into pact_z3_verify."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Absolute path to project root",
                },
                "top_n": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max functions to return by betweenness (after cut vertices)",
                },
            },
            "required": ["root"],
        },
    },
    {
        "name": "pact_metrics",
        "description": (
            "R.C. Martin module-level coupling metrics — no LLM required. "
            "Returns instability (I=Ce/(Ca+Ce)), abstractness (A), and distance from "
            "the main sequence (D=|I+A-1|) per Python module. Zone-of-pain modules "
            "(D>0.5, I<0.3) are concrete AND stable — hardest to change, highest "
            "violation risk. Zone-of-uselessness modules (D>0.5, I>0.7) are abstract "
            "AND unstable — unused abstractions. Use to prioritise which modules to "
            "verify with pact_z3_verify or inspect with pact_check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Absolute path to project root",
                },
                "zone": {
                    "type": "string",
                    "enum": ["pain", "uselessness", "main", "all"],
                    "default": "all",
                    "description": "Filter by architectural zone",
                },
                "top_n": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max modules to return, sorted by distance from main sequence",
                },
            },
            "required": ["root"],
        },
    },
    {
        "name": "pact_z3_verify",
        "description": (
            "Z3-based formal contract verification — no LLM required. "
            "Runs the pact Z3 engine on a project root: extracts behavioral contracts "
            "from AST patterns (null-check ordering, field constraints, taint rules), "
            "encodes them as SMT2, and returns proved_safe | counterexample_found | "
            "unknown per contract. counterexample_found results include a concrete "
            "input that violates the contract — feed these into pact_check or "
            "use them to guide Hypothesis fuzzing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Absolute path to project root",
                },
            },
            "required": ["root"],
        },
    },
    {
        "name": "pact_context",
        "description": (
            "Extract violation signals from git history, CHANGES.rst, and inline "
            "TODO/FIXME comments for a source file. Returns confirmed past violations "
            "and fragile areas — these are the ground-truth intent signals that weight "
            "pact_find toward violations that actually matter to users."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to source file",
                },
                "repo_root": {
                    "type": "string",
                    "description": "Git repo root (auto-detected if omitted)",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "pact_find",
        "description": (
            "Property-driven violation discovery. Reads the source file, pulls git "
            "context as a prior, asks the model what inputs break the code, and runs "
            "Hypothesis to confirm. Returns violations with concrete counterexamples — "
            "not pattern categories. Feed output into pact_heal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to source file",
                },
                "use_context": {
                    "type": "boolean",
                    "default": True,
                    "description": "Pull git/changelog prior first",
                },
                "improve": {
                    "type": "boolean",
                    "default": False,
                    "description": "Rewrite find.md if hypothesis confirmation rate < 30%% (self-improvement)",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "pact_heal",
        "description": (
            "CEGIS program repair with impartial oracle. Takes a violations JSON "
            "(from pact_find or pact_check), synthesizes a minimal patch, and — if "
            "test_cmd is provided — runs the target project's test suite as the oracle. "
            "Exit 0 = accepted, non-zero = reverted + feedback fed back into next iter. "
            "Returns patches with oracle_confirmed flag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "violations_json": {
                    "type": "string",
                    "description": "JSON string from pact_find or pact_check",
                },
                "project_root": {
                    "type": "string",
                    "description": "Project root for oracle test_cmd",
                },
                "test_cmd": {
                    "type": "string",
                    "description": "Shell command to run as oracle (e.g. 'pytest tests/ -x -q')",
                },
                "severity": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "default": ["critical", "high"],
                },
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": "Apply oracle-confirmed patches to disk",
                },
            },
            "required": ["violations_json", "project_root"],
        },
    },
    {
        "name": "pact_check",
        "description": (
            "Fast static analysis — finds bare_except, json_loads_unguarded, "
            "subprocess_exit_code_unchecked, and 13 other violation modes via AST "
            "pattern matching. No LLM calls. Returns violations in pact_heal-compatible "
            "JSON format. Use when you want speed over semantic depth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory to check"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "pact_loop",
        "description": (
            "Full autonomous self-improvement loop. Runs measure → heal → check "
            "until convergence (PROVED_CLEAN | CONVERGED | STUCK | TIMEOUT). "
            "Uses Z3 Optimize for minimum-coverage fix ordering, PageRank for "
            "call-graph priority, and spec_learner for TLA+ gap recording. "
            "Returns a LoopResult with termination reason, violation trajectory, "
            "fitness history, and generated ADRs. Requires ANTHROPIC_API_KEY."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Absolute path to directory to improve",
                },
                "test_cmd": {
                    "type": "string",
                    "description": "Shell command to run as oracle (e.g. 'pytest tests/ -x -q')",
                    "default": "",
                },
                "max_iters": {
                    "type": "integer",
                    "description": "Maximum iterations before TIMEOUT (default 10 for MCP)",
                    "default": 10,
                },
                "severity": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["critical", "high"],
                    "description": "Violation severity levels to heal",
                },
                "verbose": {
                    "type": "boolean",
                    "default": False,
                    "description": "Emit detailed per-phase progress logs",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "pact_tda",
        "description": (
            "Topological Data Analysis scoring of violations against the call graph. "
            "For each violation, computes β₁ (first Betti number) of the k-hop "
            "neighborhood in the call graph — high-β₁ violations sit at structural "
            "bottlenecks that affect many callers. Returns violations re-ordered by "
            "topological severity. Requires a graphify-out/graph.json in the project "
            "root (run graphify on the project first if absent)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "violations_json": {
                    "type": "string",
                    "description": "JSON string from pact_check or pact_find",
                },
                "project_root": {
                    "type": "string",
                    "description": "Project root containing graphify-out/graph.json",
                },
                "hops": {
                    "type": "integer",
                    "default": 2,
                    "description": "Call-graph neighborhood radius for β₁ computation",
                },
            },
            "required": ["violations_json", "project_root"],
        },
    },
    {
        "name": "pact_sheaf",
        "description": (
            "Sheaf-cohomological LLM guard analysis. Checks that every LLM response "
            "site (json.loads, requests.get, llm() call) in the file has a guard "
            "that propagates to all downstream consumers. Unguarded sites have Ȟ¹≠0 "
            "— a non-trivial first cohomology group — and are reported as violations. "
            "No LLM calls. Pure static analysis. Returns h1_semantic, n_violations, "
            "and the list of unguarded sites."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to Python file to analyse",
                },
                "project_root": {
                    "type": "string",
                    "description": "Project root for cross-file guard transport (optional)",
                },
                "interprocedural": {
                    "type": "boolean",
                    "default": True,
                    "description": "Follow same-file call edges for guard propagation",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "pact_spec_learn",
        "description": (
            "TLA+ specification learning pipeline. Two modes: "
            "(1) 'record' — takes a bug description and runs the full pipeline "
            "(analyze_gap → propose_refinement → validate_refinement → save) to "
            "add a new training example to the spec_learner corpus. Use this when "
            "a real bug escaped the formal spec. "
            "(2) 'report' — returns a summary of all corpus records: verdicts, "
            "gap names, and which prompts need improvement. "
            "Requires ANTHROPIC_API_KEY for 'record' mode."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["record", "report"],
                    "description": "'record' to add a new gap; 'report' to summarize corpus",
                },
                "bug_description": {
                    "type": "string",
                    "description": "What went wrong (required for 'record' mode)",
                },
                "bug_file": {
                    "type": "string",
                    "description": "Path to the file where the bug occurred",
                },
                "bug_line": {
                    "type": "integer",
                    "default": 0,
                    "description": "Line number of the bug",
                },
                "bug_manifestation": {
                    "type": "string",
                    "description": "What the failure looked like at runtime",
                },
                "bug_fix": {
                    "type": "string",
                    "description": "What the correct fix is",
                },
                "tla_spec_path": {
                    "type": "string",
                    "description": "Path to the TLA+ spec file (defaults to PactLoop.tla)",
                },
            },
            "required": ["mode"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _tool_pact_context(params: dict) -> dict:
    from .context import extract_context

    file_path = Path(params["file_path"])
    repo_root = Path(params["repo_root"]) if params.get("repo_root") else None

    with _sampling_backend():
        return extract_context(file_path=file_path, repo_root=repo_root, api_key="")


def _tool_pact_find(params: dict) -> dict:
    from .find import find_violations

    file_path = Path(params["file_path"])

    with _sampling_backend():
        return find_violations(
            path=file_path,
            api_key="",
            use_context=params.get("use_context", True),
            improve=params.get("improve", False),
        )


def _tool_pact_heal(params: dict) -> dict:
    import dataclasses
    import tempfile

    from .heal import heal_project

    try:
        violations = json.loads(params["violations_json"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"violations_json is not valid JSON: {exc}") from exc
    project_root = Path(params["project_root"])

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(violations, f)
        tmp = Path(f.name)

    try:
        with _sampling_backend():
            result = heal_project(
                violations_path=tmp,
                api_key="",
                severity_filter=params.get("severity", ["critical", "high"]),
                apply=params.get("apply", False),
                test_cmd=params.get("test_cmd"),
                project_root=project_root,
            )
        return dataclasses.asdict(result)
    finally:
        tmp.unlink(missing_ok=True)


def _tool_pact_check(params: dict) -> dict:
    from .checker import check_codebase

    path = Path(params["path"])
    results = check_codebase(path)

    from collections import defaultdict

    by_file: dict = defaultdict(list)
    inv_templates = {
        "bare_except": "Exception handlers must be typed and must not silently swallow errors",
        "json_loads_unguarded": "json.loads() calls must be wrapped in try/except json.JSONDecodeError",
        "subprocess_exit_code_unchecked": "Subprocess invocations must check the return code",
    }

    for r in results:
        ctx = r.context
        inv_id = f"{ctx}_{Path(r.file).stem}"
        by_file[r.file].append(
            {
                "invariant_id": inv_id,
                "file": r.file,
                "line": r.line,
                "severity": (
                    "high"
                    if ctx in ("bare_except", "required_arg_missing")
                    else "medium"
                ),
                "evidence": r.call,
                "explanation": "; ".join(r.missing) if r.missing else "",
                "_inv_statement": inv_templates.get(ctx, f"Violation: {ctx}"),
            }
        )

    modules = []
    for fpath, viols in sorted(by_file.items()):
        seen_inv: dict = {}
        invs = []
        for v in viols:
            inv_id = v["invariant_id"]
            if inv_id not in seen_inv:
                invs.append(
                    {
                        "id": inv_id,
                        "type": v["invariant_id"].rsplit("_", 1)[0],
                        "statement": v.pop("_inv_statement"),
                        "severity": v["severity"],
                        "confidence": 0.85,
                    }
                )
                seen_inv[inv_id] = True
            else:
                v.pop("_inv_statement", None)

        modules.append(
            {
                "path": fpath,
                "invariants": invs,
                "violations": [
                    {k: val for k, val in v.items() if k != "_inv_statement"}
                    for v in viols
                ],
            }
        )

    return {"project": path.name, "generated_by": "pact.checker", "modules": modules}


def _tool_pact_tda(params: dict) -> dict:
    from .graphify_graph import CallGraph
    from .pact_tda import score_corpus

    project_root = Path(params["project_root"])
    graph_path = project_root / "graphify-out" / "graph.json"
    hops = params.get("hops", 2)

    try:
        violations = json.loads(params["violations_json"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"violations_json is not valid JSON: {exc}") from exc

    if not graph_path.exists():
        return {
            "error": f"graphify-out/graph.json not found at {graph_path}. "
            "Run graphify on the project first.",
            "scored_violations": [],
        }

    cg = CallGraph.load(project_root)
    if cg is None:
        return {"error": "Failed to load call graph", "scored_violations": []}

    # Flatten violations from pact_check/pact_find format into list of dicts
    flat: list[dict] = []
    for m in violations.get("modules", []):
        for v in m.get("violations", []):
            flat.append(v)

    scored = score_corpus(
        graph_path, project_root / "corpus.jsonl", hops=hops, top_n=len(flat) or 50
    )
    # scored returns list of dicts with topo metadata — merge with flat violations
    func_to_score: dict[str, dict] = {s.get("call", ""): s for s in scored}

    results = []
    for v in flat:
        call = v.get("evidence", "")
        topo = func_to_score.get(call, {})
        results.append(
            {
                **v,
                "topo_severity": topo.get("severity", 0.0),
                "beta1": topo.get("beta1", 0),
            }
        )

    results.sort(key=lambda x: x.get("topo_severity", 0.0), reverse=True)
    return {
        "project": project_root.name,
        "scored_violations": results,
        "n_violations": len(results),
    }


def _tool_pact_sheaf(params: dict) -> dict:
    import dataclasses

    from .pact_sheaf import check_file, sheaf_summary
    from .graphify_graph import CallGraph

    file_path = params["file_path"]
    project_root_str = params.get("project_root")
    interprocedural = params.get("interprocedural", True)

    cg = None
    if project_root_str:
        cg = CallGraph.load(Path(project_root_str))

    summary = sheaf_summary(file_path, call_graph=cg)
    violations = check_file(file_path, interprocedural=interprocedural, call_graph=cg)

    return {
        "file": file_path,
        "summary": summary,
        "violations": [dataclasses.asdict(v) for v in violations],
        "n_violations": len(violations),
    }


def _tool_pact_loop(params: dict) -> dict:
    import io as _io

    from .pact_loop import main as loop_main

    target = params["target"]
    test_cmd = params.get("test_cmd", "")
    max_iters = params.get("max_iters", 10)
    severity = params.get("severity", ["critical", "high"])
    verbose = params.get("verbose", False)

    argv = [target, "--max-iters", str(max_iters)]
    if test_cmd:
        argv += ["--test-cmd", test_cmd]
    for s in severity:
        argv += ["--severity", s]
    if verbose:
        argv.append("--verbose")

    buf = _io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        with _sampling_backend():
            exit_code = loop_main(argv)
    except SystemExit as exc:
        exit_code = exc.code or 0
    finally:
        sys.stdout = old_stdout

    return {"exit_code": exit_code, "output": buf.getvalue(), "target": target}


def _tool_pact_topology(params: dict) -> dict:

    import networkx as nx

    from .extractor import extract_from_codebase
    from .reduce import (
        _build_digraph,
        compute_module_metrics,
    )

    root = Path(params["root"])
    top_n = int(params.get("top_n", 20))

    _, functions, call_sites = extract_from_codebase(root)
    metrics = compute_module_metrics(root)

    # Build call graph
    G, func_by_name = _build_digraph(functions, call_sites)
    cut_verts: list[dict] = []
    top_by_btw: list[dict] = []
    sccs: list[dict] = []

    if G is not None:
        G_u = G.to_undirected()
        btw: dict[str, float] = nx.betweenness_centrality(G_u, normalized=True)
        cv_set: set[str] = set(nx.articulation_points(G_u))

        for name in sorted(cv_set, key=lambda n: btw.get(n, 0.0), reverse=True):
            f = func_by_name.get(name)
            if f is None:  # skip unresolved builtins/stdlib
                continue
            cut_verts.append(
                {
                    "function": name,
                    "file": f.file,
                    "betweenness": round(btw.get(name, 0.0), 4),
                }
            )

        ranked = sorted(
            (
                (n, b)
                for n, b in btw.items()
                if n not in cv_set and func_by_name.get(n) is not None
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        for name, b in ranked[:top_n]:
            f = func_by_name[name]
            top_by_btw.append(
                {
                    "function": name,
                    "file": f.file,
                    "betweenness": round(b, 4),
                }
            )

        for scc in nx.strongly_connected_components(G):
            if len(scc) > 1:
                sccs.append(sorted(scc))

    return {
        "root": str(root),
        "n_functions": len(functions),
        "cut_vertices": cut_verts,
        "top_by_betweenness": top_by_btw,
        "strongly_connected_components": sccs,
        "module_count": len(metrics),
        "note": (
            "cut_vertices are load-bearing joints — removal disconnects the graph. "
            "Feed these into pact_z3_verify to formally verify their contracts."
        ),
    }


def _tool_pact_metrics(params: dict) -> dict:
    from .reduce import compute_module_metrics

    root = Path(params["root"])
    zone_filter = params.get("zone", "all")
    top_n = int(params.get("top_n", 20))

    all_metrics = compute_module_metrics(root)
    # Drop isolated modules (Ca=Ce=0 → D=1.0 by formula, not architecturally meaningful)
    results = [m for m in all_metrics if (m.ca + m.ce) > 0]

    _ZONE_MAP = {
        "pain": "zone of pain",
        "uselessness": "zone of uselessness",
        "main": "main sequence",
    }
    if zone_filter != "all":
        target_zone = _ZONE_MAP.get(zone_filter, zone_filter)
        results = [m for m in results if m.zone == target_zone]

    results.sort(key=lambda m: m.distance, reverse=True)
    results = results[:top_n]

    return {
        "root": str(root),
        "modules": [
            {
                "module": m.module,
                "instability": round(m.instability, 3),
                "abstractness": round(m.abstractness, 3),
                "distance": round(m.distance, 3),
                "zone": m.zone,
                "ca": m.ca,
                "ce": m.ce,
            }
            for m in results
        ],
        "zone_filter": zone_filter,
        "note": (
            "zone=pain: concrete+stable (hard to change, high violation risk). "
            "zone=uselessness: abstract+unstable (unused abstractions). "
            "zone=main: well-balanced."
        ),
    }


def _tool_pact_z3_verify(params: dict) -> dict:
    import dataclasses

    from .z3_engine import run

    root = Path(params["root"])
    violations = run(root)

    return {
        "root": str(root),
        "violations": [dataclasses.asdict(v) for v in violations],
        "n_violations": len(violations),
        "note": (
            "counterexample_found violations include concrete inputs that break "
            "the contract — use these to seed pact_check or Hypothesis fuzzing."
        ),
    }


def _tool_pact_spec_learn(params: dict) -> dict:
    import dataclasses

    from .spec_learner import (
        SpecGapRecord,
        analyze_gap,
        load_corpus,
        propose_refinement,
        report,
        save,
        validate_refinement,
    )

    mode = params.get("mode", "report")

    if mode == "report":
        records = load_corpus()
        return {"corpus_size": len(records), "report": report(records)}

    # mode == "record"
    tla_path_str = params.get("tla_spec_path", "")
    tla_path = (
        Path(tla_path_str)
        if tla_path_str
        else (Path(__file__).parent / "docs" / "tla" / "PactLoop.tla")
    )
    tla_text = tla_path.read_text(encoding="utf-8") if tla_path.exists() else ""

    record = SpecGapRecord(
        bug_description=params.get("bug_description", ""),
        bug_file=params.get("bug_file", ""),
        bug_line=int(params.get("bug_line", 0)),
        bug_manifestation=params.get("bug_manifestation", ""),
        bug_fix=params.get("bug_fix", ""),
        tla_spec_path=str(tla_path),
        tla_spec_text=tla_text,
    )

    with _sampling_backend():
        record = analyze_gap(record, key="")
        record = propose_refinement(record, key="")
        record = validate_refinement(record, key="")
    save(record)

    return {
        "gap_name": record.gap_name,
        "verdict": record.verdict,
        "gap_confidence": record.gap_confidence,
        "validate_confidence": record.validate_confidence,
        "new_invariants": record.new_invariants,
        "verification_claim": record.verification_claim,
        "record": dataclasses.asdict(record),
    }


_DISPATCH = {
    # Zero-LLM structural tools — work without ANTHROPIC_API_KEY
    "pact_topology": _tool_pact_topology,
    "pact_metrics": _tool_pact_metrics,
    "pact_z3_verify": _tool_pact_z3_verify,
    "pact_check": _tool_pact_check,
    "pact_sheaf": _tool_pact_sheaf,
    "pact_tda": _tool_pact_tda,
    # LLM-assisted tools — require ANTHROPIC_API_KEY in environment
    "pact_context": _tool_pact_context,
    "pact_find": _tool_pact_find,
    "pact_heal": _tool_pact_heal,
    "pact_loop": _tool_pact_loop,
    "pact_spec_learn": _tool_pact_spec_learn,
}

# ---------------------------------------------------------------------------
# MCP handshake + main loop
# ---------------------------------------------------------------------------


def _handle(req: dict) -> None:
    rid = req.get("id")
    method = req.get("method", "")

    if method == "initialize":
        _respond(
            rid,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "prompts": {}, "sampling": {}},
                "serverInfo": {"name": "pact", "version": "0.1.0"},
            },
        )

    elif method == "tools/list":
        _respond(rid, {"tools": _TOOLS})

    elif method == "prompts/list":
        prompts = []
        for p in sorted(_PROMPTS_DIR.glob("*.md")):
            if p.stem.endswith("_improve"):
                continue  # internal — skip
            prompts.append(
                {
                    "name": p.stem,
                    "description": p.read_text(encoding="utf-8")
                    .splitlines()[0]
                    .lstrip("# ")
                    .strip(),
                    "arguments": [
                        {
                            "name": "context",
                            "description": "Extra context injected before the prompt",
                            "required": False,
                        }
                    ],
                }
            )
        _respond(rid, {"prompts": prompts})

    elif method == "prompts/get":
        params = req.get("params", {})
        name = params.get("name", "")
        context = (params.get("arguments") or {}).get("context", "")
        prompt_path = _PROMPTS_DIR / f"{name}.md"
        if not prompt_path.exists():
            _error(rid, -32602, f"Prompt not found: {name}")
            return
        text = prompt_path.read_text(encoding="utf-8")
        if context:
            text = f"{context}\n\n---\n\n{text}"
        _respond(
            rid,
            {
                "description": text.splitlines()[0].lstrip("# ").strip(),
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": text}}
                ],
            },
        )

    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        fn = _DISPATCH.get(name)
        if fn is None:
            _error(rid, -32601, f"Unknown tool: {name}")
            return
        try:
            result = fn(args)
            _respond(
                rid,
                {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            )
        except Exception as exc:
            _error(
                rid, -32603, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )

    elif method == "notifications/initialized":
        pass  # no response for notifications

    else:
        if rid is not None:
            _error(rid, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        # Drain requests that arrived during a sampling/createMessage round-trip
        while _queued_requests:
            _handle(_queued_requests.pop(0))
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": str(exc)},
                }
            )
            continue
        _handle(req)
    while _queued_requests:
        _handle(_queued_requests.pop(0))


if __name__ == "__main__":
    main()
