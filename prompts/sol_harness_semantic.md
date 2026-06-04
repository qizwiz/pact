You fill the SEMANTIC body of a Halmos invariant check. The harness is ALREADY built for you:
the contract-under-test is deployed as `c`, and any ERC20 asset its constructor needed is
deployed as `asset0` (asset1, ...) with the caller (address(this)) ALREADY funded and approved.
Do NOT deploy, mint, or approve anything — only fill the slots below. The acting account is
always address(this).

Return ONE JSON object with exactly these keys:
- "params": symbolic inputs Halmos explores, e.g. ["uint256 amount","uint256 donation"]
- "bounds": require() preconditions on params, e.g. ["amount > 0 && amount < type(uint64).max"]
- "setup": statements establishing a NON-TRIVIAL pre-state (run as address(this)), e.g. ["uint256 s1 = c.deposit(1, address(this)); require(s1 > 0);"]
- "adversarial": ONE optional bounded ENVIRONMENT step the contract is exposed to — a DIRECT asset transfer-in to the contract (donation: "asset0.transfer(address(c), donation);") — or null if none
- "action": statements performing the operation(s) under test with the symbolic args
- "assert_expr": the invariant that MUST hold after the action, e.g. "c.balanceOf(address(this)) - bef > 0"

Pick the invariant most likely to expose a real economic bug: conservation (value not
created/destroyed), solvency (assets >= obligations), no-zero-share (a positive deposit must
mint > 0 shares), or monotonicity (no free value). If the contract holds an external asset and
an accounting ratio, STRONGLY consider modelling a donation in "adversarial".

Return ONLY the JSON, no prose, no fences.

CONTRACT ({{name}}):
{{src}}

DEPLOYED ASSETS (already funded + approved for address(this)): {{assets}}
