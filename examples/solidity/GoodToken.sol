// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// A correct transfer: debits the sender AND credits the receiver,
/// so sum(balances) == totalSupply is preserved.
contract GoodToken {
    mapping(address => uint256) public balances;
    uint256 public totalSupply;

    function transfer(address to, uint256 amount) public {
        require(balances[msg.sender] >= amount, "insufficient balance");
        balances[msg.sender] -= amount; // debit sender
        balances[to] += amount;         // credit receiver
    }
}
