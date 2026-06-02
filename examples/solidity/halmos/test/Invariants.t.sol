// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Sneaky} from "../src/Sneaky.sol";
import {Overflow} from "../src/Overflow.sol";
import {Good} from "../src/Good.sol";

contract Invariants {
    Sneaky s;
    Overflow o;
    Good g;

    constructor() {
        s = new Sneaky();
        o = new Overflow();
        g = new Good();
    }

    function check_conservation(address to, uint256 amount) public {
        require(to != address(this) && to != address(0));
        uint256 pre = s.balanceOf(address(this)) + s.balanceOf(to);
        s.transfer(to, amount);
        uint256 post = s.balanceOf(address(this)) + s.balanceOf(to);
        assert(post == pre);   // Sneaky: VIOLATED
    }

    function check_reward_monotone(uint256 base, uint256 bonus) public view {
        assert(o.reward(base, bonus) >= base);   // Overflow: VIOLATED
    }

    function check_conservation_good(address to, uint256 amount) public {
        require(to != address(this) && to != address(0));
        uint256 pre = g.balanceOf(address(this)) + g.balanceOf(to);
        g.transfer(to, amount);
        uint256 post = g.balanceOf(address(this)) + g.balanceOf(to);
        assert(post == pre);   // Good: should PROVE
    }

    function check_reward_good(uint256 base, uint256 bonus) public view {
        assert(g.reward(base, bonus) >= base);   // Good: checked add reverts on overflow -> PROVE
    }
}
