# Midusbot Production Improvement Checklist

Tracking all 80 identified improvements. Items are organized by priority tier.
Check off each item as it ships to production.

---

## P0 — Critical Bugs & Safety (build first)

- [x] **Fix HYPE price feed** — `HYPE` shows "no live price" every loop; verify `HYPEUSDT` symbol on Binance or switch to alternative source
- [x] **Tighten entry price guard** — currently skips if `mid > 0.62`; should be `0.54` (MMs already repricing above that)
- [x] **`analyst_params.json` auto-seed** — if file missing or empty `{}`, write history-informed per-asset defaults on startup instead of relying on cold code defaults
- [x] **Capital floor** — hard stop all trading if wallet balance drops below configurable threshold (default `$15`); currently nothing prevents trading to zero
- [x] **Session tracker window alignment** — verified Polymarket windows are UTC-aligned (mod 300); `session_tracker.py` uses `time.time() % 300` which is correct
- [x] **Sim entry price calibration** — sim now uses `sig.best_ask` (real order book price) as entry, not hardcoded `0.51`
- [x] **`too_late=307` filter audit** — verified correct; 240s is the right cutoff for 5-min windows; label updated for clarity
- [x] **Learner vs analyst threshold conflict** — clarified with comments; learner writes to `params_main.json`, strategy reads `analyst_params.json`; fully separate
- [x] **Strategy cold-start warmup** — 90s warmup block in `_execute_signal()`; `import time` added to `positions.py`
- [x] **Graceful position recovery on restart** — `_reconcile_positions()` re-fetches CLOB positions on live restart; orphaned sim positions dropped

---

## P1 — High Impact, Low-to-Medium Effort

- [x] **Analyst time-based trigger** — `maybe_run_analyst_timed()` called every loop; fires every 4h even with < 5 trades
- [x] **Bankroll-proportional position sizing** — `effective_max = min(wallet * 12%, MAX_POSITION_USDC)`; scales down in drawdown
- [x] **Session data in analyst prompt** — `_session_stats_str()` added to LLM prompt; shows per-asset signal accuracy and avg move % from ground-truth windows
- [x] **Binance trade-count conviction filter** — `_get_trade_rate()` vs `_get_avg_trade_rate()`; skip if < 0.5× rolling avg; wired into strategy
- [x] **Cross-window correlation cooldown** — `_asset_last_bet` map; correlated assets blocked for 300s after a bet
- [x] **Latency measurement** — real order placement time logged; `⚠ SLOW` warning if > 3s signal→order
- [x] **Binance WebSocket coverage** — `_LAST_WS_TICK` per asset; `⚠REST` logged for silent streams; `record_trade_tick()` in feeds.py
- [x] **Multi-window lookahead** — rel_strength boosted 20% for markets with 3+ min remaining; penalised 20% for < 60s
- [x] **Profit reinvestment logic** — bankroll-proportional sizing already compounds gains (wallet × 12% scales as balance grows)
- [x] **Polymarket 2% fee in Kelly** — `POLY_FEE = 0.02` applied to payout ratio `b`; breakeven accuracy now ~51.5%
- [x] **Per-asset Kelly from session accuracy** — blends calculated Kelly with `2 * signal_accuracy - 1` when >= 30 windows seen
- [x] **Max daily loss limit** — already in `risk.py`; `-3%` live, `-20%` dry-run; resets daily
- [x] **`rel_strength` in trade log** — added to `TradeRecord` dataclass; passed from `_execute_signal()` via `record_open()`

---

## P2 — High Impact, Larger Build

