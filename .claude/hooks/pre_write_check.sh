#!/usr/bin/env bash
# Pre-Write/Edit safety hook — warns before modifying critical trading logic.
# Claude Code calls this before any Write or Edit tool use.
# Input: JSON on stdin with {"tool_input": {"file_path": "...", ...}}

set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

# Warn on risk.py changes (Kelly formula, capital floor)
if echo "$FILE" | grep -qE 'risk\.py$'; then
    echo "NOTE: Editing risk.py — verify Kelly fraction stays <= 0.25 and capital floor logic is intact." >&2
fi

# Warn on strategy.py changes (entry guards)
if echo "$FILE" | grep -qE 'strategy\.py$'; then
    echo "NOTE: Editing strategy.py — verify ENTRY_PRICE_GUARD is not raised above 0.54." >&2
fi

# Block writes to data/ directory (data is runtime-generated, not code)
if echo "$FILE" | grep -qE '^data/'; then
    echo '{"decision":"block","reason":"Blocked: data/ files are runtime-generated. Edit src/ or config.py instead."}' >&2
    exit 2
fi

# Block writes to .env (contains secrets)
if echo "$FILE" | grep -qE '\.env$'; then
    echo '{"decision":"block","reason":"Blocked: do not write .env files. Edit .env.example instead."}' >&2
    exit 2
fi

exit 0
