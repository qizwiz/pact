# ADR-008: Precise Mode — Container-Isolated Analysis with Jedi + Blast Radius

**Status**: Accepted  
**Date**: 2026-05-16  
**Deciders**: qizwiz

---

## Context

pact's AST-derived call graph has systematic false edges. When a method `obj.method()` is seen and `obj`'s type can't be resolved, pact adds edges to every class that defines `method`. This generates phantom blast radii and inflated violation counts for real-world repos.

Two orthogonal problems needed solving simultaneously:

1. **Type resolution**: install the repo's dependencies so `obj` resolves to a concrete type
2. **Blast radius ranking**: given a resolved graph, rank violations by transitive caller count

---

## Decision

### Precise mode: container-isolated dependency installation

Build a minimal Docker image that:
- installs the target repo's dependencies (4-tier detection: pyproject.toml → requirements.txt → setup.py/setup.cfg → Pipfile)
- installs `jedi` and `pact-tool` inside the container
- runs `pact --json --blast-radius` as the ENTRYPOINT

Jedi resolves call sites against the installed environment. False edges collapse to zero for resolved types. The approach degrades gracefully — unresolved imports fall back to AST-only analysis for that site.

Alternative considered: run Jedi locally against the caller's environment. Rejected: introduces dependency bleed between target repos and the analysis host.

Alternative considered: pyright / mypy for type resolution. Rejected: server lifecycle overhead, harder to run inside a container for arbitrary repos, and neither exposes the per-call-site resolution API that Jedi's `Script.goto()` provides.

### Blast radius: `nx.ancestors()` of the enclosing function

For each violation, find its enclosing function and count transitive callers via `nx.ancestors(G, func_name)`. This gives the **blast radius**: how many callers reach this violation transitively.

Violations are ranked descending by blast radius. High-blast violations are prioritized because a bug in a widely-called function affects more execution paths.

```python
ViolationWithBlast(
    violation=v,
    enclosing_func="process_response",
    blast_radius=47,          # 47 callers transitively reach this function
    reachable_from=frozenset({"main", "handle_request", ...}),
)
```

Alternative considered: rank by call site count (direct callers only). Rejected: misses transitive cascades. A function called once but from a hub that's called 1000 times has a real blast radius of 1001, not 1.

---

## Consequences

- `PreciseScanner` is the high-accuracy path; local AST scan remains the default for speed
- GitHub Actions workflow (`precise-scan.yml`) exposes precise mode as a `workflow_dispatch`
- Blast radius is additive: `--blast-radius` re-ranks the same violations, doesn't add new ones
- Container build time (~60s for a typical Python repo) is the main cost
- The `_local_fallback()` path ensures pact still runs usefully with no Docker installed
- RTA (Rapid Type Analysis) is the next planned improvement: add edges only to instantiated types, reducing phantom edges before Jedi even runs
