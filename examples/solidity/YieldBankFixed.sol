// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @notice Users deposit `asset` for shares; the bank accrues yield (via addYield);
///         users redeem shares for a proportional amount of assets (principal + yield).
contract YieldBankFixed {
    IERC20 public asset;
    uint256 public totalShares;
    uint256 public totalAssets; // tracked internally (not balanceOf)
    mapping(address => uint256) public shares;

    constructor(IERC20 _asset) {
        asset = _asset;
    }

    function deposit(uint256 amount) external {
        asset.transferFrom(msg.sender, address(this), amount);
        uint256 newShares = totalShares == 0 ? amount : (amount * totalShares) / totalAssets;
        shares[msg.sender] += newShares;
        totalShares += newShares;
        totalAssets += amount;
    }

    function addYield(uint256 amount) external {
        asset.transferFrom(msg.sender, address(this), amount);
        totalAssets += amount; // raises the asset value of every share
    }

    function withdraw(uint256 shareAmount) external {
        uint256 assets = (shareAmount * totalAssets) / totalShares;
        shares[msg.sender] -= shareAmount;
        totalShares -= shareAmount;
        totalAssets -= assets;
        asset.transfer(msg.sender, assets);
    }
}
