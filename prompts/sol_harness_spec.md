You fill the SEMANTIC slots of a Halmos invariant-test SPEC. The harness scaffold is GENERATED
for you: it deploys the contract-under-test and any mock ERC20 you declare, and AUTOMATICALLY
funds + approves the caller (address(this)). Do NOT write any minting, approval, or deployment —
only the slots below. The acting account is always address(this), already funded and approved.

Return ONE JSON object with exactly these keys:
- "statement": a ONE-LINE natural-language statement of the invariant you are testing (so an intent judge can vet it), e.g. "a positive deposit must mint more than zero shares"
- "contract": "{{name}}"
- "imports": names to import from the src besides {{name}} (e.g. ["ERC20","IERC20"] if you declare a mock ERC20); [] if none
- "mocks": [] or [{"name":"token","kind":"erc20"}] — declare a mock ERC20 ONLY if the contract's constructor needs an ERC20 asset
- "ctor_args": constructor-argument expression, may reference a declared mock (e.g. "IERC20(address(token))"); "" if the constructor takes none
- "params": symbolic inputs Halmos will explore, e.g. ["uint256 amount","uint256 donation"]
- "bounds": require() preconditions on params, e.g. ["amount > 0 && amount < type(uint64).max"]
- "setup": statements establishing a NON-TRIVIAL pre-state, run as address(this), e.g. ["uint256 s1 = c.deposit(1, address(this)); require(s1 > 0);"]
- "adversarial": ONE optional bounded ENVIRONMENT step the contract is exposed to — a direct token transfer-in to the contract (donation), or an oracle/price write — as a single statement string; null if none. e.g. "token.transfer(address(c), donation);"
- "action": statements performing the operation(s) under test with the symbolic args
- "assert_expr": the invariant that MUST hold after the action, e.g. "c.balanceOf(address(this)) - bef > 0"

Pick the invariant most likely to expose a real economic bug: conservation (value not created/destroyed), solvency (assets >= obligations), no-zero-share (a positive deposit must mint > 0 shares), or monotonicity (no free value). If the contract holds an external asset and an accounting ratio, STRONGLY consider modelling a donation in "adversarial" — a direct transfer-in can inflate the ratio.

Return ONLY the JSON, no prose, no fences.

CONTRACT ({{name}}):
{{src}}
