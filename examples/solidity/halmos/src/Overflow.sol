// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Overflow {
    // reward must never be less than base; unchecked add can wrap (uint256 overflow)
    function reward(uint256 base, uint256 bonus) external pure returns (uint256) {
        unchecked {
            return base + bonus;  // BUG: wraps for large inputs
        }
    }
}
