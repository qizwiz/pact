A Halmos harness is built: `c` is deployed, address(this) is funded with the asset
and approved. Asset token(s): {{assets}}. Symbolic args: uint256 a, uint256 b.

The harness provides NO cheatcodes — `vm` is NOT available. Use require() only; do NOT call vm.warp,
vm.prank, or any cheatcode (the test will not compile).

Write the BODY of check_inv(uint256 a, uint256 b): bound args with require(); establish a non-trivial
pre-state by calling c's functions as address(this); assert ONE conservation/solvency invariant that
HOLDS on a correct contract but is VIOLATED if it mis-accounts. The assert MUST faithfully encode your
STATEMENT. Consider relating different quantities (held tokens vs issued shares, withdrawable vs
deposited, no-zero-share, conservation). Minimal valid Solidity, require() only.

FAITHFULNESS FIRST: assert ONLY a property the contract GENUINELY guarantees. Do NOT invent a
property that merely happens to hold in this harness. In particular, for an ERC4626 vault,
`totalSupply (shares) <= totalAssets` is NOT an invariant — shares and assets have different units
and the ratio drifts with yield/loss/fees. Asserting it is UNFAITHFUL and will (rightly) be
rejected. Pick the real promised property even if it requires conversion.

PROVER BOUNDARY (informational, NOT a license to lie): the symbolic prover cannot discharge
nonlinear arithmetic — share<->asset CONVERSIONS (convertToShares / convertToAssets / previewDeposit
/ previewRedeem / mulDiv) make it EXHAUST. If the contract's genuine invariant is linear, assert it
linearly. If the genuine invariant REQUIRES conversion, assert it faithfully anyway — an honest
"exhausted" is correct; do NOT substitute an unfaithful linear invariant just to force a proof.

Return exactly:
STATEMENT: <one-line>
BODY:
<solidity statements only>

CONTRACT ({{name}}):
{{src}}
