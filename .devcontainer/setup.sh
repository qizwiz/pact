#!/usr/bin/env bash
# Reproducible pact dev env (GitHub Codespaces / any devcontainer).
# Builds in the CLOUD so the local disk never thrashes again.
set -uo pipefail

echo ">>> [1/4] Installing Foundry (forge/cast/anvil)..."
curl -L https://foundry.paradigm.xyz | bash || true
export PATH="$HOME/.foundry/bin:$PATH"
"$HOME/.foundry/bin/foundryup" || foundryup || true

echo ">>> [2/4] Python venv from the pinned lockfile..."
python -m venv .venv
./.venv/bin/pip -q install --upgrade pip
# lockfile first (reproducible); fall back to the known dep set if a pin won't resolve on 3.12
./.venv/bin/pip -q install -r requirements.lock 2>/dev/null \
  || ./.venv/bin/pip -q install z3-solver networkx anthropic python-dotenv requests \
        tree-sitter tree-sitter-solidity

echo ">>> [2b/4] Halmos (symbolic EVM) + HuggingFace (datasets / embeddings)..."
./.venv/bin/pip -q install halmos huggingface_hub datasets || true
echo "    - real-0.8 contract source = clone a recent contest repo, e.g.:"
echo "        git clone --depth 1 https://github.com/Cyfrin/<first-flight> /workspaces/target"
echo "      then: ./.venv/bin/python sol_filter.py /workspaces/target"
echo "    - HF_TOKEN: set as a Codespaces secret (Settings > Codespaces > Secrets)."

echo ">>> [3/4] Verifying with doctor preflight..."
./.venv/bin/python doctor.py || true

echo ">>> [4/4] Done."
echo "    - Set PACT_LLM_API_KEY as a Codespaces secret (Settings > Codespaces > Secrets) — never commit it."
echo "    - For PoC runs: 'forge install foundry-rs/forge-std' in a Foundry project (cloud has network)."
echo "    - Re-run the doctor anytime:  ./.venv/bin/python doctor.py"
