#!/usr/bin/env python3
"""
Build an import dependency graph from the pact corpus.

For each unique (repo, file) pair with llm_response_unguarded violations,
fetches the top 60 lines via GitHub raw content, extracts import statements,
and ranks packages by downstream exposure (how many vulnerable files import them).

Usage:
    python import_graph.py [--limit N] [--corpus PATH] [--cache-dir PATH]
"""

import argparse
import json
import os
import re
import time
from collections import Counter
from pathlib import Path


def get_default_branch(repo: str, token: str, session) -> str:
    url = f"https://api.github.com/repos/{repo}"
    r = session.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    if r.status_code == 200:
        return r.json().get("default_branch", "main")
    return "main"


def fetch_file_head(
    repo: str, file_path: str, branch: str, token: str, session, n_lines: int = 60
) -> list[str] | None:
    """Fetch first n_lines of a file via raw GitHub content."""
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{file_path}"
    r = session.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if r.status_code != 200:
        return None
    lines = r.text.splitlines()
    return lines[:n_lines]


def extract_imports(lines: list[str]) -> list[str]:
    """Extract top-level package names from import statements."""
    packages = []
    for line in lines:
        line = line.strip()
        # import foo, import foo.bar, import foo as bar
        m = re.match(r"^import\s+([\w.]+)", line)
        if m:
            pkg = m.group(1).split(".")[0]
            packages.append(pkg)
            continue
        # from foo import bar, from foo.bar import baz
        m = re.match(r"^from\s+([\w.]+)\s+import", line)
        if m:
            pkg = m.group(1).split(".")[0]
            packages.append(pkg)
    return packages


def repo_to_package_hints(repos: set[str]) -> dict[str, str]:
    """Heuristic: map repo name → likely pip package name."""
    mapping = {}
    transforms = [
        ("-python", ""),
        ("-sdk", ""),
        ("-client", ""),
        ("-py", ""),
        ("-api", ""),
    ]
    for repo in repos:
        name = repo.split("/")[-1]
        for old, new in transforms:
            name = name.replace(old, new)
        pkg = name.replace("-", "_").lower()
        mapping[pkg] = repo
    return mapping


def main():
    parser = argparse.ArgumentParser(description="Build import graph from pact corpus")
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max (repo,file) pairs to fetch (default 500)",
    )
    parser.add_argument(
        "--corpus", default=os.path.expanduser("~/src/pact/corpus.jsonl")
    )
    parser.add_argument(
        "--cache-dir", default=os.path.expanduser("~/src/pact/import_cache")
    )
    parser.add_argument(
        "--rate-limit", type=float, default=3.0, help="Requests per second (default 3)"
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN env var required")

    import requests

    session = requests.Session()

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Load corpus
    print(f"Loading corpus: {args.corpus}")
    pairs: dict[tuple[str, str], None] = {}
    all_repos: set[str] = set()
    with open(args.corpus) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                import warnings

                warnings.warn(f"Skipping malformed JSON line: {e}")
                continue
            pairs[(d["repo"], d["file"])] = None
            all_repos.add(d["repo"])

    unique_pairs = list(pairs.keys())
    print(
        f"  {len(unique_pairs)} unique (repo, file) pairs across {len(all_repos)} repos"
    )

    # Limit
    if args.limit and len(unique_pairs) > args.limit:
        unique_pairs = unique_pairs[: args.limit]
        print(f"  Limiting to {args.limit} pairs")

    # Cache default branches
    branch_cache_path = cache_dir / "branches.json"
    branch_cache: dict[str, str] = {}
    if branch_cache_path.exists():
        try:
            branch_cache = json.loads(branch_cache_path.read_text())
        except json.JSONDecodeError:
            branch_cache = {}

    # Fetch imports
    package_counter: Counter = Counter()
    file_imports: dict[tuple[str, str], list[str]] = {}
    delay = 1.0 / args.rate_limit

    fetch_errors = 0
    for i, (repo, file_path) in enumerate(unique_pairs):
        cache_key = f"{repo}/{file_path}".replace("/", "__")
        cache_file = cache_dir / f"{cache_key}.json"

        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
            except json.JSONDecodeError as e:
                import warnings

                warnings.warn(f"Corrupt cache file {cache_file}: {e}; ignoring.")
                data = {}
            imports = data.get("imports", [])
        else:
            # Get default branch
            if repo not in branch_cache:
                branch_cache[repo] = get_default_branch(repo, token, session)
                branch_cache_path.write_text(json.dumps(branch_cache, indent=2))
                time.sleep(delay)

            branch = branch_cache[repo]
            lines = fetch_file_head(repo, file_path, branch, token, session)
            time.sleep(delay)

            if lines is None:
                fetch_errors += 1
                imports = []
            else:
                imports = extract_imports(lines)

            cache_file.write_text(
                json.dumps({"repo": repo, "file": file_path, "imports": imports})
            )

        file_imports[(repo, file_path)] = imports
        for pkg in imports:
            package_counter[pkg] += 1

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(unique_pairs)}] fetch_errors={fetch_errors}")

    print(f"\nDone. fetch_errors={fetch_errors}")
    print("\nTop 50 packages imported by vulnerable files:")
    print(f"{'Package':<35} {'Files':>8} {'% of corpus':>12}")
    print("-" * 60)
    total = len(unique_pairs)
    for pkg, count in package_counter.most_common(50):
        pct = 100 * count / total
        print(f"{pkg:<35} {count:>8} {pct:>11.1f}%")

    # Cross-reference: which corpus repos match these package names?
    pkg_to_repo = repo_to_package_hints(all_repos)
    print("\nCorpus repos that appear as upstream imports:")
    print(f"{'Package':<25} {'Repo':<50} {'Downstream files':>16}")
    print("-" * 95)
    for pkg, count in package_counter.most_common(200):
        if pkg in pkg_to_repo:
            print(f"{pkg:<25} {pkg_to_repo[pkg]:<50} {count:>16}")

    # Save results
    results_path = cache_dir / "import_graph_results.json"
    results_path.write_text(
        json.dumps(
            {
                "top_packages": package_counter.most_common(100),
                "total_pairs": len(unique_pairs),
                "fetch_errors": fetch_errors,
            },
            indent=2,
        )
    )
    print(f"\nResults saved to {results_path}")


if __name__ == "__main__":
    main()
