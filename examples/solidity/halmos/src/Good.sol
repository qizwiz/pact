// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Good {
    mapping(address => uint256) public balanceOf;
    uint256 public totalSupply;

    constructor() {
        totalSupply = 1000 ether;
        balanceOf[msg.sender] = totalSupply;
    }

    function transfer(address to, uint256 amount) external {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        balanceOf[msg.sender] -= amount;  // correct: full debit
        balanceOf[to] += amount;
    }

    function reward(uint256 base, uint256 bonus) external pure returns (uint256) {
        return base + bonus;  // checked arithmetic (0.8): reverts on overflow, never wraps
    }
}
