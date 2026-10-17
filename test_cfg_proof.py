"""Tests for pact_cfg_proof.prove_loop_guard — Z3 CFG reachability proof."""

from __future__ import annotations

import textwrap

import pytest

pytest.importorskip("z3")

from .pact_cfg_proof import prove_loop_guard  # noqa: E402


def _prove(tmp_path, src):
    p = tmp_path / "m.py"
    p.write_text(textwrap.dedent(src))
    return prove_loop_guard(str(p), "f", "chunk")


def test_guarded_access_is_proved_safe(tmp_path):
    r = _prove(
        tmp_path,
        """
        async def f(stream):
            async for chunk in stream:
                if chunk is None:
                    continue
                print(chunk.choices)
        """,
    )
    assert r.proved, r
    assert r.access_sites


def test_unguarded_access_is_unsafe(tmp_path):
    r = _prove(
        tmp_path,
        """
        async def f(stream):
            async for chunk in stream:
                print(chunk.choices)
        """,
    )
    assert not r.proved, r


def test_access_in_if_condition_is_checked(tmp_path):
    """`if chunk.choices:` dereferences chunk before any guard."""
    r = _prove(
        tmp_path,
        """
        async def f(stream):
            async for chunk in stream:
                if chunk.choices:
                    print("x")
        """,
    )
    assert not r.proved, r


def test_access_in_else_of_none_check_is_checked(tmp_path):
    """The else of `if chunk is not None: continue` runs only when chunk is None."""
    r = _prove(
        tmp_path,
        """
        async def f(stream):
            async for chunk in stream:
                if chunk is not None:
                    continue
                else:
                    print(chunk.choices)
        """,
    )
    assert not r.proved, r
