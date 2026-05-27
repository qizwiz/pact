# Structural Intent Analysis: `pact-standalone`

**Generated:** 2026-05-27  **Model:** anthropic/claude-sonnet-4-5  **Modules:** 4

## Project Summary

pact is a graph-first structural verifier that finds violations at component interfaces by treating code as G=(V,E) where violations are paths violating constraint predicates C(p), not AST pattern matches. It orchestrates Z3, Hypothesis, TLA+, and CrossHair to formally verify load-bearing functions, then uses self-improving LLM prompts to synthesize minimal patches that restore contracts.
