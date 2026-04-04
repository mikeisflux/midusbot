# /debug — Debug Bot Issues

Investigate recent errors and anomalies in bot behavior.

## Instructions

1. **Recent errors**:
   ```bash
   tail -200 logs/bot_$(date +%Y-%m-%d).log | grep -E "ERROR|Exception|Traceback" | tail -30
   ```

2. **Warning summary**:
   ```bash
   tail -500 logs/bot_$(date +%Y-%m-%d).log | grep -E "WARNING|SLOW|BOT-DETECT|STALE|ADVERSE|WS-SILENT" | tail -30
   ```

3. **Trade execution issues**:
   ```bash
   tail -500 logs/bot_$(date +%Y-%m-%d).log | grep -E "SLOW|⚠|latency|timeout|retry" | tail -20
   ```

4. **Capital/safety events**:
   ```bash
   tail -500 logs/bot_$(date +%Y-%m-%d).log | grep -E "capital floor|PAUSED|WARMUP|daily loss|replenish" | tail -20
   ```

5. **Missing price feeds**:
   ```bash
   tail -500 logs/bot_$(date +%Y-%m-%d).log | grep -E "no live price|REST fallback|WS-SILENT" | tail -20
   ```

6. **Analyze findings**:
   - Identify the most frequent error type
   - Check if errors correlate with a specific asset or time window
   - Read the relevant source file if an exception is thrown
   - Propose a targeted fix

Present: Error type | Count | Last occurrence | Likely cause | Recommended fix
