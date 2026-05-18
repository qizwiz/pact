#!/usr/bin/env python3
"""
Upstream leverage analysis: which repos, when fixed, protect the most downstream code?

Reads the import cache built by import_graph.py and cross-references with the corpus
to produce a ranked list of high-leverage upstream fix targets.

Usage:
    python upstream_analysis.py [--corpus PATH] [--cache-dir PATH] [--top N]
"""

import argparse
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path

STDLIB = {
    "os",
    "sys",
    "re",
    "json",
    "time",
    "datetime",
    "typing",
    "pathlib",
    "collections",
    "itertools",
    "functools",
    "threading",
    "asyncio",
    "io",
    "abc",
    "copy",
    "math",
    "random",
    "logging",
    "warnings",
    "contextlib",
    "dataclasses",
    "enum",
    "inspect",
    "traceback",
    "hashlib",
    "base64",
    "urllib",
    "http",
    "socket",
    "struct",
    "pickle",
    "string",
    "textwrap",
    "tempfile",
    "shutil",
    "glob",
    "fnmatch",
    "subprocess",
    "signal",
    "weakref",
    "gc",
    "types",
    "importlib",
    "builtins",
    "__future__",
    "configparser",
    "argparse",
    "unittest",
    "pdb",
    "profile",
    "timeit",
    "queue",
    "multiprocessing",
    "concurrent",
    "heapq",
    "bisect",
    "array",
    "decimal",
    "fractions",
    "statistics",
    "uuid",
    "platform",
    "getpass",
    "operator",
    "pprint",
    "reprlib",
    "codecs",
    "unicodedata",
    "csv",
    "html",
    "xml",
    "email",
    "mimetypes",
    "shlex",
    "ast",
    "dis",
    "contextvars",
    "atexit",
    "yaml",
    "toml",
    "dotenv",
    "pytest",
    "secrets",
    "resource",
    "fcntl",
    "msvcrt",
    "difflib",
    "textwrap",
    "tokenize",
    "cgi",
    "cgitb",
    "code",
    "codeop",
    "compileall",
}

# Manually curated: package import name → GitHub repo
# These are packages commonly used in AI/LLM code
KNOWN_PKG_TO_REPO: dict[str, str] = {
    "litellm": "BerriAI/litellm",
    "langchain": "langchain-ai/langchain",
    "langchain_core": "langchain-ai/langchain",
    "langchain_community": "langchain-ai/langchain",
    "langchain_text_splitters": "langchain-ai/langchain",
    "langchain_openai": "langchain-ai/langchain",
    "langchain_anthropic": "langchain-ai/langchain",
    "langchain_google_genai": "langchain-ai/langchain",
    "langgraph": "langchain-ai/langgraph",
    "openai": "openai/openai-python",
    "anthropic": "anthropics/anthropic-sdk-python",
    "agentops": "AgentOps-AI/agentops",
    "llama_index": "run-llama/llama_index",
    "llama_cpp": "abetlen/llama-cpp-python",
    "transformers": "huggingface/transformers",
    "datasets": "huggingface/datasets",
    "swarms": "kyegomez/swarms",
    "crewai": "crewAIInc/crewAI",
    "chainlit": "Chainlit/chainlit",
    "instructor": "jxnl/instructor",
    "dspy": "stanfordnlp/dspy",
    "autogen": "microsoft/autogen",
    "pydantic_ai": "pydantic/pydantic-ai",
    "logfire": "pydantic/logfire",
    "gptcache": "zilliztech/GPTCache",
    "lightrag": "HKUDS/LightRAG",
    "ragas": "explodinggradients/ragas",
    "phoenix": "Arize-ai/phoenix",
    "aider": "Aider-AI/aider",
    "smolagents": "huggingface/smolagents",
    "open_webui": "open-webui/open-webui",
    "khoj": "khoj-ai/khoj",
}


