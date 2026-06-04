# What pact's shipping engine actually does vs. the validated method

**Autonomous, no-LLM audit — 2026-05-30.** Question this answers: *is pact-the-product the
validated method, or a thin wrapper around it?* Every claim below was checked first-hand against
the source and the real artifacts on disk; nothing here is transcribed from an agent.

---

## 0. Why this is a no-LLM audit (the blocker)

The live decisive experiment — run the real `pact find` deep engine on a fresh AI-code corpus and
adjudicate its output — **could not run this session**:

- MCP sampling is off: `pact_ping` → `SamplingUnavailable: Host does not support
  sampling/createMessage. Run LLM tools via the CLI`.
- The CLI path (`pact find`) needs an LLM key; **none is in the environment**
  (`ANTHROPIC_API_KEY` / `PACT_LLM_API_KEY` / `ANTHROPIC_AUTH_TOKEN` all unset).

**To run the live experiment:** `export ANTHROPIC_API_KEY=sk-ant-...`, then `pact find` on a fresh
corpus, split by `hypothesis_confirmed`, adjudicate vs ground truth. See §4.

So this audit reads the shipping engine and audits artifacts already on disk — which turns out to
answer the binding question cleanly anyway.

---

## 1. The shipping `find` engine is REAL, not a thin wrapper

`find.py` is a genuine generate→verify loop, not a single-shot prompt:

- **Agentic read** (`_call_with_tools`, max 8 rounds): the model is given a `read_file_lines`
  tool and *must* read the file before forming opinions (`prompts/find.md` PART 0). Up to 8 rounds
  means it *can* cross-reference within a file.
- **Property-based confirmation** (`_confirm_with_hypothesis`): each candidate counterexample is
  run through **Hypothesis in a subprocess**. If Hypothesis finds an input violating the claimed
  predicate → `hypothesis_confirmed: true`, `confidence: 0.95`. Otherwise `0.7`.
- **Git-history prior** (`context.py`): findings are weighted by what has actually broken before
  (commits matching a fix-pattern, changelog, TODO/FIXME).
- **Self-improvement**: rewrites `find.md` if the confirmation rate drops below 30%.

This is the same shape as the validated method (generate → adversarially confirm against ground
truth). The "we hand you a *machine-reproduced* failing input, not a pattern flag" claim is real
and already built.

## 2. …but it is the CONFIRMABLE SUBSET of the method, by design

`prompts/find.md` imposes three constraints that bound what the engine can ever find:

1. **"one per function, max 5 total"** — recall is hard-capped per file.
2. **"Focus on semantically real violations. Skip obvious ones (None to non-None param)."** — good.
3. **"If you cannot write a runnable `hypothesis_strategy`, skip that property."** — decisive.

Constraint 3 restricts every finding to a **single-function, input-generatable, Hypothesis-
confirmable** violation. Consequences:

- **In scope (and done well):** local input-driven violations — unguarded `json.loads`,
  `choices[0]` on possibly-empty lists, length/format invariants, silent error contracts. These
  get *machine-proven* by Hypothesis. High, defensible precision.
- **Out of scope (skipped by construction):** cross-frame / multi-file / stateful bugs — worker
  idempotency across requests, races, ORM transaction boundaries, multi-call protocol violations.
  These cannot be written as a one-function `st.*` strategy, so the prompt instructs the model to
  drop them.

**This is the crux.** The "deep understanding / differentiated cross-frame value" that drove the
corpus story (e.g. the worker-idempotency class) was produced by open-ended agent reasoning in
orchestration — **not** by `pact find`, whose prompt deliberately excludes anything Hypothesis
can't reproduce. So:

> **pact-the-product IS the method for the verifiable-local slice, and is NOT the method for the
> deep cross-frame slice.** The deep slice lived in the harness, not the shipping engine.

## 3. Two precision tiers — and exactly where false positives live

Every candidate is emitted as a violation, tagged `hypothesis_confirmed` (`find.py:276–287`):

- **Confirmed (0.95):** Hypothesis found a real input violating the claimed predicate. High
  precision. *Residual FP mode:* the LLM asserts a **wrong invariant** (something that isn't
  actually a contract), and Hypothesis faithfully "confirms" a violation of a non-contract. So
  confirmed ≠ guaranteed-real; it = "the claimed property is genuinely violable." Judgment still
  needed on whether the claimed invariant is a real contract.
- **Unconfirmed (0.7):** LLM-claim only; Hypothesis could not reproduce. **This is the FP-dense
  tier.** An honest product must split on `hypothesis_confirmed` and lead with the confirmed slice.

## 4. The live experiment to settle precision/recall (needs a key)

1. Generate a fresh 8–12 app AI corpus (independent agents build natural small services; not told
   about bug patterns).
2. `pact find <each production file> --out find.json` (real engine).
3. Split findings by `hypothesis_confirmed`.
4. Adjudicate each vs ground truth: can it trigger? production vs test? real contract or invented?
5. Report **precision(confirmed)** vs **precision(unconfirmed)** and **recall**.

**Falsifiable predictions** (from the engine design, to be tested):
- confirmed-slice precision **high** (≥ ~85%) — Hypothesis-gated;
- unconfirmed-slice precision **low** (≤ ~50%) — LLM-claim-only;
- recall **capped** by max-5-per-file + the strategy-skip rule, and **zero** on cross-frame bugs.

If these hold, the honest product is the confirmed slice; the corpus's headline 93% was the
*method's* number (my agents, deeper scope), not the shipping engine's.

## 5. The intent engine ships accurate output (verified first-hand)

`intent_pact_fresh.json` (pact's intent run on itself, 2026-05-23, sonnet-4-6) is precise:

- Project summary is correct ("encodes classes of structural bugs as Z3 satisfiability constraints
  over a cross-file call graph…").
- Spot-checked invariant: *"any subprocess failure inside `_run` is silently swallowed and the
  empty string is returned; callers receive no signal."* Verified against `context.py:35` —
  `_run` is literally `try: subprocess.run(..., check=True); return r.stdout / except Exception:
  return ""`. **Exactly correct.**

The intent invariants are concrete and falsifiable, not vague — corroborating the ~95%-true intent
story on a real artifact.

## 6. The no-LLM layers are the fast/dead layers

`pact_check` (AST patterns) returned **0** on the langchain example; `pact_metrics` (R.C. Martin
coupling) is structural, not bug-finding. These are not the value; they're the cheap fallback.

---

## Bottom line (money read, sharpened)

The **defensible, real, already-built wedge** is *"we don't just flag — we hand you a failing test
case"*: the Hypothesis-confirmed local-violation finder. It is honest (machine-proven),
differentiated from linters (which cannot reproduce), and shipping today. Lead with the
**confirmed** slice; treat unconfirmed as triage, not findings.

The **"deep cross-frame understanding"** story is *not* in the shipping engine — it lived in agent
orchestration and would have to be built into `find.py` to become product. That is the harder bet:
cross-frame bugs resist Hypothesis confirmation, so the precision guarantee that makes the local
finder defensible does not transfer for free.

Honest one-liner: **pact ships the verifiable half of the method at high precision; the impressive
half was the harness, and isn't productized yet.**
