"""
Regression tests for structural-analysis bugs found during hardening.

Each test reproduces a concrete wrong result (false negative, false positive,
or miscounted topology) that pact produced before the fix.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from pact.z3_engine import PactEngine


def _z3_violations(tmp_path: Path, source: str) -> list:
    (tmp_path / "models.py").write_text(textwrap.dedent(source))
    engine = PactEngine()
    engine.load(tmp_path)
    return engine.violations()


# ── z3_engine: two create() calls on one line must not share a site ─────────


def test_z3_same_line_creates_do_not_mask_each_other(tmp_path):
    """A needs `name`, B needs `title`; each call passes the *other* model's
    field.  Keying sites on (file, line) merged both calls into one site that
    "provided" name+title, so Z3 proved the line safe — two false negatives."""
    viols = _z3_violations(
        tmp_path,
        """
        import django.db.models as m

        class A(m.Model):
            name = m.CharField(max_length=10)

        class B(m.Model):
            title = m.CharField(max_length=10)

        pair = (A.objects.create(title="t"), B.objects.create(name="n"))
        """,
    )
    found = sorted((v.call, tuple(v.missing)) for v in viols)
    assert found == [
        ("A.objects.create", ("name",)),
        ("B.objects.create", ("title",)),
    ]


# ── z3_engine: **kwargs spread means coverage is unknown ─────────────────────


def test_z3_var_kwargs_create_not_reported(tmp_path):
    """`A.objects.create(**data)` may supply every field; reporting
    "missing: name" is a false positive (failure_mode.py already skips these)."""
    viols = _z3_violations(
        tmp_path,
        """
        import django.db.models as m

        class A(m.Model):
            name = m.CharField(max_length=10)

        def make(data):
            return A.objects.create(**data)
        """,
    )
    assert viols == []


# ── pact_tda._beta1: undirected β₁ must not count a↔b twice ─────────────────


def test_beta1_mutual_recursion_is_not_a_cycle():
    """a→b and b→a are one undirected edge; β₁ of a single edge is 0.
    tda._nx_fallback already normalises edges, so the two β₁ implementations
    disagreed (1 vs 0) on every mutually-recursive pair."""
    import networkx as nx

    from pact.pact_tda import _beta1
    from pact.tda import _nx_fallback

    edges = {("a", "b"), ("b", "a")}
    assert _beta1(edges) == (2, 1, 0)
    assert _nx_fallback(nx.DiGraph(list(edges))).beta1 == 0


def test_beta1_triangle_with_back_edges_is_one_cycle():
    from pact.pact_tda import _beta1

    edges = {("a", "b"), ("b", "c"), ("c", "a"), ("b", "a"), ("c", "b")}
    assert _beta1(edges)[2] == 1
