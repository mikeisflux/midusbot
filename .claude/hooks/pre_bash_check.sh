#!/usr/bin/env bash
# Pre-Bash safety hook — blocks dangerous commands before execution.
# Claude Code calls this before running any Bash tool.
# Input: JSON on stdin with {"tool_input": {"command": "..."}}

set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

# Block live trading activation
if echo "$CMD" | grep -qE 'DRY_RUN=false'; then
    echo '{"decision":"block","reason":"Blocked: DRY_RUN=false would enable live trading. Set this only in .env, not via command line."}' >&2
    exit 2
fi

# Block destructive file operations on data/
if echo "$CMD" | grep -qE 'rm .*data/|rm -rf'; then
    echo '{"decision":"block","reason":"Blocked: deleting data/ files would destroy training data and trade history."}' >&2
    exit 2
fi

# Block direct bot execution (must be done manually by the user)
if echo "$CMD" | grep -qE '^python main\.py\b'; then
    echo '{"decision":"block","reason":"Blocked: use the terminal to start the bot manually. Claude should not launch live processes."}' >&2
    exit 2
fi

# Warn about position API calls (manual sell endpoint)
if echo "$CMD" | grep -qE 'curl.*localhost.*positions'; then
    echo "WARNING: curl to /positions endpoint — this may trigger real order operations." >&2
fi

exit 0
