# Direction: pact as a "symbolic spreadsheet for security"

*Captured 2026-05-31. A product/architecture direction, not a committed decision — a thing to prototype.*

## The seed insight

A **spreadsheet is a single-state theorem prover**: you declare relationships between
cells (formulas) and an engine maintains them via dependency propagation. The *only* gap
between a spreadsheet and a real formal-verification prover (e.g. Certora) is:

1. **universal quantification** — a spreadsheet checks your formulas for the *one* concrete
   state currently in the cells; a prover checks them for *all possible* values; and
2. **a solver** (z3 / SMT) to discharge the ∀.

So: **FV = spreadsheet + ∀ + z3.**

## Why this matters for pact

pact's solid findings today are largely *commodity* — local, imperative pattern-matching
(the bandit/semgrep tier). The differentiation lives one level up:

> Move pact from *local pattern-matching* toward **declarative global-invariant checking**:
> the **AI agents *propose* global conservation/solvency invariants** over a dataflow model
> of the code, and **z3 *discharges* them** across all inputs. **The counterexample is the finding.**

We already have the substrate: `z3_engine.py`, `constraint_graph.py`, `contract_encoder.py`,
`checker.py`. This direction is about *what we point them at* — global invariants proposed by
the agent layer, not hand-written local rules.

### Why it's the moat, not just a feature

- **Local checks are structurally blind to systemic theft.** (Concrete lesson: reentrancy
  drains the *pool* while every individual account balance still reads as valid — a per-account
  invariant passes; only the *aggregate* solvency invariant `balance >= Σ obligations` catches it.)
  So global-invariant pact finds exactly the class of bug the commodity tier *cannot*.
- **Favor global conservation laws over local guards.** The valuable invariants are
  conservation/solvency identities (`Σ balances == totalSupply`, `held >= owed`), maintained
  the way physics maintains conservation: *locally* (hook every state write, propagate the delta
  to a ghost accumulator — no iteration), globally enforced.
- It **is** the AI+formal-verification niche the market is now paying for
  (cf. Certora "AI Composer" baking FV into AI codegen; CertiK's remote "Blockchain Security
  Expert – AI Track" building LLM agents that auto-analyze contracts). pact framed this way is
  both the differentiated artifact *and* the legible proof of the niche.

## Division of labor

- **AI agents → propose invariants** (the hard, creative part; humans are bad at inventing the
  right global invariant). This is where pact's agent-orchestration edge lives.
- **z3 → discharge them** (`unsat` = property holds for all inputs; a model = the exploit/finding).

## The open hard part (be honest)

The unsolved problem is getting the agent to propose invariants that are both **true** and
**non-trivial** — i.e. precision. This is the same precision risk flagged in the commercial
assessment; it doesn't go away, it just moves to "are the proposed invariants any good?"
A measurable prototype loop: *agent proposes candidate conservation invariant → z3 checks →
counterexample surfaces as finding*, scored on precision against a labeled corpus.
