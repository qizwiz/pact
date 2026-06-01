// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// A BUGGY transfer: credits the receiver but forgets to debit the sender,
/// so tokens are minted from nothing and sum(balances) drifts above totalSupply.
contract BadToken {
    mapping(address => uint256) public balances;
    uint256 public totalSupply;

    function transfer(address to, uint256 amount) public {
        // BUG: no `balances[msg.sender] -= amount;` — value created from nothing
        balances[to] += amount;
    }
}
