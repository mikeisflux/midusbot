#!/usr/bin/env bash
# auto_improve.sh — Autonomous Claude Code improvement loop
# Runs on a cron schedule. Claude checks the bot, fixes issues,
# improves performance, and proactively engineers new improvements.
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

PROMPT="You are the autonomous engineering agent for Midusbot, a Polymarket 5-minute UpDown trading bot.
You have two roles: (1) maintenance engineer — keep the bot healthy and bug-free, and (2) senior quant developer — proactively improve it to make as much money as possible.

Working directory: $REPO_DIR
Current time: $(date)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 1 — MAINTENANCE (fix what's broken)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. CHECK ERRORS: Read the last 200 lines of logs/bot_$(date +%Y-%m-%d).log.
   Find ERROR, Exception, Traceback, SLOW, WS-SILENT, ADVERSE, BOT-DETECT patterns.
   Fix any critical errors — read the relevant source file, patch the root cause, verify with py_compile.

2. CHECK PERFORMANCE: Read data/journal.jsonl (last 50 trades) and data/session_log.jsonl (last 100 rows).
   Compute: win rate, total P&L, per-asset breakdown, avg hold time.
   If win rate < 50% over last 30 trades, run the backtest threshold sweep and update data/analyst_params.json.

3. CHECK ALPHA DECAY:
   python3 -c \"from src.learner import Learner; l = Learner(); print(l.alpha_decay_report())\"
   If win rate is trending down over time, tighten thresholds by 10-15%.

4. CHECK FEATURE IMPORTANCE:
   python3 -c \"from src.learner import Learner; l = Learner(); print(l.feature_importance())\"
   Disable any signal with near-zero or negative correlation in strategy.py.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 2 — IMPROVEMENT (make more money)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Now ask yourself this question and answer it with code:

\"Based on all the logs, financial tracking data, trade journal, session outcomes, signal accuracy,
alpha decay trends, and the full source code — as a senior quant developer whose sole goal is
maximizing profit: what is the single highest-impact improvement I can make right now?\"

Think like a senior developer:
- Look at what's actually losing money (adverse selection? late entries? wrong assets? regime misclassification?)
- Look at what's working well and how to do more of it
- Look for logic bugs that silently cost money (wrong timing, missed exits, bad sizing)
- Look for signal improvements (better confirmation, tighter filters, new data sources)
- Look for execution improvements (order placement, latency, fill quality)
- Read the relevant source files before changing anything
- Make ONE focused, high-quality improvement — not ten mediocre ones
- If the improvement is speculative, add an A/B test flag for it instead of forcing it live

Examples of good improvements:
- Tightening entry timing if late entries are losing more than early ones
- Adding a volatility filter if choppy-regime losses are high
- Improving early exit thresholds based on actual outcome distribution
- Fixing position sizing if large positions are losing more than small ones
- Adding a new confirmation signal if feature importance shows current signals are weak
- Rewriting a hot path that's causing SLOW warnings

Examples of bad improvements (do NOT do these):
- Raising ENTRY_PRICE_GUARD above 0.54
- Raising KELLY_FRACTION above 0.25
- Removing safety guards
- Making speculative changes with no data backing them up

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 3 — SHIP IT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

5. For EVERY file you modified:
   python3 -m py_compile <file>
   Do not skip this. Fix any syntax errors before committing.

6. Commit all changes:
   git add -u
   git commit -m 'auto-improve: <maintenance fixes> | <improvement made and why>'
   git push origin \$(git branch --show-current)

7. Restart if source files changed:
   pm2 restart midusbot

8. Print your report in this exact format:
   MAINTENANCE: <what was broken and fixed, or 'nothing broken'>
   IMPROVEMENT: <what you improved and the data/reasoning behind it>
   CHANGED: <list of files modified>
   RESULT: <expected impact on win rate / P&L>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD RULES — never break these
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- NEVER set DRY_RUN=false
- NEVER change ENTRY_PRICE_GUARD above 0.54
- NEVER change KELLY_FRACTION above 0.25
- NEVER delete data/ files
- NEVER make changes you cannot justify with data from the logs or journal
- If you find nothing to improve: print 'ALL OK — no changes needed' and exit cleanly"

echo "[$(date)] Running Claude (maintenance + improvement pass)..." >> "$LOG_FILE"

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
    SUMMARY=$(echo "$OUTPUT" | grep -E "^(MAINTENANCE|IMPROVEMENT|CHANGED|RESULT|ALL OK)" | head -10)
    [ -z "$SUMMARY" ] && SUMMARY=$(echo "$OUTPUT" | tail -4)
    PAYLOAD=$(python3 -c "
import json, sys
msg = sys.stdin.read().strip()[:1800]
print(json.dumps({'content': f'**[auto-improve]**\n{msg}'}))
" <<< "$SUMMARY")
    curl -s -X POST "$DISCORD_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" > /dev/null || true
fi
