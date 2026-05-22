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

    violations = json.loads(params["violations_json"])
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
