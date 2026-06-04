A Halmos harness is built: `c` is deployed, address(this) is funded with the asset
and approved. Asset token(s): {{assets}}. Symbolic args: uint256 a, uint256 b.

The harness provides NO cheatcodes — `vm` is NOT available. Use require() only; do NOT call vm.warp,
vm.prank, or any cheatcode (the test will not compile).

Write the BODY of check_inv(uint256 a, uint256 b): bound args with require(); establish a non-trivial
pre-state by calling c's functions as address(this); assert ONE conservation/solvency invariant that
HOLDS on a correct contract but is VIOLATED if it mis-accounts. The assert MUST faithfully encode your
STATEMENT. Consider relating different quantities (held tokens vs issued shares, withdrawable vs
deposited, no-zero-share, conservation). Minimal valid Solidity, require() only.

Return exactly:
STATEMENT: <one-line>
BODY:
<solidity statements only>

CONTRACT ({{name}}):
{{src}}
