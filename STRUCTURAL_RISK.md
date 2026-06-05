# Structural Risk Report

**Date:** 2026-06-05  
**Cut vertices in call graph:** 363  
**Top-N analysed:** 20  
**Violated:** 5  **Clean:** 15

---

## Violated Functions

### 1. `extract_project_intent` — `intent.py`
| Field | Value |
|---|---|
| Betweenness | 0.0289 |
| Risk score | **0.1157** |
| Pattern | `content_index_unguarded` |

**Missing obligations (3):**
- L1354 — `worst[0]` — WP: `len(worst) > 0` — counterexample: `worst = []`
- L1378 — `worst[0][0]` — WP: `len(worst[0]) > 0` — counterexample: `worst = []`
- L1378 — `worst[0]` — WP: `len(worst) > 0` — counterexample: `worst = []`

**Unguarded sites:**
- L2121 via `_verify_intent_gaps→` arg `inv.applies_to` — `content_index_unguarded`
- L1354 via `_batch_improve→` arg `worst` — `content_index_unguarded`

---

### 2. `_extract_git_log` — `intent.py`
| Field | Value |
|---|---|
| Betweenness | 0.0214 |
| Risk score | **0.0643** |
| Pattern | `content_index_unguarded` |

**Missing obligations (2):**
- L848 — `cluster[0]` — WP: `len(cluster) > 0` — counterexample: `cluster = []`
- L882 — `unique_authors[0]` — WP: `len(unique_authors) > 0` — counterexample: `unique_authors = []`

**Unguarded sites:**
- L848 arg `cluster` — `content_index_unguarded`

---

### 3. `heal_project` — `heal.py`
| Field | Value |
|---|---|
| Betweenness | 0.0171 |
| Risk score | **0.0343** |
| Pattern | `content_index_unguarded` |

**Missing obligations (1):**
- L281 — `project_root.resolve` — WP: `project_root is not None` — counterexample: `project_root = None`

---

### 4. `_collect_cut_vertices` — `adrs.py`
| Field | Value |
|---|---|
| Betweenness | 0.0123 |
| Risk score | **0.0247** |
| Pattern | `content_index_unguarded` |

**Missing obligations (1):**
- L182 — `intent_json.exists` — WP: `intent_json is not None` — counterexample: `intent_json = None`

---

### 5. `run_pipeline` — `pipeline.py`
| Field | Value |
|---|---|
| Betweenness | 0.0109 |
| Risk score | **0.0217** |
| Pattern | `content_index_unguarded` |

**Unguarded sites:**
- L109 via `_call_llm→` arg `msg.content` — `content_index_unguarded`

---

## Clean Functions (15)

| Function | File | Risk Score |
|---|---|---|
| `main` | `scripts/auto_pr.py` | 0.2027 |
| `_interproc_z3` | `pact_interproc.py` | 0.0351 |
| `_mine_pydriller` | `enrich.py` | 0.0295 |
| `walk` | `ts_checker.py` | 0.0243 |
| `LLMResponseEngine.result` | `z3_engine.py` | 0.0183 |
| `check_codebase` | `checker.py` | 0.0175 |
| `sheaf_summary` | `pact_sheaf.py` | 0.0147 |
| `_handle` | `mcp_server.py` | 0.0140 |
| `_run_mypy` | `checker.py` | 0.0139 |
| `_extract_tuples` | `z3_engine.py` | 0.0138 |
| `heal` | `pact_loop.py` | 0.0128 |
| `_Visitor.visit_Assign` | `z3_engine.py` | 0.0118 |
| `structural_risk_report` | `constraint_graph.py` | 0.0115 |
| `_run_semgrep` | `checker.py` | 0.0110 |
| `_tool_pact_topology` | `mcp_server.py` | 0.0107 |

---

## Trend

**Compared to 2026-06-04:** No change — violation set and scores are identical for the fifth consecutive day.

- **New violations:** none
- **Resolved violations:** none
- **Score deltas:** all 0.0000

The 5 persistent violations (`extract_project_intent`, `_extract_git_log`, `heal_project`, `_collect_cut_vertices`, `run_pipeline`) remain unaddressed. All are `content_index_unguarded` pattern — unguarded sequence indexing at call-graph cut vertices. These have been stable across 2026-06-01, 2026-06-02, 2026-06-03, 2026-06-04, and 2026-06-05. These represent the highest-priority targets for the next heal cycle.

---

