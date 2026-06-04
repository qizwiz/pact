A Halmos test harness is ALREADY built: the contract-under-test `c` is deployed, the caller
address(this) is funded with the asset and has approved `c`, and `vm` is available (forge-std Test).
The asset token(s): {{assets}}. Symbolic args available: uint256 a, uint256 b.

Write ONLY the BODY of `function check_inv(uint256 a, uint256 b)`:
- bound the args with require() to small concrete values (require(a <= 10); require(b <= 10);)
- bound away zero if needed (require(a > 0);)
- call c's deposit/stake function ONCE with bounded arg `a` to establish minimal state
- assert ONLY: asset.balanceOf(address(c)) >= a

DO NOT:
- call vm.warp, vm.roll, vm.prank, or any cheatcodes except require
- call reward, earned, claim, withdraw, redeem, unstake, or any function beyond the initial deposit/stake
- perform transfers, approvals, or any token operations on `c` or asset beyond the single deposit/stake call
- assert anything other than the single balance >= deposited amount inequality
- add try/catch, loops, or conditional branches

This tests the most basic solvency property: after depositing `a` tokens, the contract holds at least `a` tokens.
Keep it absolutely minimal for fast symbolic execution. Use the EXACT function name from the contract (e.g., deposit, stake, mint).

Return exactly this format, no fences:
STATEMENT: <one-line plain-english invariant>
BODY:
<the Solidity statements of the body only — no function signature, no contract>

CONTRACT ({{name}}):
{{src}}