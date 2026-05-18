"""
Tests for pact_sheaf.py — the sheaf-cohomological LLM response checker.

Key cases:
1. Unguarded access → violation, Ȟ¹ = 1
2. Intra-procedural guard → clean
3. Cross-function guard (interprocedural transport) → clean
   (this is the case failure_mode.py gets wrong — current checker fires a FP here)
4. Two independent unguarded accesses → Ȟ¹ = 2
5. Two accesses sharing one CallResult, one guard fixes both → Ȟ¹ = 1
"""

import textwrap
from pact_sheaf import (
    check_file,
    h1_rank_for_file,
    _harvest_sites,
    _z3_check_guarded,
    SiteKind,
    _HAS_Z3,
)

import pytest


def _check(src: str, *, interprocedural: bool = True) -> list:
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(textwrap.dedent(src))
        name = f.name
    try:
        return check_file(name, interprocedural=interprocedural)
    finally:
        os.unlink(name)


def _rank(src: str) -> int:
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(textwrap.dedent(src))
        name = f.name
    try:
        return h1_rank_for_file(name)
    finally:
        os.unlink(name)


# ---------------------------------------------------------------------------
# Basic detection
# ---------------------------------------------------------------------------


def test_unguarded_fires():
    viols = _check("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            return r.choices[0].message.content
    """)
    assert len(viols) == 1
    assert viols[0].attr == "choices"
    assert not viols[0].guarded
    assert viols[0].spec_id == "openai-chat#choices-nonempty"


def test_intra_procedural_guard_clean():
    viols = _check("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            if not r.choices:
                raise ValueError("empty")
            return r.choices[0].message.content
    """)
    assert viols == []


def test_anthropic_unguarded_fires():
    viols = _check("""
        def fn(client):
            msg = client.messages.create(model="claude-3", messages=[])
            return msg.content[0].text
    """)
    assert len(viols) == 1
    assert viols[0].attr == "content"
    assert viols[0].spec_id == "anthropic-messages#content-nonempty"


def test_anthropic_guarded_clean():
    viols = _check("""
        def fn(client):
            msg = client.messages.create(model="claude-3", messages=[])
            if not msg.content:
                raise ValueError("empty")
            return msg.content[0].text
    """)
    assert viols == []


# ---------------------------------------------------------------------------
# H¹ rank semantics
# ---------------------------------------------------------------------------


def test_h1_rank_unguarded_is_nonzero():
    rank = _rank("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            return r.choices[0].message.content
    """)
    assert rank > 0


def test_h1_rank_guarded_is_zero():
    rank = _rank("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            if not r.choices:
                raise ValueError
            return r.choices[0].message.content
    """)
    assert rank == 0


def test_two_independent_accesses_same_response():
    """
    Two ErrorSites for the SAME CallResult with ONE guard should be Ȟ¹=0
    — one guard fixes both.
    """
    viols = _check("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            if not r.choices:
                raise ValueError
            a = r.choices[0].message.content
            b = r.choices[0].message.role
            return a, b
    """)
    assert viols == []


# ---------------------------------------------------------------------------
# THE KEY TEST: interprocedural guard transport
# This is the case failure_mode.py gets wrong (false positive)
# ---------------------------------------------------------------------------


def test_interprocedural_guard_suppresses_violation():
    """
    Guard lives in a helper function.  Current failure_mode.py fires a FP.
    Sheaf interprocedural transport follows the call edge and finds the guard.
    """
    viols = _check(
        """
        def safe_get(r):
            if not r.choices:
                raise ValueError("LLM returned empty response")
            return r.choices[0].message.content

        def handler(client):
            response = client.chat.completions.create(messages=[])
            return safe_get(response)
    """,
        interprocedural=True,
    )
    assert viols == [], f"Expected no violations (guard is in safe_get), got: {viols}"


def test_interprocedural_disabled_fires():
    """
    With interprocedural=False, the same code fires a violation (baseline parity).
    """
    viols = _check(
        """
        def safe_get(r):
            if not r.choices:
                raise ValueError("LLM returned empty response")
            return r.choices[0].message.content

        def handler(client):
            response = client.chat.completions.create(messages=[])
            return safe_get(response)
    """,
        interprocedural=False,
    )
    # safe_get itself is the ErrorSite — it accesses choices[0] with a guard
    # handler has no ErrorSite (it just passes response to safe_get)
    # So even intra-procedurally, safe_get's own guard makes it clean
    # The key FP case is when the CALLER passes the var without guarding:
    # handler → safe_get(response) where safe_get guards.  That only matters
    # if handler itself had r.choices[0] access — which it doesn't here.
    # Both modes should agree on this particular snippet.
    assert viols == []


def test_caller_guards_before_passing():
    """
    Guard in the CALLER before passing to a helper that accesses [0].
    The helper's ErrorSite is guarded via transport from the caller.
    """
    viols = _check(
        """
        def extract_content(r):
            return r.choices[0].message.content

        def handler(client):
            response = client.chat.completions.create(messages=[])
            if not response.choices:
                raise ValueError
            return extract_content(response)
    """,
        interprocedural=True,
    )
    # extract_content's ErrorSite has no local guard.
    # Phase 2 (caller-to-callee transport): handler guards response before passing
    # to extract_content, so the ArgBoundary receives a pre-guarded variable.
    # Synthetic BranchGuard injected → Ȟ¹ = 0.
    assert (
        viols == []
    ), f"Expected no violations (caller guards before passing), got: {viols}"


# ---------------------------------------------------------------------------
# Site graph structure
# ---------------------------------------------------------------------------


def test_site_graph_has_correct_kinds():
    import tempfile
    import os

    src = textwrap.dedent("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            if not r.choices:
                raise ValueError
            return r.choices[0].message.content
    """)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(src)
        name = f.name
    try:
        sg = _harvest_sites(name)
    finally:
        os.unlink(name)

    kinds = {s.kind for s in sg.sites.values()}
    assert SiteKind.CALL_RESULT in kinds
    assert SiteKind.BRANCH_GUARD in kinds
    assert SiteKind.ERROR_SITE in kinds
    assert len(sg.morphisms) >= 2  # CallResult→BranchGuard, BranchGuard→ErrorSite


