You are auditing a Solidity contract. Propose its key GLOBAL invariants — properties that MUST hold for the contract to be correct (conservation: total tracked == sum of parts; solvency; monotonicity / no-free-value; authorization; no-overflow of a critical accumulator).

Return ONLY a JSON array; each item:
{"id": "inv_1", "statement": "<one precise sentence>", "applies_to": ["fnName"], "rationale": "<why it must hold>"}

CONTRACT:
{{src}}
