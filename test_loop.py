"""
Tests for pact_loop.py — convergence math, fitness, LoopState, TLA+ generation.

Does NOT make LLM calls. Tests the orchestration logic only.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from .pact_loop import (
    IterationState,
    LoopResult,
    MeasureResult,
    _converged,
    _stuck,
    compute_fitness,
    generate_adr,
    generate_tla_spec,
)

# ---------------------------------------------------------------------------
# Fitness function
# ---------------------------------------------------------------------------


class TestComputeFitness:
    def test_zero_violations_max_quality(self):
        s = IterationState(iteration=1)
        s.total_violations = 0
        s.heal_accept_rate = 1.0
        s.oracle_confirm_rate = 1.0
        s.find_confirm_rate = 1.0
        s.topo_score = 1.0
        s.avg_prompt_score = 1.0
        s.sheaf_score = 1.0
        f = compute_fitness(s, initial_violations=10)
        assert f == pytest.approx(1.0, abs=1e-6)

    def test_all_zero_quality(self):
        s = IterationState(iteration=1)
        s.total_violations = 10
        s.heal_accept_rate = 0.0
        s.oracle_confirm_rate = 0.0
        s.find_confirm_rate = 0.0
        s.topo_score = 0.0
        s.avg_prompt_score = 0.0
        s.sheaf_score = 0.0
        f = compute_fitness(s, initial_violations=10)
        assert f == pytest.approx(0.0, abs=1e-6)

    def test_zero_initial_violations_treated_as_one(self):
        # Never divides by zero
        s = IterationState(iteration=1)
        s.total_violations = 0
        f = compute_fitness(s, initial_violations=0)
        assert 0.0 <= f <= 1.0

    def test_violation_reduction_improves_fitness(self):
        s1 = IterationState(iteration=1)
        s1.total_violations = 20
        s2 = IterationState(iteration=2)
        s2.total_violations = 5
        f1 = compute_fitness(s1, initial_violations=20)
        f2 = compute_fitness(s2, initial_violations=20)
        assert f2 > f1

    def test_oracle_trust_component(self):
        s = IterationState(iteration=1)
        s.total_violations = 5
        s.oracle_confirm_rate = 1.0
        s.heal_accept_rate = 1.0
        s.find_confirm_rate = 0.0
        s.topo_score = 0.0
        s.avg_prompt_score = 0.0
        s.sheaf_score = 0.0
        f = compute_fitness(s, initial_violations=10)
        # violation component: 0.25 * 0.5 = 0.125
        # heal: 0.20 * 1.0 = 0.20
        # oracle: 0.15 * 1.0 = 0.15
        assert f == pytest.approx(0.125 + 0.20 + 0.15, abs=1e-6)

    def test_fitness_in_unit_interval(self):
        import random

        rng = random.Random(42)
        for _ in range(50):
            s = IterationState(iteration=1)
            s.total_violations = rng.randint(0, 100)
            s.heal_accept_rate = rng.random()
            s.oracle_confirm_rate = rng.random()
            s.find_confirm_rate = rng.random()
            s.topo_score = rng.random()
            s.avg_prompt_score = rng.random()
            s.sheaf_score = rng.random()
            iv = rng.randint(1, 100)
            f = compute_fitness(s, initial_violations=iv)
            assert 0.0 <= f <= 1.0, f"fitness {f} out of [0,1]"


# ---------------------------------------------------------------------------
# Convergence detection
# ---------------------------------------------------------------------------


class TestConvergence:
    def test_too_few_iterations(self):
        assert not _converged([0.5, 0.6], epsilon=0.01, window=3)

    def test_flat_history_converges(self):
        assert _converged([0.7, 0.7, 0.7], epsilon=0.01, window=3)

    def test_small_variation_converges(self):
        assert _converged([0.700, 0.701, 0.700], epsilon=0.01, window=3)

    def test_large_variation_not_converged(self):
        assert not _converged([0.5, 0.7, 0.9], epsilon=0.01, window=3)

    def test_uses_only_recent_window(self):
        # Early oscillation should not matter
        history = [0.1, 0.9, 0.1, 0.9, 0.7, 0.7, 0.7]
        assert _converged(history, epsilon=0.01, window=3)

    def test_epsilon_boundary(self):
        # delta = 0.01 exactly — should NOT converge (strict <)
        assert not _converged([0.700, 0.710], epsilon=0.01, window=2)
        # delta = 0.009 — should converge
        assert _converged([0.700, 0.709], epsilon=0.01, window=2)


# ---------------------------------------------------------------------------
# Stuck detection
# ---------------------------------------------------------------------------


class TestStuck:
    def test_too_few(self):
        assert not _stuck([0], window=2)

    def test_all_zero_stuck(self):
        assert _stuck([0, 0], window=2)

    def test_one_nonzero_not_stuck(self):
        assert not _stuck([1, 0], window=2)

    def test_trailing_zeros_stuck(self):
        assert _stuck([5, 3, 0, 0], window=2)

    def test_trailing_nonzero_not_stuck(self):
        assert not _stuck([0, 0, 1], window=2)


# ---------------------------------------------------------------------------
# LoopState serialization
# ---------------------------------------------------------------------------


class TestIterationStateSerialization:
    def test_roundtrip(self):
        s = IterationState(iteration=3)
        s.total_violations = 42
        s.heal_accept_rate = 0.75
        s.fitness = 0.634
        d = dataclasses.asdict(s)
        assert d["iteration"] == 3
        assert d["total_violations"] == 42
        assert d["fitness"] == pytest.approx(0.634)

    def test_json_serializable(self):
        s = IterationState(iteration=1)
        s.measure = MeasureResult(checker_total=5, checker_by_mode={"bare_except": 3})
        raw = json.dumps(dataclasses.asdict(s))
        restored = json.loads(raw)
        assert restored["measure"]["checker_total"] == 5

    def test_loop_result_summary(self):
        r = LoopResult(
            target="/tmp/proj",
            test_cmd="pytest",
            termination="CONVERGED",
            initial_violations=20,
            final_violations=3,
            final_fitness=0.87,
            elapsed_seconds=42.5,
        )
        s = r.summary()
        assert "CONVERGED" in s
        assert "20 → 3" in s
        assert "0.870" in s


# ---------------------------------------------------------------------------
# TLA+ spec generation
# ---------------------------------------------------------------------------


class TestTLAGeneration:
    def test_generates_tla_and_cfg(self, tmp_path):
        tla_path = generate_tla_spec(max_iters=5, output_dir=tmp_path)
        assert tla_path.exists()
        cfg_path = tmp_path / "PactLoop.cfg"
        assert cfg_path.exists()

    def test_tla_contains_oracle_safety(self, tmp_path):
        tla_path = generate_tla_spec(max_iters=5, output_dir=tmp_path)
        text = tla_path.read_text()
        assert "OracleSafety" in text
        assert "patches_applied" in text
        assert "oracle_passed" in text

    def test_tla_contains_termination(self, tmp_path):
        tla_path = generate_tla_spec(max_iters=5, output_dir=tmp_path)
        text = tla_path.read_text()
        assert "Termination" in text
        assert "MAX_ITERS" in text

    def test_cfg_contains_constants(self, tmp_path):
        generate_tla_spec(max_iters=7, output_dir=tmp_path)
        cfg = (tmp_path / "PactLoop.cfg").read_text()
        assert "MAX_ITERS = 7" in cfg
        assert "INVARIANT OracleSafety" in cfg
        assert "PROPERTY Termination" in cfg

    def test_formal_tla_spec_exists(self):
        formal = Path(__file__).parent / "docs" / "tla" / "PactLoop.tla"
        assert formal.exists(), "docs/tla/PactLoop.tla not found"
        text = formal.read_text()
        assert "FitnessMonotone" in text
        assert "StuckDetection" in text or "Stuck" in text
        assert "WF_vars" in text
        assert "PhaseProgress" in text

    def test_formal_cfg_has_all_properties(self):
        cfg = Path(__file__).parent / "docs" / "tla" / "PactLoop.cfg"
        assert cfg.exists()
        text = cfg.read_text()
        assert "INVARIANT OracleSafety" in text
        assert "PROPERTY Termination" in text
        assert "PROPERTY FitnessMonotone" in text


# ---------------------------------------------------------------------------
# ADR generation
# ---------------------------------------------------------------------------


class TestADRGeneration:
    def test_generates_file_on_heal_accepted(self, tmp_path):
        adr_dir = tmp_path / "adr"
        state = IterationState(iteration=1)
        state.heal_accepted = 2
        state.heal_attempted = 3
        state.heal_accept_rate = 0.67
        state.fitness = 0.54

        filename = generate_adr(state, Path("/tmp/proj"), adr_dir, 10)
        assert filename is not None
        assert (adr_dir / filename).exists()
        text = (adr_dir / filename).read_text()
        assert "ADR-" in text
        assert "iteration 1" in text

    def test_no_adr_when_nothing_significant(self, tmp_path):
        adr_dir = tmp_path / "adr"
        state = IterationState(iteration=2)
        state.heal_accepted = 0
        state.measure = MeasureResult(sheaf_rank=0)
        filename = generate_adr(state, Path("/tmp/proj"), adr_dir, 10)
        assert filename is None

    def test_adr_on_proved_clean(self, tmp_path):
        adr_dir = tmp_path / "adr"
        state = IterationState(iteration=1)
        state.total_violations = 0
        state.termination = "PROVED_CLEAN"
        filename = generate_adr(state, Path("/tmp/proj"), adr_dir, 5)
        assert filename is not None

    def test_adr_number_increments(self, tmp_path):
        adr_dir = tmp_path / "adr"
        adr_dir.mkdir()
        # Pre-seed two ADRs
        (adr_dir / "ADR-001-foo.md").write_text("x")
        (adr_dir / "ADR-040-bar.md").write_text("x")

        state = IterationState(iteration=1)
        state.heal_accepted = 1
        filename = generate_adr(state, Path("/tmp/proj"), adr_dir, 5)
        assert filename is not None
        assert "ADR-041" in filename

    def test_adr_body_contains_all_dimensions(self, tmp_path):
        adr_dir = tmp_path / "adr"
        state = IterationState(iteration=3)
        state.heal_accepted = 1
        state.heal_attempted = 2
        state.heal_accept_rate = 0.5
        state.oracle_confirmed = 1
        state.oracle_confirm_rate = 0.5
        state.fitness = 0.62
        state.measure = MeasureResult(
            checker_total=5,
            sheaf_rank=2,
            interproc_transitive=3,
            scc_count=1,
            hub_count=2,
        )
        filename = generate_adr(state, Path("/tmp/myproject"), adr_dir, 10)
        text = (adr_dir / filename).read_text()
        assert "Sheaf" in text or "sheaf" in text
        assert "Oracle" in text or "oracle" in text
        assert "0.62" in text


# ---------------------------------------------------------------------------
# CLI subcommand
# ---------------------------------------------------------------------------


class TestCLI:
    def test_loop_help_exits_zero(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "pact", "loop", "--help"],
            capture_output=True,
            text=True,
            cwd="/Users/jonathanhill/src",
        )
        assert r.returncode == 0
        assert "test-cmd" in r.stdout

    def test_loop_requires_target(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "pact", "loop"],
            capture_output=True,
            text=True,
            cwd="/Users/jonathanhill/src",
        )
        assert r.returncode != 0
