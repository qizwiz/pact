Your generated Halmos test failed ({{kind}}). Fix it and return ONLY the corrected Solidity
`contract Invariants {{ ... }}` with check_ functions. Keep the ACTOR MODEL intact: address(this)
must be funded and approved BEFORE any call, or every path reverts.

- {{kind}} == compile : fix the compile error shown below (watch ERC20 base constructor arity:
  OpenZeppelin is (name, symbol); Solmate is (name, symbol, decimals)).
- {{kind}} == revert  : every symbolic path reverted, so the test is vacuous. Mint to address(this)
  and approve the contract in the constructor BEFORE any deposit; establish a real non-zero
  pre-state inside the check function. Keep the same invariant.

SIGNAL:
{{signal}}

CURRENT TEST:
{{harness}}
