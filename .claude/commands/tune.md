# /tune — Tune Bot Parameters

Analyze recent session data and recommend parameter adjustments.

## Instructions

1. **Read current state**:
   - `data/analyst_params.json` — current thresholds and settings
   - `data/session_log.jsonl` — last 200 session windows
   - `data/journal.jsonl` — last 50 trades

2. **Compute per-asset signal accuracy** from session log:
   ```python
   # For each asset: accuracy = correct_predictions / total_windows
   # correct = predicted direction matches actual direction
   ```

3. **Run alpha decay check**:
   ```bash
   python3 -c "from src.learner import Learner; l = Learner(); print(l.alpha_decay_report())"
   ```

4. **Run feature importance**:
   ```bash
   python3 -c "from src.learner import Learner; l = Learner(); print(l.feature_importance())"
   ```

5. **Analyze and recommend**:
   - For assets with accuracy > 58%: consider lowering threshold by 10-15% to catch more trades
   - For assets with accuracy < 52%: raise threshold by 15-20% to filter noise
   - If alpha decay shows >10% WR drop: flag — edge may be shrinking
   - Show True Kelly for each asset: `Kelly = 2 * accuracy - 1`
   - Compare True Kelly to current `KELLY_FRACTION` in config.py

6. **Output recommended `data/analyst_params.json`** as a JSON block the user can review and apply.

**IMPORTANT**: Do not write to `data/analyst_params.json` automatically. Present recommendations for user review first.