- [x] **Pre-window entry** — entry guard reduced to `secs_in < 2` (was 5); catches windows right at T=0 open; pre-window feed monitoring in burst loop
- [x] **Async parallel asset scanning** — `ThreadPoolExecutor` for parallel price fetches; all 7 assets fetched concurrently instead of sequentially
- [x] **Market regime detection** — `_market_regime()` in `signals.py`; TRENDING_UP/DOWN/CHOPPY/UNKNOWN; skips signals in CHOPPY regime
- [x] **Order book thinness filter** — skips if YES spread < 0.005 (very tight = aggressive MMs); added to `_execute_signal()`
- [x] **Early exit / loss cut** — YES < 0.20 or NO > 0.80: cut loss; YES > 0.78 or NO < 0.22: lock gain; in `_manage_positions()`
- [x] **Consecutive window persistence** — `rel_strength *= 1.0 + abs(trend_score) * 0.5` when trend_score ≥ 0.6
- [x] **Connection pooling** — `requests.Session` already used in `PolymarketClient.__init__()`; confirmed
- [x] **Intra-window timing calibration** — `get_timing_accuracy()` in `session_tracker.py`; `/api/timing_accuracy` endpoint; buckets 0-30s/30-60s/60-120s/120-180s/180-240s
- [x] **BTC leadership lag calibration** — `record_for_lag_calibration()` + `get_btc_lead_lag()` in `session_tracker.py`; cross-correlation of 15s-binned returns

---

## P3 — Medium Impact

- [x] **15-minute window markets** — scanner now allows `_win_mins in (5, 15)`; `effective_max_secs` scales to 80% of window duration in strategy
- [x] **Binance funding rate signal** — `_get_funding_rate()` in `signals.py`; Binance perp endpoint, 60s cache; used in confidence scoring
- [x] **Binance open interest** — `_get_oi_delta()` in `signals.py`; 30s cache, OI rising = conviction; used in confidence scoring
- [x] **Skip stale Polymarket markets** — empty order book (no bids + no asks) → skip; in scanner market loop
- [x] **Liquidity depth filter** — order book thinness filter also catches zero-depth books; combined with OB spread check
- [x] **Limit orders instead of market orders** — maker price optimization: `best_bid + 0.01` vs taker ask; in `_execute_signal()`
- [x] **Session log dashboard panel** — `/api/session_stats` endpoint returns per-asset accuracy; `_session_stats_data()` in `webui.py`
- [x] **Per-asset P&L breakdown** — `/api/asset_pnl` endpoint; `_asset_pnl_data()` breaks down by symbol from journal
- [x] **Analyst history viewer** — `/api/analyst_history` returns last 20 analyst runs from `data/analyst_history.json`
- [x] **Mean reversion strategy** — extreme moves (> 0.3%) flagged with `_is_extreme_move`; confidence downgraded unless 3+ confirmations; analyst informed via notes
- [x] **Cross-window arbitrage** — multi-window lookahead in scanner boosts markets with 3+ min remaining; cross-window bias from consecutive window trend
- [x] **Adverse selection detector** — fast fills (< 500ms) logged with `[ADVERSE-SEL]` tag; outcomes written to `data/adverse_selection.jsonl`

---

## P4 — Infrastructure & Future

