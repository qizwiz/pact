# ADR-004: YAML Declarative Rules with `family:` Key for Framework Lineages

**Status**: Proposed  
**Date**: 2026-05-15  

---

## Context

pact currently encodes failure modes as Python `FailureMode` instances in
`failure_mode.py`. Each mode has a `name`, `description`, and a `check`
function — a Python callable that interrogates the call graph.

As the number of modes grows and we start seeing cross-framework patterns
(Django ORM + SQLAlchemy + Peewee all exhibit the "partial-save without scope"
bug; LangChain + LlamaIndex + AutoGen all exhibit "LLM response unguarded"),
the monolithic Python file becomes hard to extend. Adding a new mode means:

1. Writing a new AST scanner function
2. Registering a new `FailureMode` instance
3. Adding tests
4. No structural relationship to existing related modes

There is also no way to say "this constraint applies to any ORM, not just
Django" without duplicating logic.

---

## Decision

**Failure modes are declared in YAML with a `family:` key that captures
ancestral framework lineages.**

Each YAML rule file specifies:
```yaml
name: save_without_update_fields
family: partial-update-without-scope
description: |
  Model.save() without update_fields re-writes every column, clobbering
  concurrent partial updates.
frameworks:
  - django.db.models.Model
  - sqlalchemy.orm.Session.flush
  - peewee.Model.save
tla_spec: docs/tla/SaveWithoutUpdateFields.tla
adr: docs/adr/ADR-001-graph-first-architecture.md
check: pact.failure_mode.save_without_update_fields
```

The `family:` key groups rules into constraint families. All rules in the
same family share the same temporal property (TLA+ spec), the same ADR, and
the same fix pattern. The only difference is which frameworks they target.

### Family examples

| Family                     | Members                                                          |
|----------------------------|------------------------------------------------------------------|
| `partial-update-without-scope` | save_without_update_fields (Django, SQLAlchemy, Peewee)    |
| `optional-access-unguarded`    | optional_dereference, llm_response_unguarded                |
| `mutable-shared-state`         | mutable_default_arg, class-level-mutable                    |
| `async-not-consumed`           | missing_await, missing-asyncio-run                          |
| `swallow-all-exceptions`       | bare_except                                                 |

---

## Why `family:` rather than inheritance

Python class inheritance would create the same grouping but:
- It ties the rule schema to a specific Python class hierarchy
- It cannot be consumed by non-Python tooling (tree-sitter queries, IDE plugins)
- Adding a new framework member requires editing Python, not YAML

YAML + `family:` is data, not code. A contributor can add a new framework to
an existing family by adding one line, without understanding the checker
implementation.

---

## The schema

```yaml
# Rule schema v1
name: <string>             # unique identifier, snake_case
family: <string>           # constraint family slug
description: <string>      # one-paragraph human explanation
frameworks: [<string>]     # qualified class/method names that trigger this rule
severity: info|warn|error  # default: error
tla_spec: <path>           # TLA+ spec proving correctness
adr: <path>                # ADR explaining the architectural decision
check: <dotted.path>       # Python function implementing the check
suggest: <string>          # one-line fix suggestion (used by --suggest flag)
```

---

## Current status

This ADR is **Proposed** — the YAML schema is designed but not yet implemented.
The existing Python `FailureMode` instances remain authoritative. The YAML
layer will be added as a thin schema-validation + documentation layer first,
then gradually take over rule registration.

**Migration path:**
1. Add `docs/rules/` directory with one `.yaml` per mode
2. Validate YAML against the schema (jsonschema) in CI
3. Auto-generate the `FailureMode` list from YAML at import time
4. Deprecate direct `FailureMode` instantiation in `failure_mode.py`

---

## Alternatives Considered

**Separate Python files per rule** — one file per FailureMode, auto-discovered
by the loader. Cleaner than a monolith, but still Python-first. Doesn't solve
the framework-family problem.

**tree-sitter query files** — each rule is a `.scm` query file. Fast and
language-agnostic, but cannot express path constraints (graph properties). A
tree-sitter query finds a node; pact needs to find a path.

**OPA/Rego policies** — policy-as-code, Datalog-like. Compelling for large
organizations with policy review workflows. Overkill for a single-developer
tool; Rego is unfamiliar to most Python developers.
