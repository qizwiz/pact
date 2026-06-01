# ADR-070: Language Extractor Abstraction and IR Naming

**Status**: Proposed  
**Date**: 2026-05-31  

---

## Context

pact's engine is genuinely language-agnostic. `z3_engine.py` is a Z3 **Fixedpoint
(Datalog)** query engine — *"Extract AST facts → Z3 Fixedpoint database; Z3's
Fixedpoint derives all violations"* — and `prover.py` / `contract_encoder.py` are
SMT **provers** (`Solver`, `check()`, `unsat`=holds / `sat`=counterexample, plus a
CEGIS loop). Both consume an IR, not source. Per ADR-001 the ground-truth structure
is the constraint/call graph `G = (V, E)` with `V = FunctionManifest`, `E = CallSite`.

But the per-language adapters that *produce* that IR were never unified. An
architecture sweep (2026-05-31) found three divergent attach points:

- **Python**: `extractor.py` (`ast`) → `FailureMode` → `FailureEvidence` (the full path).
- **TypeScript**: `ts_checker.py` walks tree-sitter nodes and returns `Violation`
  directly, **bypassing `FailureMode`**.
- **Go**: `go_checker.py` + `go/checker/main.go`, a subprocess emitting
  language-prefixed mode names (`go_ignored_error`) over JSON.

There is **no `Extractor` ABC**; each language integrates at a different abstraction
level. Adding a Solidity capability — for the AI + formal-verification smart-contract
niche (the "symbolic spreadsheet": an LLM proposes a conservation invariant, Z3
discharges it, counterexample = finding) — would become a **fourth one-off** unless
the socket is defined first.

Two further findings constrain the decision:

1. **Two Python-AST stowaways** sit in otherwise-language-agnostic modules:
   `constraint_graph.py:11/27` (`import ast`, `_is_pure_function`) and
   `z3_engine.py:379-493` (`_extract_llm_facts`, an `ast.NodeVisitor` inside
   `LLMResponseEngine`). `analyze_constraint_dag(G)` and `PactEngine` are themselves clean.
2. The current constraint vocabulary (`FieldConstraint`: presence / length / choice /
   range) **cannot express a global conservation invariant over a mapping**
   (`sum(balances) == totalSupply`). That requires a new constraint kind and Z3
   arrays/quantifiers.

Naming is contested, and the codebase already carries terms: **Manifests**
(`ModelManifest`, `FunctionManifest`, `CallSite`, `FieldConstraint` in `extractor.py`);
**EDB / IDB** (Datalog extensional/intensional — explicit in code comments);
**"contract IR"** (the pre-encoded Z3 script on the prover path); and the **constraint
graph** (ADR-001 ground truth). Two terms are overloaded and dangerous: **"frontend"**
(UI connotation) and **"contract"** (already means *behavioral contract*; would collide
with *smart contract*).

---

## Decision

**1. Define an `Extractor` ABC — the missing socket.** A per-language adapter that
emits Manifests and builds the constraint graph:

```python
class Extractor(ABC):
    def extract_models(self, root)     -> list[ModelManifest]:   ...  # state vars / structs / models
    def extract_functions(self, root)  -> list[FunctionManifest]: ...
    def extract_call_sites(self, root) -> list[CallSite]:        ...
    def build_constraint_dag(self)     -> "nx.DiGraph":          ...  # the G of ADR-001
```

`extractor.py`'s current logic becomes `PythonExtractor`; Go and TS are retrofitted
behind the ABC over time. Solidity is added as `SolidityExtractor` — a **peer, not a
fourth one-off**.

