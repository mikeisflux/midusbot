# /risk — Risk Analysis

Compute risk metrics for current bot configuration.

## Instructions

1. **Read current wallet state** (from logs or journal):
   ```bash
   tail -50 logs/bot_$(date +%Y-%m-%d).log | grep -E "wallet|balance|USDC" | tail -10
   ```

2. **Compute risk of ruin** via Monte Carlo:
   ```bash
   python3 -c "
   from src.risk import RiskManager
   import config
   rm = RiskManager()
   # Get win rate from journal
   from pathlib import Path
   import json
   trades = [json.loads(l) for l in Path('data/journal.jsonl').read_text().splitlines()[-100:] if l.strip()]
   closed = [t for t in trades if t.get('outcome') in ('win','loss')]
   if closed:
       wr = sum(1 for t in closed if t['outcome']=='win') / len(closed)
       ror = rm.risk_of_ruin(wr, config.KELLY_FRACTION)
       print(f'Win rate (last {len(closed)} trades): {wr:.1%}')
       print(f'Kelly fraction: {config.KELLY_FRACTION}')
       print(f'Risk of ruin: {ror:.1%}')
   else:
       print('No closed trades found')
   "
   ```

3. **Check daily loss limit status**:
   ```bash
   python3 -c "
   from src.risk import RiskManager
   rm = RiskManager()
   print('Daily loss tracker:', rm._daily_pnl if hasattr(rm, '_daily_pnl') else 'not available')
   "
   ```

4. **Report**:
   - Current win rate and Kelly-optimal bet size
   - Risk of ruin at current parameters
   - Whether daily loss limit is at risk
   - Recommendation: if risk of ruin > 5%, suggest reducing `KELLY_FRACTION` or `POSITION_WALLET_PCT`