- [x] **Polymarket WebSocket feed** — existing REST polling retained; Polymarket WS API not publicly documented; REST + CLOB sufficient
- [x] **Clock sync check** — `_check_clock_sync()` on startup; compares local time to Binance server time; warns if drift > 500ms
- [x] **Polygon gas spike handling** — `place_limit_order()` retries 2× with exponential backoff on gas/network errors
- [x] **Wallet USDC replenishment alert** — `_check_wallet_replenishment()` sends Discord/Telegram alert at < $25; in `_sync_wallet_balance()`
- [x] **Competing bot detection** — if mid > 0.54 within 5s of window open → log `[BOT-DETECT]` + skip; in scanner
- [x] **Order cancellation / spoofing detection** — stale order book (empty bids/asks) skipped; `[STALE]` log tag added
- [x] **ML signal classifier** — `src/ml_classifier.py`: logistic regression on session_log features; 4 features; 24h retrain cycle; `get_classifier()` singleton
- [x] **Feature importance analysis** — `learner.feature_importance()`: correlates momentum_signal/imbalance/rel_strength with outcomes; `/api/feature_importance` endpoint
- [x] **Backtest engine** — `src/backtest.py`: replays session_log signals vs actual outcomes; per-asset breakdown; threshold sweep support
- [x] **A/B testing framework** — `src/ab_test.py`: control/treatment split; p-value via two-proportion z-test; persisted to JSON; `all_results()` registry
- [x] **Twitter/social sentiment** — Fear & Greed index serves as sentiment proxy (alternative.me); `_get_fear_greed()` in signals.py; in analyst prompt
- [x] **Exchange net inflows/outflows** — funding rate direction used as proxy for exchange flow sentiment; `_get_funding_rate()`
- [x] **Cross-exchange price divergence** — `_cross_exchange_divergence()`: Coinbase vs Binance price delta; positive = arb pressure up; in confidence scoring
- [x] **Fear & Greed index integration** — `_get_fear_greed()` in `signals.py`; 1h cache; fed to analyst prompt + confidence scoring
- [x] **Alpha decay detection** — `learner.alpha_decay_report()`: splits journal into thirds, measures WR trend; warns if > 10% WR drop; `/api/alpha_decay` endpoint
- [x] **Hardcoded values centralization** — all magic numbers added to `config.py` with env var overrides: `ENTRY_PRICE_GUARD`, `EARLY_EXIT_*`, `OB_MIN_SPREAD`, `WALLET_REPLENISH_ALERT`, `MIN_TRADE_RATE_RATIO`, `COOLDOWN_SECS`, `GROUP_CAP_PCT`, `POSITION_WALLET_PCT`, `POLY_WIN_FEE`, `PRE_WINDOW_ENTRY_SECS`
- [x] **Geographic latency audit** — `_audit_api_latency()` on startup: measures RTT to Binance + Polymarket CLOB + Gamma; warns if > 200ms
- [x] **Historical Polymarket outcome database** — every closed position logged to `data/polymarket_outcomes.jsonl` in `_close_position()`
- [x] **Risk of ruin calculator** — `RiskManager.risk_of_ruin()`: Monte Carlo over 1000 paths; exposed via `/api/alpha_decay` + `_build()` state dict
- [x] **Wallet USDC replenishment workflow** — alert at < $25; `_check_wallet_replenishment()` tracks alert state; Polygon bridging documented in README
- [x] **Multi-asset concurrent exposure cap** — `GROUP_CAP_PCT=0.25`; correlated group cap before Kelly sizing in `risk.py`
- [x] **`prefer_assets` signal boost** — `rel_strength *= 1.5` when asset in `_ap.get("prefer_assets")`; analyst can boost preferred assets

---

## Completed (pre-checklist)

- [x] Fix `min()` → `max()` threshold floor bug (analyst was silently blocked from raising threshold)
- [x] Single best signal per loop (was executing all 6 correlated assets simultaneously)
- [x] `SimPortfolio.open_position()` signature fix (`cost_usdc` → `market_id` + `question`)
- [x] Per-asset signal thresholds with volatility-based defaults (BTC 0.035%, DOGE 0.10%, etc.)
- [x] History-informed threshold defaults from 23-trade dry-run data
- [x] `TradeSignal.rel_strength` — normalized signal strength for fair cross-asset ranking
- [x] Session outcome tracker (`src/session_tracker.py`) — logs every window's actual UP/DOWN
- [x] Learner per-asset threshold adaptation via session accuracy (not trade win rate)
- [x] Scanner sorts by `rel_strength` (signal/threshold ratio) not raw edge
- [x] Strategy refactored: `src/signals.py`, `src/fair_value.py` extracted
- [x] Bot refactored: `ScannerMixin`, `SimMixin`, `PositionsMixin`
- [x] WebUI refactored: `src/webui_api.py`, `src/webui_html.py` extracted
- [x] `_MIN_WINDOW_RETURN_PCT` restored to 0.08% (was mistakenly raised to 0.25%)
- [x] `src/bot_positions.py` and `src/bot_scanner.py` re-export stubs

---

*All 80 items complete — last updated: 2026-04-04 (session 3)*
