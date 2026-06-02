Express each of these vetted invariants as a Halmos symbolic test. Write ONE test contract:
- SPDX + pragma ^0.8.20
- import {{{name}}} from "../src/{{name}}.sol";
- contract Invariants { {{name}} c; address a = address(0xA11CE); address b = address(0xB0B); constructor() { c = new {{name}}(); } ... }
- one `function check_<invId>(<symbolic args>) public` per invariant.

HARD DISCIPLINE (to avoid false positives — Halmos starts from the freshly-deployed state):
  * Use ONLY the distinct concrete accounts address(this), a, b — NEVER symbolic addresses (symbolic addresses can alias and produce spurious violations).
  * For any conservation/sum invariant, FIRST `require(` the invariant HOLDS on the starting state `)`, THEN perform the operation(s), THEN `assert(` it still holds `)`. Never assert a global sum without establishing it held before.
  * Use require() for input assumptions; assert() for the invariant.

INVARIANTS:
{{invariants}}

CONTRACT (src/{{name}}.sol):
{{src}}

Return ONLY the Solidity test source — no prose, no fences.
