You are auditing a Solidity contract. Propose key GLOBAL invariants — properties that MUST hold for the contract to be correct (conservation: total tracked == sum of parts; solvency; monotonicity / no-free-value; authorization; no-overflow of a critical accumulator).

For EACH invariant, you MUST specify a CONCRETE execution sequence that:
1. Establishes a NON-TRIVIAL pre-state (e.g., stake tokens, deposit funds, mint shares) 
2. **Injects ONE bounded adversarial environment step (direct token transfer/donation that inflates ratios, or symbolic oracle price read) BETWEEN setup and target operation**
3. Then exercises the target operation with meaningful parameters
4. Ensures require() preconditions are satisfiable from that pre-state

Example for vault donation attack: alice.deposit(100); attacker.transfer(token, vault, d); alice.deposit(amount); // assert shares minted > 0
Example for oracle manipulation: user.open(collateral_c); oracle.setPrice(p); user.liquidate(); // assert bounds respected

Return ONLY a JSON array; each item:
{"id": "inv_1", "statement": "<one precise sentence>", "applies_to": ["fnName"], "setup_sequence": "<concrete call sequence: setup, THEN adversarial step, THEN target operation, e.g. 'alice.deposit(100); attacker.transfer(underlying, address(this), d); bob.deposit(50);'>", "rationale": "<why it must hold despite adversarial environment>"}

CONTRACT:
{{src}}