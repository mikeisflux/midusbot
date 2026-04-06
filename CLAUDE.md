# Midusbot — Claude Code Project Memory

## What This Bot Does

Midusbot is a Polymarket 5-minute UpDown trading bot that exploits **oracle lag**: Binance crypto prices update 60-120 seconds before Polymarket market makers reprice their YES/NO contracts. The edge is maximum at T=0 when a new window opens at 0.50 and Binance has already moved.

**Core strategy**: When Binance shows BTC +0.08% in the last 5 minutes, the "Will BTC be higher in 5 min?" YES token is still 0.50. That's a mispriced bet. Enter YES at 0.50-0.51 before MMs catch up.

## Architecture

```
main.py              — entry point, argparse, logging setup
config.py            — ALL tunable constants (env var overrides)
src/
  bot.py             — PolymarketBot: main loop, startup checks
  scanner.py         — ScannerMixin: parallel asset scanning, window detection
  positions.py       — PositionsMixin: order placement, position management
  strategy.py        — signal evaluation, entry guards, multi-confirmation scoring
  signals.py         — Binance price feeds, regime detection, OI/funding/cross-exchange
  feeds.py           — Binance WebSocket feed manager
  session_tracker.py — logs every 5-min window outcome (UP/DOWN) for calibration
  learner.py         — per-asset threshold adaptation, analyst trigger, alpha decay
  analyst.py         — LLM analyst (Ollama qwen2.5-coder) for param recommendations
  risk.py            — Kelly sizing, group exposure cap, risk of ruin
  fair_value.py      — fair value estimation
  client.py          — Polymarket CLOB client with retry/backoff
  sim.py             — paper trading simulation
  backtest.py        — offline backtest engine against session_log
  ml_classifier.py   — logistic regression on session features
  ab_test.py         — A/B test framework with z-test p-values
  webui.py           — Flask web dashboard (port 5000)
  webui_api.py       — API endpoints
  webui_html.py      — HTML templates
  dashboard.py       — terminal rich dashboard
  discord_bot.py     — Discord alert integration
  trend.py           — multi-timeframe trend detection
  utils.py           — shared utilities
data/
  session_log.jsonl         — every 5-min window outcome
  analyst_params.json       — LLM-recommended per-asset params
  params_main.json          — learner-adapted thresholds
  journal.jsonl             — closed trade records
  adverse_selection.jsonl   — fast-fill outcome tracking
  polymarket_outcomes.jsonl — all closed position outcomes
  analyst_history.json      — last 20 LLM runs
  ml_classifier.json        — trained classifier weights
logs/                — daily rotating log files
```

## Key Trading Rules (DO NOT CHANGE without understanding impact)

- **Entry guard**: Skip if YES mid > `ENTRY_PRICE_GUARD` (0.54) — MMs already repriced
- **Capital floor**: Hard stop if wallet < `CAPITAL_FLOOR_USDC` ($15)
- **Position sizing**: `min(wallet × 12%, MAX_POSITION_USDC)` — bankroll-proportional
- **Kelly fee**: 2% Polymarket win fee baked into Kelly formula (`POLY_WIN_FEE = 0.02`)
- **Cooldown**: No correlated assets within 300s of each other (`COOLDOWN_SECS`)
- **Warmup**: 90s cold-start block on restart (price history must populate)
- **Regime filter**: Skip signals in CHOPPY regime (sideways market = 50% noise)
- **OB filter**: Skip if YES spread < 0.005 (aggressive MMs = edge is gone)
- **Too-late cutoff**: Skip if > 240s into 5-min window (only 60s left, not worth it)
- **Early exit**: Cut loss if YES < 0.20; lock gain if YES > 0.78
- **Daily loss limit**: -3% live, -20% dry-run (in `risk.py`)

## Assets Tracked

`BTC`, `ETH`, `SOL`, `DOGE`, `WIF`, `HYPE`, `TRUMP` (configured in `src/bot.py`)

Default thresholds (in `analyst_params.json`):
- BTC: 0.035% | ETH: 0.040% | SOL: 0.060% | DOGE: 0.100% | WIF: 0.120%

## Build & Run Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run in dry-run mode (default, no real orders)
python main.py

# Run without rich dashboard
python main.py --no-dashboard

# Run with live trading (requires .env with PRIVATE_KEY etc.)
DRY_RUN=false python main.py

# Syntax check a file
python -m py_compile src/strategy.py

# Check all source files
for f in src/*.py; do python -m py_compile $f && echo "OK: $f"; done

# Run tests
python -m pytest test/

# Web dashboard (started automatically)
# http://localhost:5000

# Key API endpoints:
# GET /api/session_stats      — per-asset signal accuracy
# GET /api/asset_pnl          — P&L breakdown by asset
# GET /api/analyst_history    — last 20 LLM runs
# GET /api/timing_accuracy    — win rate by seconds-into-window
# GET /api/alpha_decay        — win-rate trend + risk of ruin
# GET /api/feature_importance — signal correlation with outcomes
# POST /positions             — manual position management
```

## Environment Variables (`.env`)

```bash
PRIVATE_KEY=           # Polymarket private key (from Settings → Private Key)
CLOB_API_KEY=          # Polymarket CLOB API credentials
CLOB_API_SECRET=
CLOB_API_PASSPHRASE=
FUNDER_ADDRESS=        # Your Polymarket wallet address
SIGNATURE_TYPE=1       # 1 = POLY_PROXY (standard Polymarket export)
DRY_RUN=true           # Set false for live trading
TRADING_PAUSED=false   # Pause new buys without stopping the bot
CAPITAL_FLOOR_USDC=15  # Hard stop threshold
MAX_POSITION_USDC=10   # Max per-trade size
DISCORD_WEBHOOK_URL=   # Optional: Discord alerts
TELEGRAM_BOT_TOKEN=    # Optional: Telegram alerts
```

## Data Files — DO NOT DELETE

- `data/session_log.jsonl` — source of truth for backtesting and calibration
- `data/analyst_params.json` — LLM recommendations; edit to tune the bot manually
- `data/journal.jsonl` — all trade history; used by learner + alpha decay

## Common Debugging

```bash
# Watch live logs
tail -f logs/bot_$(date +%Y-%m-%d).log

# Check last analyst run
cat data/analyst_history.json | python -m json.tool | tail -50

# Check current params
cat data/analyst_params.json

# Check learner params
cat data/params_main.json

# Manually trigger backtest
python -c "from src.backtest import BacktestEngine; r = BacktestEngine().run(); print(r)"

# Check alpha decay
python -c "from src.learner import Learner; l = Learner(); print(l.alpha_decay_report())"

# Check feature importance
python -c "from src.learner import Learner; l = Learner(); print(l.feature_importance())"
```

## Safety Reminders for AI Assistance

1. **Never modify `ENTRY_PRICE_GUARD` above 0.54** — higher values let the bot trade when edge is gone
2. **Never remove the capital floor check** — trading to zero is possible without it
3. **Never change Kelly fraction above 0.25** — higher = ruin risk
4. **Check `config.py` first** before adding any magic numbers to strategy files
5. **Run `py_compile` on every modified file** before committing
6. **DRY_RUN=true is the default** — live trading requires explicit `DRY_RUN=false` in .env
7. **The `analyst_params.json` file overrides code defaults** — check it if behavior seems wrong
8. **`session_log.jsonl` is precious** — it's the training data for the classifier and backtest engine
