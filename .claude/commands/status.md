# /status — Bot Status Report

Generate a comprehensive status report for the Midusbot trading bot.

## Instructions

Run the following checks and summarize findings:

1. **Syntax check all source files**:
   ```bash
   for f in src/*.py; do python3 -m py_compile "$f" 2>&1 && echo "OK: $f" || echo "FAIL: $f"; done
   ```

2. **Current params** — read `data/analyst_params.json` and `data/params_main.json`

3. **Recent performance** — read last 20 lines of `data/journal.jsonl` and compute:
   - Win rate (last 20 trades)
   - Total P&L
   - Per-asset breakdown

4. **Session accuracy** — read last 100 lines of `data/session_log.jsonl`, compute per-asset signal accuracy

5. **Last analyst run** — read `data/analyst_history.json`, show the most recent entry's timestamp and recommendations

6. **Log warnings** — run:
   ```bash
   tail -100 logs/bot_$(date +%Y-%m-%d).log | grep -E "WARNING|ERROR|SLOW|BOT-DETECT|ADVERSE" | tail -20
   ```

Present findings as a concise table with: Asset | Threshold | Sessions | Accuracy | Win Rate | P&L
Flag any assets with accuracy < 52% or win rate < 50% as needing attention.
