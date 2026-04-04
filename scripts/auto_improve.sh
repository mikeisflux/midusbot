#!/usr/bin/env bash
# auto_improve.sh — Autonomous Claude Code improvement loop
# Runs on a cron schedule. Claude checks the bot, fixes issues,
# improves performance, commits changes, and restarts if needed.
# Logs everything to logs/auto_improve_YYYY-MM-DD.log

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$REPO_DIR/logs/auto_improve_$(date +%Y-%m-%d).log"
LOCK_FILE="/tmp/midusbot_auto_improve.lock"

mkdir -p "$REPO_DIR/logs"

# Prevent overlapping runs
if [ -f "$LOCK_FILE" ]; then
    echo "[$(date)] auto_improve already running (lock exists), skipping" >> "$LOG_FILE"
    exit 0
fi
touch "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT

echo "" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"
echo "[$(date)] auto_improve starting" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

# Load env vars (API key etc.)
if [ -f "$REPO_DIR/.env" ]; then
    set -o allexport
    source "$REPO_DIR/.env"
    set +o allexport
fi

# Ensure API key is set
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "[$(date)] ERROR: ANTHROPIC_API_KEY not set — aborting" >> "$LOG_FILE"
    exit 1
fi

PROMPT="You are the autonomous maintenance agent for Midusbot, a Polymarket trading bot.
Your job is to keep the bot healthy, profitable, and bug-free without any human intervention.

Working directory: $REPO_DIR

Do ALL of the following in order:

1. CHECK FOR ERRORS: Read the last 200 lines of today's log file at logs/bot_\$(date +%Y-%m-%d).log.
   Look for ERROR, Exception, Traceback, SLOW, WS-SILENT, ADVERSE patterns.
   If you find critical errors (exceptions causing crashes, repeated failures), fix them.

2. CHECK PERFORMANCE: Read data/journal.jsonl (last 50 trades) and data/session_log.jsonl (last 100 rows).
   Compute win rate and P&L. If win rate < 50% over last 30 trades, run the backtest and tune thresholds.

3. CHECK ALPHA DECAY: Run:
   python3 -c \"from src.learner import Learner; l = Learner(); print(l.alpha_decay_report())\"
   If alpha is decaying (win rate dropping), tighten thresholds by 10-15%.

4. CHECK FEATURE IMPORTANCE: Run:
   python3 -c \"from src.learner import Learner; l = Learner(); print(l.feature_importance())\"
   If any signal has near-zero correlation, consider disabling it in strategy.py.

5. FIX ANY BUGS: If you found errors in step 1, read the relevant source file and fix the root cause.
   Run python3 -m py_compile on every file you modify. Do not skip this.

6. COMMIT CHANGES: If you made any changes, commit them:
   git add -u && git commit -m 'auto-improve: <describe what you fixed/tuned>'
   Then push: git push origin \$(git branch --show-current)

7. RESTART IF NEEDED: If you changed any source files, restart the bot:
   pm2 restart midusbot

8. REPORT: Print a concise summary of what you found and what you changed.
   Format: FOUND: <issues> | CHANGED: <files> | RESULT: <outcome>

Rules:
- NEVER set DRY_RUN=false
- NEVER change ENTRY_PRICE_GUARD above 0.54
- NEVER change KELLY_FRACTION above 0.25
- NEVER delete data/ files
- If nothing needs fixing, just print: ALL OK — no changes needed"

echo "[$(date)] Running Claude..." >> "$LOG_FILE"

OUTPUT=$(claude \
    --dangerously-skip-permissions \
    --print \
    "$PROMPT" \
    2>&1)

echo "$OUTPUT" >> "$LOG_FILE"
echo "[$(date)] auto_improve complete" >> "$LOG_FILE"

# Send summary to Discord if webhook is configured
DISCORD_WEBHOOK="${DISCORD_WEBHOOK_URL:-}"
if [ -n "$DISCORD_WEBHOOK" ]; then
    SUMMARY=$(echo "$OUTPUT" | grep -E "^(FOUND|CHANGED|RESULT|ALL OK)" | tail -5 || echo "$OUTPUT" | tail -3)
    PAYLOAD=$(python3 -c "
import json, sys
msg = sys.stdin.read().strip()
print(json.dumps({'content': f'**[auto-improve]** {msg}'}))
" <<< "$SUMMARY")
    curl -s -X POST "$DISCORD_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" > /dev/null || true
fi
