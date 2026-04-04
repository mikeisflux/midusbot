#!/usr/bin/env bash
# Post-Write/Edit audit hook — runs py_compile on modified Python files.
# Catches syntax errors immediately after any file is written or edited.
# Input: JSON on stdin with {"tool_input": {"file_path": "...", ...}}

set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

# Only check Python files
if echo "$FILE" | grep -qE '\.py$'; then
    if python3 -m py_compile "$FILE" 2>&1; then
        echo "✓ Syntax OK: $FILE" >&2
    else
        echo "✗ Syntax ERROR in $FILE — please fix before proceeding." >&2
        # Don't block (exit 2 would block) — just warn so Claude sees the error
        exit 0
    fi
fi

exit 0
