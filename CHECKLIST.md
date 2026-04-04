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
- [ ] **Binance trade-count conviction filter** — only trade when `trades_per_sec > 2×` asset's rolling average; low trade count = one big order, no follow-through
- [x] **Cross-window correlation cooldown** — `_asset_last_bet` map; correlated assets blocked for 300s after a bet
- [x] **Latency measurement** — real order placement time logged; `⚠ SLOW` warning if > 3s signal→order
- [ ] **Binance WebSocket coverage** — verify the WS feed is running and delivering ticks for ALL 7 assets (BTC/ETH/SOL/XRP/DOGE/BNB/HYPE); fall back to REST per-asset if WS missing any
- [ ] **Multi-window lookahead** — when multiple windows are open, prefer markets with 3+ minutes remaining over ones expiring in < 60s
- [ ] **Profit reinvestment logic** — if balance grows above starting capital, allow `max_position` to scale up proportionally (compound gains)
- [ ] **Polymarket 2% fee in Kelly** — current Kelly ignores the 2% fee on winnings; true breakeven accuracy is ~53%, not 50%; adjust formula
- [ ] **Per-asset Kelly from session accuracy** — `Kelly = 2 * signal_accuracy - 1`; use session log data once 30+ windows per asset accumulated
- [x] **Max daily loss limit** — already in `risk.py`; `-3%` live, `-20%` dry-run; resets daily
- [x] **`rel_strength` in trade log** — added to `TradeRecord` dataclass; passed from `_execute_signal()` via `record_open()`

---

## P2 — High Impact, Larger Build

- [ ] **Pre-window entry** — watch Binance in the 30s BEFORE a window opens; fire immediately at T=0 when window opens at YES=0.50 (maximum oracle lag before MMs wake up)
- [ ] **Async parallel asset scanning** — currently scans BTC→ETH→SOL sequentially; by asset 7 the signal is 3–5s stale; scan all 7 assets concurrently with `asyncio`
- [ ] **Market regime detection** — detect trending vs choppy/sideways market; skip signals in ranging regimes where momentum accuracy drops to ~50%
- [ ] **Order book thinness filter** — thin spread + low depth = MMs absent = maximum edge; thick book = already repriced; check `best_ask - best_bid` and total depth
- [ ] **Early exit / loss cut** — if position hits `0.25` mid-window, exit early and redeploy capital; if at `0.72+` with 90s left, lock in the gain
- [ ] **Consecutive window persistence** — from session log: if BTC closed UP 4 of last 5 windows, weight trend-follow signal higher; measure empirically per-asset
- [ ] **Connection pooling** — new HTTP connection per Polymarket REST call adds 200–500ms; use `requests.Session` with keep-alive
- [ ] **Intra-window timing calibration** — from session data: learn which seconds into the window (30s? 60s? 120s?) have highest signal accuracy per asset
- [ ] **BTC leadership lag calibration** — measure empirically (from session log) how many seconds SOL/ETH/DOGE follow BTC; currently hardcoded 15–45s assumption

---

## P3 — Medium Impact

- [ ] **15-minute window markets** — Polymarket has 15-min UpDown windows; longer window = more time before MMs reprice; add support alongside 5-min
- [ ] **Binance funding rate signal** — perpetual funding rate sign = market sentiment bias; negative = bearish, positive = bullish; free directional signal
- [ ] **Binance open interest** — rising OI + price move = conviction; falling OI = exhaustion/short covering; available from Binance futures API
- [ ] **Skip stale Polymarket markets** — if a market hasn't had a trade in the last 2 minutes, MMs are absent AND the price is stale; skip
- [ ] **Liquidity depth filter** — require minimum `$50` of depth at the top of the order book (not just any liquidity); thin books = unreliable fills
- [ ] **Limit orders instead of market orders** — post limit at `0.51` instead of taking the `0.52` ask; save the spread on every trade
- [ ] **Session log dashboard panel** — web UI panel showing per-asset signal accuracy, window counts, avg move %; live from session_log.jsonl
- [ ] **Per-asset P&L breakdown** — dashboard currently shows total P&L only; break down by asset
- [ ] **Analyst history viewer** — web UI panel showing what the LLM decided each run, with its reasoning and params applied
- [ ] **Mean reversion strategy** — when `window_return > 0.3%` (extreme), probability of mean reversion increases; consider betting the opposite direction
- [ ] **Cross-window arbitrage** — if BTC just moved +0.2%, the NEXT window (opening in 4 min) will also open at 0.50 mispriced; pre-position
- [ ] **Adverse selection detector** — if trades that fill instantly lose more than slow-fill trades, a smarter counterparty is on the other side; track fill latency vs outcome

---

## P4 — Infrastructure & Future

- [ ] **Polymarket WebSocket feed** — replace REST polling for market prices with WebSocket for faster price updates and order book streaming
- [ ] **Clock sync check** — verify server clock is within 500ms of NTP; if off, window boundary detection is wrong
- [ ] **Polygon gas spike handling** — during network congestion orders can fail silently; add retry logic and gas price monitoring
- [ ] **Wallet USDC replenishment alert** — Discord notification when balance drops below `$25`; currently only alerts on errors
- [ ] **Competing bot detection** — fast repricing (< 5s after window open) = other bots present = edge is gone; skip the market
- [ ] **Order cancellation / spoofing detection** — large orders that appear then disappear make signals unreliable; detect and skip
- [ ] **ML signal classifier** — train logistic regression on session log features (time-of-day, window_return at Ns, trade_count, regime) to output win probability
- [ ] **Feature importance analysis** — determine which signals (window_return, acceleration, multitf, btc_lead) actually predict outcomes vs which are noise
- [ ] **Backtest engine** — replay historical Binance prices + historical Polymarket odds to test strategies offline before deploying
- [ ] **A/B testing framework** — safely test new signals on 50% of trades while keeping old logic on other 50%
- [ ] **Twitter/social sentiment** — real-time crypto sentiment spikes often precede price moves 30–90s; integrate sentiment feed
- [ ] **Exchange net inflows/outflows** — coins moving TO exchanges = selling pressure; moving OUT = accumulation
- [ ] **Cross-exchange price divergence** — BTC $100 higher on Coinbase than Binance → arbitrage pressure pushes Binance up; predictable directional signal
- [ ] **Fear & Greed index integration** — broad sentiment; momentum signals more reliable when aligned with sentiment
- [ ] **Alpha decay detection** — track whether edge is shrinking over time (more bots competing); pause and recalibrate if edge drops below breakeven
- [ ] **Hardcoded values centralization** — magic numbers scattered across 10+ files; consolidate all tunable params into `config.py`
- [ ] **Geographic latency audit** — measure round-trip to Binance and Polymarket APIs; if > 100ms consider server relocation
- [ ] **Historical Polymarket outcome database** — store all market outcomes (not just traded ones) for offline signal validation
- [ ] **Risk of ruin calculator** — given current win rate, Kelly fraction, and balance, compute probability of ruin; surface in dashboard
- [ ] **Wallet USDC replenishment workflow** — document and automate the process of bridging USDC to Polygon when balance is low
- [ ] **Multi-asset concurrent exposure cap** — even across different windows, total exposure across correlated assets (BTC+ETH+BNB) should have a group cap
- [ ] **`prefer_assets` signal boost** — when analyst sets `prefer_assets: ["SOL"]`, currently just logs it; should actively boost SOL's `rel_strength` score so it wins the ranking more often

---

## Completed

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

*Last updated: 2026-04-04 (session 2)*