def heuristic_pkg_to_repo(pkg: str, corpus_repos: set[str]) -> str | None:
    """Try to match a package name to a corpus repo by name similarity."""
    if pkg in KNOWN_PKG_TO_REPO:
        candidate = KNOWN_PKG_TO_REPO[pkg]
        if candidate in corpus_repos:
            return candidate
        return None
    # Try heuristic: match pkg to repo name (last component, normalized)
    for repo in corpus_repos:
        repo_name = repo.split("/")[-1].lower()
        repo_name = repo_name.replace("-", "_").replace(".", "_")
        # Strip common suffixes
        for suffix in ("-python", "-sdk", "-client", "-py", "-api", "_python", "_sdk"):
            if repo_name.endswith(suffix):
                repo_name = repo_name[: -len(suffix)]
        if pkg.lower() == repo_name:
            return repo
    return None


def main():
    parser = argparse.ArgumentParser(description="Upstream leverage analysis")
    parser.add_argument(
        "--corpus", default=os.path.expanduser("~/src/pact/corpus.jsonl")
    )
    parser.add_argument(
        "--cache-dir", default=os.path.expanduser("~/src/pact/import_cache")
    )
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)

    # Load corpus: violations per repo, stars per repo
    print(f"Loading corpus: {args.corpus}")
    repo_violations: Counter = Counter()
    repo_stars: dict[str, int] = {}
    repo_files: dict[str, set] = defaultdict(set)
    with open(args.corpus) as f:
        for line in f:
            d = json.loads(line)
            repo = d["repo"]
            repo_violations[repo] += 1
            repo_stars[repo] = d.get("stars", 0)
            repo_files[repo].add(d["file"])
    corpus_repos = set(repo_violations.keys())
    print(
        f"  {sum(repo_violations.values())} violations across {len(corpus_repos)} repos"
    )

    # Load import cache
    cache_files = [
        f
        for f in cache_dir.glob("*.json")
        if f.name
        not in ("branches.json", "import_graph_results.json", "upstream_analysis.json")
    ]
    print(f"  {len(cache_files)} cached file imports")

    # Build: pkg → set of downstream repos (corpus repos that import this pkg)
    pkg_downstream: dict[str, set] = defaultdict(set)
    for f in cache_files:
        d = json.loads(f.read_text())
        importer_repo = d.get("repo", "")
        for pkg in d.get("imports", []):
            if pkg and pkg not in STDLIB:
                pkg_downstream[pkg].add(importer_repo)

    # Resolve packages to corpus repos
    # repo → set of downstream corpus repos that import it
    repo_downstream: dict[str, set] = defaultdict(set)
    pkg_resolution: dict[str, str] = {}  # pkg → resolved corpus repo

    for pkg, downstream_repos in pkg_downstream.items():
        resolved = heuristic_pkg_to_repo(pkg, corpus_repos)
        if resolved:
            pkg_resolution[pkg] = resolved
            # Downstream repos: only those that are DIFFERENT from the upstream repo
            for dr in downstream_repos:
                if dr != resolved:
                    repo_downstream[resolved].add(dr)

    # Compute leverage scores
    # leverage = violations * log(1 + stars) * (1 + downstream_count)
    # This weights: how many violations to fix × how many stars (developers reading) × fan-out
    results = []
    for repo in corpus_repos:
        violations = repo_violations[repo]
        stars = repo_stars.get(repo, 0)
        downstream = len(repo_downstream.get(repo, set()))
        if violations == 0:
            continue
        leverage = violations * math.log1p(stars) * (1 + downstream)
        results.append(
            {
                "repo": repo,
                "violations": violations,
                "stars": stars,
                "downstream_repos": downstream,
                "leverage": leverage,
                "downstream_list": sorted(repo_downstream.get(repo, set())),
            }
        )

    results.sort(key=lambda x: -x["leverage"])

    print(f"\n{'Repo':<45} {'Viol':>5} {'Stars':>7} {'Dnstrm':>7} {'Leverage':>10}")
    print("-" * 80)
    for r in results[: args.top]:
        print(
            f"{r['repo']:<45} {r['violations']:>5} {r['stars']:>7,} "
            f"{r['downstream_repos']:>7} {r['leverage']:>10.1f}"
        )

    # Show which packages resolved to corpus repos
    print("\nResolved package→repo mappings used:")
    for pkg, repo in sorted(pkg_resolution.items(), key=lambda x: x[0]):
        downstream_count = len(repo_downstream.get(repo, set()))
        if downstream_count > 0:
            print(f"  import {pkg:<30} → {repo}  ({downstream_count} downstream)")

    # Save
    out_path = cache_dir / "upstream_analysis.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
