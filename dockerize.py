"""
pact.dockerize — auto-Dockerize a Python repo and run precise analysis inside it.

The precision problem: pact's AST-derived call graph has false edges because
method calls on unresolved types generate edges to every class that defines
that method.  Installing a project's dependencies and running Jedi inside the
installed environment resolves this: Jedi uses the actual installed types, so
`obj.method()` resolves to exactly the class(es) that `obj` can be at that
call site.

This module:
  1. Detects the package manager from repo files (pyproject.toml, requirements.txt,
     setup.py, Pipfile) — four tiers, best-effort fallback.
  2. Generates a minimal Dockerfile that installs deps + pact + jedi.
  3. Builds and runs the image, capturing precise violation output with blast radii.
  4. Cleans up the image after use.

The goal is not a runnable application container — it's a reproducible analysis
environment.  If pip install partially fails, Jedi still resolves what did install.
The analysis degrades gracefully to AST-only for packages that couldn't be resolved.

Usage
-----
    from pact.dockerize import PreciseScanner

    scanner = PreciseScanner("/path/to/repo")
    results = scanner.run()          # returns list[ViolationWithBlast]
    print(scanner.fitness.summary()) # GraphFitness for the precise graph

CLI
---
    python3 -m pact.dockerize path/to/repo [--keep-image] [--no-blast]
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------

_INSTALL_TIERS: list[tuple[str, str]] = [
    # (sentinel_file, install_command)
    (
        "pyproject.toml",
        "pip install --quiet -e . 2>/dev/null || pip install --quiet . 2>/dev/null || true",
    ),
    ("requirements.txt", "pip install --quiet -r requirements.txt 2>/dev/null || true"),
    ("setup.py", "pip install --quiet -e . 2>/dev/null || true"),
    ("setup.cfg", "pip install --quiet -e . 2>/dev/null || true"),
    (
        "Pipfile",
        "pip install --quiet pipenv && pipenv install --system --skip-lock 2>/dev/null || true",
    ),
]


def detect_install_command(repo_root: Path) -> str:
    """Return the best pip install command for this repo, or 'true' if unknown."""
    for sentinel, cmd in _INSTALL_TIERS:
        if (repo_root / sentinel).exists():
            return cmd
    return "true"


def detect_python_version(repo_root: Path) -> str:
    """Infer required Python version from pyproject.toml or .python-version."""
    pv = repo_root / ".python-version"
    if pv.exists():
        ver = pv.read_text().strip().split(".")
        if len(ver) >= 2:
            return f"{ver[0]}.{ver[1]}"

    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        import re

        m = re.search(r'python_requires\s*=\s*["\']>=\s*(\d+\.\d+)', text)
        if m:
            return m.group(1)

    return "3.11"  # safe default — supported by Jedi and pyright


# ---------------------------------------------------------------------------
# Dockerfile generation
# ---------------------------------------------------------------------------

_DOCKERFILE_TEMPLATE = """\
FROM python:{python_version}-slim