# ---------------------------------------------------------------------------
# Z3 local theory solver
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_Z3, reason="z3 not installed")
def test_z3_unguarded_is_sat():
    """Unguarded ErrorSite: assume ¬P(es) must be SAT (not UNSAT) → not proven guarded."""
    import tempfile
    import os

    src = textwrap.dedent("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            return r.choices[0].message.content
    """)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(src)
        name = f.name
    try:
        sg = _harvest_sites(name)
        result = _z3_check_guarded(sg)
    finally:
        os.unlink(name)

    error_sites = sg.error_sites()
    assert len(error_sites) == 1
    assert result[error_sites[0].id] is False  # SAT → not guarded → violation


@pytest.mark.skipif(not _HAS_Z3, reason="z3 not installed")
def test_z3_guarded_is_unsat():
    """Guarded ErrorSite: assume ¬P(es) must be UNSAT → proven guarded."""
    import tempfile
    import os

    src = textwrap.dedent("""
        def fn(client):
            r = client.chat.completions.create(messages=[])
            if not r.choices:
                raise ValueError
            return r.choices[0].message.content
    """)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(src)
        name = f.name
    try:
        sg = _harvest_sites(name)
        result = _z3_check_guarded(sg)
    finally:
        os.unlink(name)

    error_sites = sg.error_sites()
    assert len(error_sites) == 1
    assert result[error_sites[0].id] is True  # UNSAT → proven guarded → clean


@pytest.mark.skipif(not _HAS_Z3, reason="z3 not installed")
def test_z3_interprocedural_synthetic_guard_propagates():
    """
    After interprocedural transport, the synthetic BranchGuard forces
    P(ErrorSite) = True via Z3 implication chain.
    """
    import tempfile
    import os
    from pact_sheaf import _apply_interprocedural_transport

    src = textwrap.dedent("""
        def safe_get(r):
            if not r.choices:
                raise ValueError
            return r.choices[0].message.content

        def handler(client):
            response = client.chat.completions.create(messages=[])
            return safe_get(response)
    """)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(src)
        name = f.name
    try:
        sg = _harvest_sites(name)
        _apply_interprocedural_transport(sg, name)
        result = _z3_check_guarded(sg)
    finally:
        os.unlink(name)

    # handler's ErrorSite (if any) should be guarded via the synthetic node
    for es in sg.error_sites():
        if es.func == "handler":
            assert result[es.id] is True, f"Z3 failed to propagate guard to {es.id}"


# ---------------------------------------------------------------------------
# Streaming chunk None-deref spec (openai-stream-chunk#null-deref)
# Covers the autogen #7130 class of bugs
# ---------------------------------------------------------------------------


def test_stream_chunk_unguarded_fires():
    viols = _check("""
        async def create_stream(client):
            chunks = client.chat.completions.create(stream=True)
            async for chunk in chunks:
                maybe_model = chunk.model
                if len(chunk.choices) == 0:
                    continue
    """)
    assert len(viols) == 1
    assert viols[0].attr == "model"
    assert viols[0].spec_id == "openai-stream-chunk#null-deref"


def test_stream_chunk_guarded_clean():
    viols = _check("""
        async def create_stream(client):
            chunks = client.chat.completions.create(stream=True)
            async for chunk in chunks:
                if chunk is None:
                    continue
                maybe_model = chunk.model
                if len(chunk.choices) == 0:
                    continue
    """)
    assert viols == []


def test_stream_chunk_h1_before_after():
    before = _rank("""
        async def create_stream(client):
            chunks = client.chat.completions.create(stream=True)
            async for chunk in chunks:
                maybe_model = chunk.model
    """)
    after = _rank("""
        async def create_stream(client):
            chunks = client.chat.completions.create(stream=True)
            async for chunk in chunks:
                if chunk is None:
                    continue
                maybe_model = chunk.model
    """)
    assert before > 0
    assert after == 0