## Full JSON Report

<details>
<summary>structural_risk_report output (2026-06-05)</summary>

```json
{
  "project": "",
  "n_cut_vertices": 363,
  "n_analysed": 20,
  "n_violated": 5,
  "n_clean": 15,
  "risk_findings": [
    {
      "function": "main",
      "file": "scripts/auto_pr.py",
      "betweenness": 0.2027,
      "risk_score": 0.2027,
      "missing_obligations": [],
      "discharged_obligations": [
        {
          "line": 858,
          "operation": "dynamic[0]",
          "wp": "len(dynamic) > 0",
          "evidence": "`dynamic` positive guard L857"
        }
      ],
      "unguarded_sites": [],
      "guarded_sites": [
        {
          "line": 194,
          "via": "_load_state→",
          "arg": "STATE_FILE.read_text()",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        },
        {
          "line": 153,
          "via": "_dynamic_queue→",
          "arg": "line",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        }
      ],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "extract_project_intent",
      "file": "intent.py",
      "betweenness": 0.0289,
      "risk_score": 0.1157,
      "missing_obligations": [
        {
          "line": 1354,
          "operation": "worst[0]",
          "wp": "len(worst) > 0",
          "counterexample": "worst = []"
        },
        {
          "line": 1378,
          "operation": "worst[0][0]",
          "wp": "len(worst[0]) > 0",
          "counterexample": "worst = []"
        },
        {
          "line": 1378,
          "operation": "worst[0]",
          "wp": "len(worst) > 0",
          "counterexample": "worst = []"
        }
      ],
      "discharged_obligations": [
        {
          "line": 2446,
          "operation": "output.is_dir",
          "wp": "output is not None",
          "evidence": "`output` positive guard L2445"
        },
        {
          "line": 2121,
          "operation": "inv.applies_to[0]",
          "wp": "len(inv.applies_to) > 0",
          "evidence": "`not inv.applies_to` early-exit L2118"
        }
      ],
      "unguarded_sites": [
        {
          "line": 2121,
          "via": "_verify_intent_gaps→",
          "arg": "inv.applies_to",
          "guarded": false,
          "pattern": "content_index_unguarded"
        },
        {
          "line": 1354,
          "via": "_batch_improve→",
          "arg": "worst",
          "guarded": false,
          "pattern": "content_index_unguarded"
        }
      ],
      "guarded_sites": [
        {
          "line": 2239,
          "via": "",
          "arg": "_triage_cache.read_text()",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        },
        {
          "line": 2355,
          "via": "",
          "arg": "p.read_text()",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        }
      ],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_extract_git_log",
      "file": "intent.py",
      "betweenness": 0.0214,
      "risk_score": 0.0643,
      "missing_obligations": [
        {
          "line": 848,
          "operation": "cluster[0]",
          "wp": "len(cluster) > 0",
          "counterexample": "cluster = []"
        },
        {
          "line": 882,
          "operation": "unique_authors[0]",
          "wp": "len(unique_authors) > 0",
          "counterexample": "unique_authors = []"
        }
      ],
      "discharged_obligations": [
        {
          "line": 757,
          "operation": "records[0]",
          "wp": "len(records) > 0",
          "evidence": "`not records` early-exit L748"
        },
        {
          "line": 899,
          "operation": "records[0]",
          "wp": "len(records) > 0",
          "evidence": "`not records` early-exit L748"
        }
      ],
      "unguarded_sites": [
        {
          "line": 848,
          "via": "",
          "arg": "cluster",
          "guarded": false,
          "pattern": "content_index_unguarded"
        }
      ],
      "guarded_sites": [],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_interproc_z3",
      "file": "pact_interproc.py",
      "betweenness": 0.0351,
      "risk_score": 0.0351,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "heal_project",
      "file": "heal.py",
      "betweenness": 0.0171,
      "risk_score": 0.0343,
      "missing_obligations": [
        {
          "line": 281,
          "operation": "project_root.resolve",
          "wp": "project_root is not None",
          "counterexample": "project_root = None"
        }
      ],
      "discharged_obligations": [
        {
          "line": 1402,
          "operation": "output.is_dir",
          "wp": "output is not None",
          "evidence": "`output` positive guard L1401"
        }
      ],
      "unguarded_sites": [],
      "guarded_sites": [
        {
          "line": 1203,
          "via": "",
          "arg": "violations_path.read_text(encoding='utf-8')",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        },
        {
          "line": 1265,
          "via": "",
          "arg": "_cache_path.read_text()",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        }
      ],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_mine_pydriller",
      "file": "enrich.py",
      "betweenness": 0.0295,
      "risk_score": 0.0295,
      "missing_obligations": [],
      "discharged_obligations": [
        {
          "line": 838,
          "operation": "series[0]",
          "wp": "len(series) > 0",
          "evidence": "`not series` early-exit L831"
        }
      ],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_collect_cut_vertices",
      "file": "adrs.py",
      "betweenness": 0.0123,
      "risk_score": 0.0247,
      "missing_obligations": [
        {
          "line": 182,
          "operation": "intent_json.exists",
          "wp": "intent_json is not None",
          "counterexample": "intent_json = None"
        }
      ],
      "discharged_obligations": [
        {
          "line": 184,
          "operation": "intent_json.read_text",
          "wp": "intent_json is not None",
          "evidence": "`intent_json and intent_json.exists()` and-guard L182"
        }
      ],
      "unguarded_sites": [],
      "guarded_sites": [
        {
          "line": 184,
          "via": "",
          "arg": "intent_json.read_text()",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        }
      ],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "walk",
      "file": "ts_checker.py",
      "betweenness": 0.0243,
      "risk_score": 0.0243,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "run_pipeline",
      "file": "pipeline.py",
      "betweenness": 0.0109,
      "risk_score": 0.0217,
      "missing_obligations": [],
      "discharged_obligations": [
        {
          "line": 109,
          "operation": "msg.content[0]",
          "wp": "len(msg.content) > 0",
          "evidence": "`not msg.content` early-exit L107"
        }
      ],
      "unguarded_sites": [
        {
          "line": 109,
          "via": "_call_llm→",
          "arg": "msg.content",
          "guarded": false,
          "pattern": "content_index_unguarded"
        }
      ],
      "guarded_sites": [
        {
          "line": 808,
          "via": "",
          "arg": "intent_path.read_text()",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        },
        {
          "line": 115,
          "via": "_call_llm→",
          "arg": "text",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        }
      ],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "LLMResponseEngine.result",
      "file": "z3_engine.py",
      "betweenness": 0.0183,
      "risk_score": 0.0183,
      "missing_obligations": [],
      "discharged_obligations": [
        {
          "line": 108,
          "operation": "vals[0]",
          "wp": "len(vals) > 0",
          "evidence": "`0 in vals and 1 in vals` and-guard L107"
        }
      ],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "check_codebase",
      "file": "checker.py",
      "betweenness": 0.0175,
      "risk_score": 0.0175,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [
        {
          "line": 626,
          "via": "_run_mypy→",
          "arg": "raw",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        }
      ],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "sheaf_summary",
      "file": "pact_sheaf.py",
      "betweenness": 0.0147,
      "risk_score": 0.0147,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_handle",
      "file": "mcp_server.py",
      "betweenness": 0.014,
      "risk_score": 0.014,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_run_mypy",
      "file": "checker.py",
      "betweenness": 0.0139,
      "risk_score": 0.0139,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [
        {
          "line": 626,
          "via": "",
          "arg": "raw",
          "guarded": true,
          "pattern": "json_loads_unguarded"
        }
      ],
      "z3_sat": "unsat",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_extract_tuples",
      "file": "z3_engine.py",
      "betweenness": 0.0138,
      "risk_score": 0.0138,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "heal",
      "file": "pact_loop.py",
      "betweenness": 0.0128,
      "risk_score": 0.0128,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_Visitor.visit_Assign",
      "file": "z3_engine.py",
      "betweenness": 0.0118,
      "risk_score": 0.0118,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "structural_risk_report",
      "file": "constraint_graph.py",
      "betweenness": 0.0115,
      "risk_score": 0.0115,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_run_semgrep",
      "file": "checker.py",
      "betweenness": 0.011,
      "risk_score": 0.011,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    },
    {
      "function": "_tool_pact_topology",
      "file": "mcp_server.py",
      "betweenness": 0.0107,
      "risk_score": 0.0107,
      "missing_obligations": [],
      "discharged_obligations": [],
      "unguarded_sites": [],
      "guarded_sites": [],
      "z3_sat": "not_run",
      "z3_model": {},
      "load_bearing_constraints": [],
      "beta1": 0,
      "constraint_dag_nodes": 0
    }
  ]
}
```

</details>
