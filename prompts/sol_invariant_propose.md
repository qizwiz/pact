You are auditing a Solidity contract. Propose key GLOBAL invariants — properties that MUST hold for the contract to be correct (conservation: total tracked == sum of parts; solvency; monotonicity / no-free-value; authorization; no-overflow of a critical accumulator).

For EACH invariant, you MUST specify a CONCRETE execution sequence that:
1. Establishes a NON-TRIVIAL pre-state (e.g., stake tokens, deposit funds, mint shares) 
2. Then exercises the target operation with meaningful parameters
3. Ensures require() preconditions are satisfiable from that pre-state

Example: To test an unstake invariant, first stake(amount_s), THEN unstake(amount_u) where amount_u <= amount_s.

Return ONLY a JSON array; each item:
{"id": "inv_1", "statement": "<one precise sentence>", "applies_to": ["fnName"], "setup_sequence": "<concrete call sequence establishing non-trivial state before testing, e.g. 'alice.stake(100); alice.unstake(50);'>", "rationale": "<why it must hold>"}

CONTRACT:
{{src}}