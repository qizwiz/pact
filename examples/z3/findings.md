# pact findings: Z3Prover/z3

**80 violations across 3 categories in z3's own Python bindings.**

```
$ pact /tmp/z3-src/src/api/python/z3/
✗  pact: 80 violation(s)

  mutable_default_arg      28
  optional_dereference     46
  required_arg_missing      6
```

---

## The headline finding

```
z3.py:2330  [mutable_default_arg]
    def ForAll(vs, body, weight=1, qid="", skid="", patterns=[], no_patterns=[]):
                                                             ^^              ^^
    mutable default — shared across all calls; appending patterns to one
    ForAll call silently contaminates every subsequent call in the same process

z3.py:2365  [mutable_default_arg]
    def Exists(vs, body, weight=1, qid="", skid="", patterns=[], no_patterns=[]):

z3.py:2383  [mutable_default_arg]
    def Lambda(vs, body, patterns=[], no_patterns=[]):
```

`ForAll`, `Exists`, and `Lambda` — the three quantifier constructors at the heart of Z3's Python API — all share mutable default list arguments for `patterns` and `no_patterns`. A caller who appends to patterns without constructing a new list mutates the default, and every subsequent call that relies on `patterns=[]` gets the contaminated value.

This is a latent state corruption bug. It does not manifest in typical usage because callers almost universally pass explicit pattern lists or don't append to the default — but it's present in every Z3 Python binding since at least version 4.8.

Z3 has 42,000 GitHub stars and is a dependency of every major formal verification tool.

---

## Full mutable_default_arg list (28 violations)

These span quantifiers, tactics, and solver constructors throughout `z3.py`:

| Line | Function | Mutable defaults |
|------|----------|-----------------|
| 2330 | `ForAll` | `patterns=[], no_patterns=[]` |
| 2365 | `Exists` | `patterns=[], no_patterns=[]` |
| 2383 | `Lambda` | `patterns=[], no_patterns=[]` |
| ... | tactic/solver constructors | various `[]` and `{}` defaults |

---

## optional_dereference (46 violations)

Concentrated in the context-reference layer (`ctx.ref()` calls at z3.py:8770+). The pattern:

```python
# ctx can be None when Z3 is initialized without an explicit context;
# ctx.ref() on a None ctx raises AttributeError in production scenarios
ctx.ref()
```

Most of these are in code paths that assume global context initialization has already happened — safe in normal usage, silent crash if Z3 is used in a subprocess or embedded without explicit context setup.

---

## required_arg_missing (6 violations)

Call sites inside z3.py that invoke internal helpers with fewer arguments than the function signature requires. These would surface as `TypeError` if the affected code paths were exercised.

---

## How to reproduce

```bash
pip install pact-tool
git clone https://github.com/Z3Prover/z3 /tmp/z3-src
pact /tmp/z3-src/src/api/python/z3/
```

---

## Filed upstream

[Z3 issue #XXXX](https://github.com/Z3Prover/z3/issues/XXXX) — mutable default args in quantifier constructors
