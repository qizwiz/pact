"""
pact must work when installed as a package, i.e. without its own directory on
sys.path. CI runs with PYTHONPATH=. which makes bare `from ast_utils import`
style imports resolve by accident; these tests run in a clean interpreter
with only the package's parent directory on sys.path.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent
_PKG = _PKG_DIR.name


def _run_isolated(code: str, tmp_path: Path) -> str:
    script = tmp_path / "probe.py"
    script.write_text(
        f"import sys\nsys.path.insert(0, {str(_PKG_DIR.parent)!r})\n"
        + textwrap.dedent(code)
    )
    r = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout


def test_pact_tda_importable_as_package_module(tmp_path):
    out = _run_isolated(
        f"""
        from {_PKG} import pact_tda
        print("ok", pact_tda.score_function.__name__)
        """,
        tmp_path,
    )
    assert "ok score_function" in out


_SHEAF_PROBE = """
    import json, textwrap, warnings
    from pathlib import Path
    from {pkg}.pact_sheaf import sheaf_summary
    from {pkg}.graphify_graph import CallGraph

    root = Path({root!r})
    f = root / "svc.py"
    f.write_text(textwrap.dedent('''
        def fn(client):
            a(client)
            r = client.chat.completions.create(messages=[])
            return r.choices[0].message.content

        def a(c):
            return b(c)

        def b(c):
            return d(c)

        def d(c):
            return fn(c)
    '''))
    warnings.simplefilter("ignore")
    s = sheaf_summary(str(f), call_graph=CallGraph._from_ast(root))
    print(json.dumps({{k: s.get(k) for k in ("tda_beta1_max", "spectral_gap")}}))
"""


def _sheaf(tmp_path):
    import json

    proj = tmp_path / "proj"
    proj.mkdir()
    out = _run_isolated(_SHEAF_PROBE.format(pkg=_PKG, root=str(proj)), tmp_path)
    return json.loads(out.strip().splitlines()[-1])


def test_sheaf_callgraph_beta1_computed_in_package_mode(tmp_path):
    # fn -> a -> b -> d -> fn is one independent cycle: beta1 = 4 - 4 + 1 = 1
    assert _sheaf(tmp_path)["tda_beta1_max"] == 1


def test_sheaf_spectral_gap_computed_from_call_graph(tmp_path):
    pytest.importorskip("scipy")
    gap = _sheaf(tmp_path)["spectral_gap"]
    assert gap is not None
