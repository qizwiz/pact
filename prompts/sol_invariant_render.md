Express each of these vetted invariants as a Halmos symbolic test. Write ONE test contract:
- SPDX + pragma ^0.8.20
- import {{{name}}} from "../src/{{name}}.sol";
- contract Invariants { {{name}} c; address a = address(0xA11CE); address b = address(0xB0B); constructor() { c = new {{name}}(); } ... }
- one `function check_<invId>(<symbolic args>) public` per invariant.

HARD DISCIPLINE (to avoid false positives AND false negatives):
  * Use ONLY the distinct concrete accounts address(this), a, b — NEVER symbolic addresses (symbolic addresses can alias and produce spurious violations).
  * For any conservation/sum invariant, FIRST `require(` the invariant HOLDS on the starting state `)`, THEN perform the operation(s), THEN `assert(` it still holds `)`. Never assert a global sum without establishing it held before.
  * Use require() for input assumptions; assert() for the invariant.
  * CRITICAL: EXERCISE a realistic multi-step sequence to establish non-trivial pre-state BEFORE testing state-changing operations. Example: to test unstake(u), FIRST call stake(s) with s>0 so that staked>=u is satisfiable and the unstake logic actually executes. Do NOT test operations that require non-zero balances on the zero-initialized contract state — build up necessary balances first within the test function.
  * For operations that decrement balances (withdraw, unstake, transfer-out, burn), the test MUST first call the corresponding increment operation (deposit, stake, transfer-in, mint) with sufficient symbolic amount to make the decrement preconditions satisfiable beyond the trivial zero case.
  * MANDATORY FOR VAULT / RATIO / ORACLE INVARIANTS: Model the adversarial ENVIRONMENT by including ONE bounded step that exercises externally-triggered state changes the contract is exposed to: direct token transfer-in (donation), token balance inflation, or symbolic oracle/price update. Insert this as a single step in the inductive sequence: e.g. deposit(amount1); [DONATE/INFLATE: token.transfer(address(c), donation) or oracle price shift]; deposit(amount2); assert invariant. This catches donation-inflation bugs and oracle manipulation. Keep it a single bounded adversarial step, NOT a full attacker contract, for symbolic execution tractability.

INVARIANTS:
{{invariants}}

CONTRACT (src/{{name}}.sol):
{{src}}

Return ONLY the Solidity test source — no prose, no fences.