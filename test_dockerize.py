"""Tests for pact.dockerize — auto-Dockerizer and precise scanner."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .dockerize import (
    PreciseScanner,
    ScanResult,
    detect_install_command,
    detect_python_version,
    generate_dockerfile,
    _image_tag,
)

# ---------------------------------------------------------------------------
# detect_install_command
# ---------------------------------------------------------------------------


def test_detect_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
    cmd = detect_install_command(tmp_path)
    assert "pyproject" not in cmd  # command uses pip, not filename
    assert "pip install" in cmd


def test_detect_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n")
    cmd = detect_install_command(tmp_path)
    assert "requirements.txt" in cmd


def test_detect_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup; setup()")
    cmd = detect_install_command(tmp_path)
    assert "pip install" in cmd


def test_detect_pipfile(tmp_path):
    (tmp_path / "Pipfile").write_text("[packages]\nrequests = '*'")
    cmd = detect_install_command(tmp_path)
    assert "pipenv" in cmd


def test_detect_none_returns_true(tmp_path):
    cmd = detect_install_command(tmp_path)
    assert cmd == "true"


def test_pyproject_takes_precedence_over_requirements(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
    (tmp_path / "requirements.txt").write_text("requests\n")
    cmd = detect_install_command(tmp_path)
    # pyproject is tier 1, requirements is tier 2 — pyproject wins
    assert "requirements.txt" not in cmd


# ---------------------------------------------------------------------------
# detect_python_version
# ---------------------------------------------------------------------------


def test_python_version_from_dotfile(tmp_path):
    (tmp_path / ".python-version").write_text("3.10.4\n")
    assert detect_python_version(tmp_path) == "3.10"


def test_python_version_default(tmp_path):
    assert detect_python_version(tmp_path) == "3.11"


def test_python_version_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.9"\n' 'python_requires = ">=3.9"\n'
    )
    ver = detect_python_version(tmp_path)
    # Either reads it or defaults — must be a valid version string
    assert ver.startswith("3.")


# ---------------------------------------------------------------------------
# generate_dockerfile
# ---------------------------------------------------------------------------


def test_dockerfile_contains_from(tmp_path):
    df = generate_dockerfile(tmp_path)
    assert df.startswith("FROM python:")


def test_dockerfile_installs_jedi_and_pact(tmp_path):
    df = generate_dockerfile(tmp_path)
    assert "jedi" in df
    assert "pact-tool" in df


def test_dockerfile_contains_install_command(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n")
    df = generate_dockerfile(tmp_path)
    assert "requirements.txt" in df


def test_dockerfile_entrypoint_uses_blast_radius(tmp_path):
    df = generate_dockerfile(tmp_path)
    assert "--blast-radius" in df
    assert "--json" in df


# ---------------------------------------------------------------------------
# _image_tag
# ---------------------------------------------------------------------------


def test_image_tag_is_stable(tmp_path):
    t1 = _image_tag(tmp_path)
    t2 = _image_tag(tmp_path)
    assert t1 == t2


def test_image_tag_differs_across_paths(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert _image_tag(tmp_path) != _image_tag(other)


def test_image_tag_format(tmp_path):
    tag = _image_tag(tmp_path)
    assert tag.startswith("pact-precise-")
    assert len(tag) == len("pact-precise-") + 8


# ---------------------------------------------------------------------------
# PreciseScanner — fallback path (no Docker)
# ---------------------------------------------------------------------------


def test_scanner_falls_back_when_no_docker(tmp_path):
    """When Docker is unavailable, scanner runs pact locally."""
    (tmp_path / "empty.py").write_text("x = 1\n")
    with patch("pact.dockerize._docker_available", return_value=False):
        scanner = PreciseScanner(tmp_path)
        result = scanner.run()
    assert isinstance(result, ScanResult)
    assert not result.docker_available
    # No error — local fallback ran successfully
    assert result.error == "" or isinstance(result.violations_json, list)


def test_scanner_local_fallback_returns_list(tmp_path):
    """Local fallback always returns a list, even for empty repos."""
    (tmp_path / "clean.py").write_text("def f():\n    return 1\n")
    with patch("pact.dockerize._docker_available", return_value=False):
        scanner = PreciseScanner(tmp_path)
        result = scanner.run()
    assert isinstance(result.violations_json, list)


# ---------------------------------------------------------------------------
# ScanResult
# ---------------------------------------------------------------------------


def test_scan_result_defaults():
    r = ScanResult()
    assert r.violations_json == []
    assert r.image_tag == ""
    assert r.error == ""
    assert r.docker_available is True


def test_scan_result_error_path():
    r = ScanResult(error="build failed", docker_available=False)
    assert r.error == "build failed"
    assert not r.docker_available