**2. Naming rulings (binding):**
- Language-agnostic IR / ground truth = **the constraint graph** (ADR-001's term).
- The Extractor's typed output = **Manifests**.
- Datalog facts / derived = **EDB / IDB** (unchanged).
- The per-language adapter = **Extractor** (CodeQL's term; pact's own code says *"Extract
  AST facts"*; the Datalog/Fixedpoint architecture *is* CodeQL's). Parser sublayer =
  **tree-sitter** (already used in `ast_utils.py`; has a Solidity grammar).
- **Banned for the Solidity work:** "frontend" (UI overload) and "contract IR" ("contract"
  already means behavioral contract — collides with smart-contract). Use *Manifest* /
  *constraint graph*.

**3. Grow, don't fork.** The core (Datalog query + SMT prove over a constraint graph) is
genuinely general; the deeper invariant exists, so Solidity is absorbed by **growing the
constraint vocabulary**, not forking a separate tool. The growth is contained: add a
`conservation` / `invariant` constraint kind in the clean `contract_templates` /
`contract_encoder` layer. The graph and the Extractor socket shape do **not** change — the
*constraint vocabulary* grows. (Conditional: revisit fork-vs-grow if EVM semantics later
prove inexpressible in `G` — see Consequences.)

**4. Two minimal decoupling refactors** (so the core takes IR, not a source `root`):
- `constraint_graph.py`: make `_is_pure_function` an **injectable predicate** (default =
  current Python `ast` impl); `structural_risk_report` consumes a built `nx.DiGraph`.
  `analyze_constraint_dag(G)` is already clean.
- Leave `PactEngine` (already takes IR) and the Python-only `LLMResponseEngine` untouched
  (out of scope for the conservation path).

---

## Alternatives Considered

- **"frontend" / `LanguageFrontend`** — rejected: "frontend" carries a UI connotation that
  misleads every web developer reading the repo. (The architecture-sweep agents themselves
  drifted into this term — evidence of its pull.)
- **"contract IR" for the Solidity representation** — rejected: doubly overloaded
  ("contract" = behavioral contract in pact; = smart contract here). Reusing it guarantees
  confusion.
- **Translator / Adapter / Lifter** — considered. "Lifter" is apt specifically for an
  EVM-*bytecode* path (lifting bytecode → IR) and is held in reserve for that. "Extractor"
  wins for the source-level path on three grounds: CodeQL precedent, pact's own *"Extract
  AST facts"* language, and the Datalog architecture being CodeQL's.
- **Fork a separate smart-contract tool** — rejected (for now): the core is genuinely
  language-agnostic (verified — no AST escapes into `PactEngine` / `analyze_constraint_dag`),
  so a shared core serves both; forking would duplicate the Z3/Datalog/CEGIS machinery. Held
  *conditionally*: if EVM state/storage/value semantics cannot be expressed in `G` without
  bending it out of shape, revisit.
- **Make Solidity a fourth one-off (like Go/TS)** — rejected: that is precisely the debt this
  ADR pays down. Defining the ABC now also gives Python/Go/TS a contract they never had.

---

## Consequences

- `Extractor` becomes the contract every language adapter must satisfy, including
  retrofitting the three existing ones (Python/Go/TS) over time.
- **Validation gate (Proposed → Accepted):** a `SolidityExtractor` parses one
  ERC-20-style `.sol` via tree-sitter, emits Manifests for `mapping(address=>uint) balances`
  + `uint totalSupply`, a new `conservation_invariant` template renders Z3 for
  `sum(balances) == totalSupply` preserved across `transfer`, fed to the **unchanged**
  `contract_encoder` / `prover` → `unsat` = holds, `sat` = counterexample = finding. That
  single green/red path is the smallest viable proof of the seam.
- The constraint vocabulary grows (a conservation/invariant kind); the graph and Extractor
  socket shape do not.
- Naming discipline is binding: no "frontend"; no "contract IR" for Solidity. Sanctioned
  terms: **Extractor / Manifest / constraint graph / EDB / IDB**.
- Deferred / out of scope for the first step: `solidity_failure_mode`, the spec-learning tier
  (coupled to Python runtime tracing via `sys.settrace`), `sol_fixer`, corpus scan. EVM
  semantics beyond pure state-write algebra (overflow wraparound, reentrancy ordering,
  `msg.sender` / storage aliasing) are **not** captured by the conservation-invariant first
  step and are deferred — they are the real test of whether *grow-not-fork* holds.
- Risk: if retrofitting Go/TS to the ABC reveals they cannot fit the same socket cleanly,
  the ABC is wrong and must be revised **before** Solidity commits to it.
