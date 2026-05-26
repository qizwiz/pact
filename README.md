# pact

[![CI](https://github.com/qizwiz/pact/actions/workflows/ci.yml/badge.svg)](https://github.com/qizwiz/pact/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pact-tool)](https://pypi.org/project/pact-tool/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://pypi.org/project/pact-tool/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Structural verification for codebases AI builds.**

---

## The problem every engineering leader has right now

Your team ships 10× faster with AI. Your on-call rotation is also 10× more interesting.

AI agents write each function correctly in isolation. The failures appear at the *joints* — the interfaces between components that each agent assumed were someone else's responsibility. Tests don't catch these because tests only exercise the paths you already imagined. The incidents that cost you at 3am are the ones nobody thought to test.

pact treats your codebase the way a structural engineer treats a bridge: find the load-bearing joints, verify the contracts at those joints formally, stress-test them adversarially, and produce the minimal patch — before the code ships.

```bash
pip install pact-tool
pact .
```

```
✗  pact: 14 violation(s)

  tasks/auto_eval.py:89  [missing_await]
    coroutine 'trigger_evaluation' called without await —
    the evaluation never runs; this is a silent no-op

  model_hub/views.py:203  [optional_dereference]
    'api_key' assigned from .get() at line 201 but passed to
    LLM client without None check — AttributeError in production

  evaluations/tasks.py:156  [save_without_update_fields]
    .save() re-writes every column; concurrent request at line 201
    overwrites the status you just set

  utils/cache.py:44  [bare_except]
    except Exception: pass — the Redis timeout that caused
    your 3am incident is silently swallowed here
```

**Python, TypeScript, and Go** supported. Runs in your existing CI pipeline in 5 minutes.

---

## Track record

pact has found violations in **14,148 unique sites across 1,254 repositories**. 12 PRs merged upstream to major open-source projects.

| Repository | Stars | Violations found |
|------------|-------|-----------------|
| langchain-ai/langchain | 136k | 438 — including 256 unawaited async calls across every LLM provider |
| hiyouga/LlamaFactory | 71k | 18 — response array access without length guard, silent training failures |
| home-assistant/core | 87k | 34,701 across 14,096 files |
| microsoft/generative-ai-for-beginners | 64k | `choices[0]` without None check in tutorial code |
| future-agi/future-agi | internal | 755 — 410 concurrent-write races across Django models |

None of these were found by the linters already in those repositories.

---

## For engineering leaders

**The core question pact answers: which parts of your codebase, if they break, take the most downstream behavior with them?**

AI writes code that is locally correct. pact tells you whether it is *structurally* correct — whether the contracts between components hold, whether the load-bearing functions have been formally verified, and whether your codebase's dependency topology is safe to change.

### Drop it into CI today

```yaml
# .github/workflows/ci.yml
- name: pact
  uses: qizwiz/pact/.github/actions/pact@main
  with:
    path: .
    incremental: "true"   # only analyzes files changed since main
    strict: "true"        # fails the build on any new violation
```

Or directly:
```yaml
- run: pip install pact-tool z3-solver && pact . --incremental main --strict
```

`--incremental main` ensures your team never introduces a new category of violation without being told. Existing violations are not blocked — only new ones. This means zero disruption to shipping velocity on day one.

### Three-week onboarding path

**Week 1** — `pact . --incremental main --strict` in CI. Zero disruption. Blocks new violation types only.

**Week 2** — `pact fix . --apply` on the highest-density files. Each fix is verified by Z3 before application — not a suggestion, a proof.

**Week 3** — `pact intent analyze . --out intent.json && pact pipeline intent.json` for any critical subsystem being rewritten by agents. Extracts behavioral contracts, verifies them formally, stress-tests adversarially, heals automatically.

---

## Why not a linter

Linters match patterns. pact reasons about structure.

`response.choices[0].message.content` is valid Python. It passes every linter. pact knows that `choices` is typed `Optional[list]` in the OpenAI SDK — the type says it can be an empty list — and that accessing `[0]` without a length check raises `IndexError` on content filtering. The linter sees a valid attribute access. pact sees a contract violation, and can prove it: Z3 will hand you the exact input that causes it.

The difference matters more as AI writes more of your code. Linters were designed for human-paced codebases where the reviewer catches the structural mistakes. In the AI era, the reviewer is pact.

---

## The verification pipeline (for AI-generated code)

When agents are writing most of your code, you need to verify the contracts between their work — not just surface-check for known patterns.

```bash
# Requires: pip install "pact-tool[llm]" and ANTHROPIC_API_KEY

# Step 1: extract behavioral contracts from your codebase
pact intent analyze path/to/project/ --out intent.json

# Step 2: run the full verification pipeline
pact pipeline intent.json -v
```

```
  step 1: z3 → payments.py:process_payment
  Z3: ✗ violated — input x=-1 breaks the "amount must be positive" contract
  step 2: hypothesis → payments.py:process_payment
  Hypothesis: ✗ violated — counterexample: process_payment(amount=-1, currency='USD')
  step 3: heal → payments.py:process_payment
  heal: ✓ patch verified — added guard: if amount <= 0: raise ValueError
```

**Intent analysis** reads your code and docstrings and writes down what each function is supposed to guarantee. "This function must always return a positive number." "This function must never modify its input."

**Z3 verification** takes each guarantee and exhaustively searches for any input that breaks it. This is not sampling — it is mathematical proof. If Z3 says the contract holds, it has *proved* it holds. If it finds a violation, it hands you the exact input.

**Hypothesis stress-testing** seeds adversarial search from the Z3 counterexample, finding the full shape of the failure, not just one instance.

**Heal** generates a candidate patch using an LLM, then *verifies the patch with Z3* before accepting it. LLM proposes; Z3 decides. This is counterexample-guided synthesis (CEGIS) — the same technique used in formal program synthesis research, applied to your codebase.

---

## What pact checks

### Python

| Check | What it catches |
|-------|-----------------|
| `optional_dereference` | `.first()` / `.get()` result used without `None` check |
| `missing_await` | Async function called without `await` — body never runs |
| `bare_except` | `except Exception: pass` — silent error suppression |
| `save_without_update_fields` | `.save()` overwrites all columns, races concurrent writes |
| `unvalidated_lookup_chain` | `d.get(k)` result used as dict key without guard |
| `required_arg_missing` | Call omits a required argument |
| `mutable_default_arg` | `def f(x=[]):` — shared state across calls |
| `llm_response_unguarded` | `response.choices[0]` without length check |
| `model_constraint` | Django model created missing a required field |
| `format_arg_mismatch` | `"{} {}".format(a)` — too few args → IndexError at runtime |

### Go

`go_ignored_error`, `go_bare_recover`, `go_unchecked_assertion`, `go_goroutine_no_sync`

### TypeScript

Full tree-sitter parser, same violation taxonomy.

---

## Structural analysis

Beyond individual violations, pact identifies which parts of your codebase are *structurally risky* — not because they have bugs today, but because of where they sit in the call graph.

```bash
pact . --reduce
```

```
  TANGLE  payments.charge  [payments/charge.py:14]
    cycle: payments.charge → payments.validate → payments.charge
    3 functions in a mutual call cycle — breaking the cycle removes 2 back-edges
    reduction_potential=2  violations=4

  PASSTHROUGH  api.route_and_forward  [api/router.py:88]
    pure hop with no logic — inline to collapse 1 node + 2 edges
    reduction_potential=3
```

**Load-bearing functions** — called cut vertices in graph theory — are the functions where, if behavior changes, the most downstream code breaks. pact finds these and prioritizes formal verification there. A contract violation in a cut vertex propagates everywhere that depends on it.

The R.C. Martin instability metrics (`I`, `A`, `D`) are also computed per module:
- **Zone of pain** (D > 0.7, concrete, highly depended-on): these modules are hard to change and critically load-bearing
- **Zone of uselessness** (D > 0.7, abstract, nobody depends on): these abstractions are stranded — carrying maintenance cost with no users

---

## Git archaeology

```bash
pact enrich path/to/project/ --out enrich.json
```

`enrich` extracts Tornhill signals from the full git history: hotspot scores (churn × complexity), temporal coupling (files that change together but are architecturally separate), and knowledge silos (the minor-contributor ratio that Bird et al. showed is the strongest post-release defect predictor ever measured at Microsoft). These signals feed the intent graph as L1 coverage edges.

---

## TLA+ specifications

For critical subsystems, pact generates a formal specification — a mathematically precise description of what must always be true — and verifies it with TLC.

```bash
pact spec complete path/to/tasks.py -o MySpec.tla
```

Four built-in templates cover the most common temporal properties:
- `resource_lifecycle` — acquire without release is a real violation
- `ordering` — initialization before use
- `accumulation` — monotone growth invariants
- `liveness` — eventual completion guarantees

---

## How it works

```
extractor.py    AST → function manifests, call sites, type information
failure_mode.py Per-check plugin layer
z3_engine.py    Constraint solver — whole-program satisfiability queries
checker.py      Orchestration: extract → verify → deduplicate → report
pipeline.py     Z3 → Hypothesis → heal pipeline for contract verification
intent.py       Behavioral contract extraction from source + docstrings
enrich.py       Git archaeology + GitHub layer + intent graph construction
heal.py         CEGIS patch loop: generate → verify → accept/reject
reduce.py       Structural analysis: cycles, pass-throughs, R.C. Martin metrics
specgen.py      AST → TLA+ specification skeleton
cli.py          Entry point
```

Each violation pact reports is not a heuristic — it is Z3-satisfiable: there exists a concrete input that causes it. The interprocedural taint analysis tracks violations across file boundaries, resolving calls to their actual source files rather than guessing from function names.

---

## Install

```bash
pip install pact-tool              # scanner + structural analysis
pip install "pact-tool[llm]"      # + verification pipeline (requires ANTHROPIC_API_KEY)
```

Python 3.10+. Z3 solver included. No Java required for basic usage; TLC requires a JVM.

---

## Licensing

pact is MIT licensed. Free forever for self-hosted use.

A commercial tier (pact Cloud) is in development: web dashboard, GitHub App integration, team-level trend analysis, compliance reports, SSO, and SLA support. If you're interested in early access for your organization — particularly if you're scaling AI-generated code across a team — reach out at jonathan.f.hill@gmail.com.

Annual enterprise licenses are available for organizations that need on-premises deployment or custom rule sets.
