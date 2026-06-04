import audit, os
CASES = [
    ("VulnVault",   "/tmp/VulnVault_flat.sol",   "bug (no mitigation)"),
    ("OffsetVault", "/tmp/OffsetVault_flat.sol", "protected (offset=6)"),
    ("CleanToken",  "/tmp/CleanToken_flat.sol",  "clean ERC20"),
]
print("real-OZ benchmark: does the chain flag vuln, and AVOID flagging the protected/clean?\n")
for name, path, truth in CASES:
    if not os.path.exists(path):
        print(f"  {name}: missing"); continue
    res = audit.audit_contract(open(path).read() and name, open(path).read())
    print(f"  {name:<12} [{truth:<22}] -> {res.get('status')}")
    print(f"        inv: {res.get('statement','')[:90]}")
    if res.get('reason'): print(f"        reason: {res.get('reason')[:90]}")
