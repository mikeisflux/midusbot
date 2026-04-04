# /backtest — Run Backtest Analysis

Run the backtest engine against historical session data.

## Instructions

1. **Run backtest**:
   ```bash
   python3 -c "
   from src.backtest import BacktestEngine
   engine = BacktestEngine()
   results = engine.run(lookback_days=7)
   print(f'Total trades: {results.total_trades}')
   print(f'Win rate: {results.win_rate:.1%}')
   print(f'Total P&L: \${results.total_pnl:.2f}')
   print('Per-asset:')
   for asset, stats in results.per_asset.items():
       print(f'  {asset}: {stats}')
   "
   ```

2. **Run threshold sweep** to find optimal per-asset thresholds:
   ```bash
   python3 -c "
   from src.backtest import BacktestEngine
   engine = BacktestEngine()
   thresholds = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]
   results = engine.run_threshold_sweep(thresholds, lookback_days=14)
   for thresh, r in results.items():
       print(f'{thresh:.3f}: WR={r.win_rate:.1%} trades={r.total_trades} pnl=\${r.total_pnl:.2f}')
   "
   ```

3. **Interpret results**:
   - Find threshold with best risk-adjusted return (WR * sqrt(N_trades))
   - Note minimum trade count for statistical significance (>= 30 trades)
   - Flag any threshold that improves WR without reducing trade count too much

4. **Output**: Present a table of threshold vs (win_rate, trade_count, pnl) and highlight the recommended setting.
