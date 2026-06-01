"""Tests for instant_apply (local merge) and heal's zero-LLM tier-0 path."""

from __future__ import annotations

from pact.instant_apply import (
    DEDENT_REPLACE,
    EXACT_REPLACE,
    FUZZY_REPLACE,
    NONE,
    _chunks,
    instant_apply,
)


class TestInstantApplyPhase1:
    def test_exact_substring_replace(self):
        src = "x = 1\ny = 2\nz = 3\n"
        res = instant_apply(src, "y = 2\n", "y = 22\n")
        assert res.strategy == EXACT_REPLACE
        assert res.confidence == 1.0
        assert res.merged == "x = 1\ny = 22\nz = 3\n"

    def test_dedent_block_replace_reindents(self):
        src = "def f():\n    try:\n        go()\n    except:\n        pass\n"
        # original given without the surrounding indentation
        res = instant_apply(src, "except:\n    pass", "except Exception:\n    raise")
        assert res.strategy == DEDENT_REPLACE
        assert res.confidence == 0.98
        assert "    except Exception:\n        raise" in res.merged

    def test_empty_original_is_unmergeable(self):
        res = instant_apply("a = 1\n", "   ", "b = 2\n")
        assert res.merged is None
        assert res.strategy == NONE

    def test_escaped_newlines_normalized(self):
        src = "a = 1\nb = 2\n"
        res = instant_apply(src, "a = 1\\nb = 2\\n", "a = 10\\nb = 20\\n")
        assert res.merged == "a = 10\nb = 20\n"


class TestInstantApplyFuzzy:
    def test_fuzzy_replace_hits_most_similar_chunk(self):
        src = (
            "def alpha():\n    return 1\n\n"
            "def beta(x):\n    y = x + 1\n    return y\n\n"
            "def gamma():\n    return 3\n"
        )
        # Near-miss of beta's body (whitespace/comment drift) — not verbatim.
        original = "def beta(x):\n    y = x + 1  # compute\n    return y"
        replacement = "def beta(x):\n    y = x + 2\n    return y"
        res = instant_apply(src, original, replacement, min_confidence=0.6)
        assert res.strategy == FUZZY_REPLACE
        assert res.confidence >= 0.6
        assert "y = x + 2" in res.merged
        # untouched neighbours preserved
        assert "def alpha():" in res.merged and "def gamma():" in res.merged

    def test_fuzzy_miss_below_threshold_returns_none_with_best_ratio(self):
        src = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
        res = instant_apply(
            src,
            "def totally_different():\n    raise SystemExit",
            "x",
            min_confidence=0.9,
        )
        assert res.merged is None
        assert res.strategy == NONE
        assert 0.0 <= res.confidence < 0.9


class TestChunks:
    def test_chunks_fold_in_decorators(self):
        src = "@deco\ndef f():\n    return 1\n\nclass C:\n    pass\n"
        chunks = _chunks(src)
        texts = [c.text for c in chunks]
        assert any(t.startswith("@deco\ndef f():") for t in texts)
        assert any(t.startswith("class C:") for t in texts)

    def test_chunks_empty_on_syntax_error(self):
        assert _chunks("def (:\n") == []


class TestApplyPatchSmart:
    def test_exact_passthrough(self):
        from pact.heal import apply_patch_smart

        merged, conf, strat = apply_patch_smart("a = 1\n", "a = 1\n", "a = 2\n")
        assert merged == "a = 2\n"
        assert conf == 1.0 and strat == "exact"

    def test_fuzzy_fallback_when_not_verbatim(self):
        from pact.heal import apply_patch_smart

        src = "def beta(x):\n    y = x + 1\n    return y\n"
        merged, conf, strat = apply_patch_smart(
            src,
            "def beta(x):\n    y = x + 1  # drift\n    return y",
            "def beta(x):\n    y = x + 9\n    return y",
            min_confidence=0.6,
        )
        assert merged is not None and "y = x + 9" in merged
        assert strat == FUZZY_REPLACE


class TestMinimalLinePatch:
    def test_reduces_to_minimal_block_and_round_trips(self):
        from pact.heal import _minimal_line_patch, apply_patch

        original = "a = 1\nb = 2\nc = 3\nd = 4\n"
        patched = "a = 1\nb = 22\nc = 33\nd = 4\n"
        mp = _minimal_line_patch(original, patched)
        assert mp is not None
        orig_block, repl_block = mp
        # minimal: the unchanged a/d lines are not in the block
        assert "a = 1" not in orig_block and "d = 4" not in orig_block
        assert apply_patch(original, orig_block, repl_block) == patched

    def test_returns_none_when_identical(self):
        from pact.heal import _minimal_line_patch

        assert _minimal_line_patch("x = 1\n", "x = 1\n") is None


class TestDeterministicPatch:
    def test_bare_except_generates_zero_llm_patch(self):
        from pact.heal import _deterministic_patch, apply_patch

        src = "def f():\n    try:\n        go()\n    except:\n        pass\n"
        lines = src.splitlines(keepends=True)
        violation = {
            "mode": "bare_except",
            "file": "x.py",
            "line": 4,  # the `except:` line
            "call": "except:",
            "message": "bare except swallows errors",
        }
        patch = _deterministic_patch(violation, lines)
        assert patch is not None
        merged = apply_patch(src, patch.original, patch.replacement)
        assert merged is not None
        assert "except Exception:" in merged

    def test_unfixable_mode_returns_none(self):
        from pact.heal import _deterministic_patch

        violation = {"mode": "some_mode_with_no_fixer", "file": "x.py", "line": 1}
        assert _deterministic_patch(violation, ["a = 1\n"]) is None