# System deps for native extensions (numpy, scipy, etc.)
RUN apt-get update -qq && apt-get install -y --no-install-recommends \\
    git gcc g++ make libffi-dev libssl-dev && \\
    rm -rf /var/lib/apt/lists/*

WORKDIR /repo
COPY . .

# Install the repo's own dependencies (best-effort — partial install is fine)
RUN {install_command}

# Install analysis tools
RUN pip install --quiet jedi networkx pact-tool 2>/dev/null || true

# Analysis entrypoint: run pact with blast-radius, output JSON
ENTRYPOINT ["python3", "-m", "pact.cli", "/repo", "--json", "--blast-radius"]
"""


def generate_dockerfile(repo_root: Path) -> str:
    return _DOCKERFILE_TEMPLATE.format(
        python_version=detect_python_version(repo_root),
        install_command=detect_install_command(repo_root),
    )


# ---------------------------------------------------------------------------
# Jedi-enhanced extractor (runs inside the Docker container)
# ---------------------------------------------------------------------------

_JEDI_RESOLVER_SCRIPT = """\
\"\"\"
Jedi call-site resolver — runs INSIDE the Docker container where deps are installed.

For each call site (file, line, col), asks Jedi where the callee is defined.
Returns a JSON mapping of (file, line, col) → (def_file, def_name) for resolved sites.
Input: JSON list of {file, line, col, caller, callee} from pact's extractor.
Output: JSON list of the same with added {resolved_file, resolved_name} fields.
\"\"\"
import json, sys, os
sys.path.insert(0, "/repo")

try:
    import jedi
    jedi.settings.fast_parser = False  # more accurate, tolerable at batch scale
    _HAS_JEDI = True
except ImportError:
    _HAS_JEDI = False

call_sites = json.load(sys.stdin)
results = []

for cs in call_sites:
    resolved = dict(cs)
    if _HAS_JEDI and cs.get("file") and cs.get("line") and cs.get("col") is not None:
        try:
            src = open(cs["file"]).read()
            script = jedi.Script(source=src, path=cs["file"])
            defs = script.goto(line=cs["line"], column=cs["col"])
            if defs:
                d = defs[0]
                resolved["resolved_file"] = str(d.module_path or "")
                resolved["resolved_name"] = d.full_name or d.name or ""
        except Exception:
            pass  # degrade gracefully to AST-only for this site
    results.append(resolved)

print(json.dumps(results))
"""


# ---------------------------------------------------------------------------
# Docker runner
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    violations_json: list[dict] = field(default_factory=list)
    image_tag: str = ""
    error: str = ""
    docker_available: bool = True


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _image_tag(repo_root: Path) -> str:
    h = hashlib.sha1(str(repo_root.resolve()).encode()).hexdigest()[:8]
    return f"pact-precise-{h}"


def build_image(repo_root: Path, tag: str | None = None) -> tuple[str, bool]:
    """Build a Docker image for precise analysis of repo_root.

    Returns (image_tag, success).  On failure returns (tag, False).
    """
    if not _docker_available():
        return "", False

    tag = tag or _image_tag(repo_root)
    dockerfile_content = generate_dockerfile(repo_root)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix="Dockerfile", dir=repo_root, delete=False
    ) as f:
        f.write(dockerfile_content)
        dockerfile_path = f.name

    try:
        result = subprocess.run(
            ["docker", "build", "-f", dockerfile_path, "-t", tag, str(repo_root)],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min build timeout
        )
        return tag, result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return tag, False
    finally:
        os.unlink(dockerfile_path)


def run_analysis(tag: str, timeout: int = 120) -> ScanResult:
    """Run pact --json --blast-radius inside the built image."""
    if not _docker_available():
        return ScanResult(docker_available=False, error="docker not found")

    try:
        result = subprocess.run(
            ["docker", "run", "--rm", tag],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0 and not result.stdout.strip():
            return ScanResult(error=result.stderr[:500])
        try:
            data = json.loads(result.stdout)
            return ScanResult(violations_json=data, image_tag=tag)
        except json.JSONDecodeError:
            return ScanResult(error=f"non-JSON output: {result.stdout[:200]}")
    except subprocess.TimeoutExpired:
        return ScanResult(error=f"analysis timed out after {timeout}s")


def remove_image(tag: str) -> None:
    """Remove the analysis image (cleanup)."""
    if _docker_available():
        result = subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
        if result.returncode != 0:
            import warnings

            warnings.warn(
                f"docker rmi {tag} failed (exit {result.returncode})",
                RuntimeWarning,
                stacklevel=2,
            )


# ---------------------------------------------------------------------------
# High-level scanner
# ---------------------------------------------------------------------------


class PreciseScanner:
    """Run pact with Jedi-resolved call graph inside an auto-Dockerized repo.

    Usage:
        scanner = PreciseScanner("/path/to/cloned/repo")
        violations = scanner.run()

    The scanner builds a Docker image for the repo, installs deps, runs pact
    with Jedi inside the container, returns violations ranked by blast radius.
    Cleans up the image unless keep_image=True.
    """

    def __init__(
        self,
        repo_root: str | Path,
        keep_image: bool = False,
        timeout: int = 180,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.keep_image = keep_image
        self.timeout = timeout
        self._tag: str = ""
        self._result: ScanResult | None = None

    def run(self) -> ScanResult:
        if not _docker_available():
            print(
                "[pact/dockerize] Docker not available — falling back to local scan",
                file=sys.stderr,
            )
            return self._local_fallback()

        print(
            f"[pact/dockerize] building image for {self.repo_root.name} …",
            file=sys.stderr,
        )
        tag, ok = build_image(self.repo_root)
        self._tag = tag

        if not ok:
            print(
                "[pact/dockerize] image build failed — falling back to local scan",
                file=sys.stderr,
            )
            if tag and not self.keep_image:
                remove_image(tag)
            return self._local_fallback()

        print(f"[pact/dockerize] running analysis in {tag} …", file=sys.stderr)
        result = run_analysis(tag, timeout=self.timeout)
        self._result = result

        if not self.keep_image:
            remove_image(tag)

        return result

    def _local_fallback(self) -> ScanResult:
        """Run pact locally without Docker (AST-only, current behaviour)."""
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pact.cli", str(self.repo_root), "--json"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"pact exited {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:200]}"
                )
            data = json.loads(proc.stdout) if proc.stdout.strip() else []
            return ScanResult(violations_json=data, docker_available=False)
        except Exception as e:
            return ScanResult(error=str(e), docker_available=False)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    import argparse

    p = argparse.ArgumentParser(
        description="pact precise scanner — auto-Dockerize a repo and run pact+Jedi inside it"
    )
    p.add_argument("repo", help="Path to cloned repository root")
    p.add_argument(
        "--keep-image",
        action="store_true",
        help="Do not remove Docker image after scan",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Analysis timeout in seconds (default: 180)",
    )
    p.add_argument(
        "--dockerfile-only",
        action="store_true",
        help="Print generated Dockerfile and exit",
    )
    args = p.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"error: {repo_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.dockerfile_only:
        print(generate_dockerfile(repo_root))
        return

    scanner = PreciseScanner(
        repo_root, keep_image=args.keep_image, timeout=args.timeout
    )
    result = scanner.run()

    if result.error:
        print(f"[pact/dockerize] error: {result.error}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result.violations_json, indent=2))
    print(
        f"\n[pact/dockerize] {len(result.violations_json)} violation(s)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
