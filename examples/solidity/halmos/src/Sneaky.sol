// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Sneaky {
    mapping(address => uint256) public balanceOf;
    uint256 public totalSupply;

    constructor() {
        totalSupply = 1000 ether;
        balanceOf[msg.sender] = totalSupply;
    }

    function transfer(address to, uint256 amount) external {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        balanceOf[msg.sender] -= amount / 2;  // BUG: under-debit; credits full amount below
        balanceOf[to] += amount;
    }
}
