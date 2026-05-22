"""
pact MCP server -- exposes pact as MCP tools over stdio.

Tools:
  pact_context(file_path)              → git/changelog intent signals
  pact_find(file_path)                 → property violations + counterexamples
  pact_heal(violations_json, test_cmd) → patch + oracle result
  pact_check(path)                     → fast static violations

Usage:
    python -m pact.mcp_server
    # or via pyproject.toml entry point: pact-mcp
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

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
# Tool registry
# ---------------------------------------------------------------------------

_TOOLS = [
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
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _tool_pact_context(params: dict) -> dict:
    from .context import extract_context

    file_path = Path(params["file_path"])
    repo_root = Path(params["repo_root"]) if params.get("repo_root") else None
    api_key = _api_key()

    return extract_context(file_path=file_path, repo_root=repo_root, api_key=api_key)


def _tool_pact_find(params: dict) -> dict:
    from .find import find_violations

    file_path = Path(params["file_path"])
    api_key = _api_key()

    return find_violations(
        path=file_path,
        api_key=api_key,
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
    api_key = _api_key()

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(violations, f)
        tmp = Path(f.name)

    try:
        result = heal_project(
            violations_path=tmp,
            api_key=api_key,
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
    from .pact_loop import main as loop_main

    target = params["target"]
    test_cmd = params.get("test_cmd", "")
    max_iters = params.get("max_iters", 10)
    severity = params.get("severity", ["critical", "high"])
    verbose = params.get("verbose", False)
    api_key = _api_key()

    argv = [target, "--max-iters", str(max_iters)]
    if test_cmd:
        argv += ["--test-cmd", test_cmd]
    for s in severity:
        argv += ["--severity", s]
    if verbose:
        argv.append("--verbose")
    # Pass API key via environment (already set for _api_key() call)

    import io
    import sys
    import os

    old_env = os.environ.get("ANTHROPIC_API_KEY", "")
    os.environ["ANTHROPIC_API_KEY"] = api_key

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        exit_code = loop_main(argv)
    except SystemExit as exc:
        exit_code = exc.code or 0
    finally:
        sys.stdout = old_stdout
        if old_env:
            os.environ["ANTHROPIC_API_KEY"] = old_env

    output = buf.getvalue()
    return {
        "exit_code": exit_code,
        "output": output,
        "target": target,
    }


def _api_key() -> str:
    import os

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    return key


_DISPATCH = {
    "pact_context": _tool_pact_context,
    "pact_find": _tool_pact_find,
    "pact_heal": _tool_pact_heal,
    "pact_check": _tool_pact_check,
    "pact_loop": _tool_pact_loop,
    "pact_tda": _tool_pact_tda,
    "pact_sheaf": _tool_pact_sheaf,
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
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pact", "version": "0.1.0"},
            },
        )

    elif method == "tools/list":
        _respond(rid, {"tools": _TOOLS})

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


if __name__ == "__main__":
    main()
