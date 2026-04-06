# /restart — Restart Bot via PM2

Restart the running bot process immediately without deploying code changes.

## Instructions

1. **Check PM2 process name**:
   ```bash
   pm2 list 2>/dev/null || echo "PM2 not available"
   ```

2. **Restart**:
   ```bash
   pm2 restart midusbot 2>/dev/null \
     || pm2 restart bot 2>/dev/null \
     || pm2 restart 0 2>/dev/null \
     || { echo "No matching PM2 process — starting fresh"; pm2 start main.py --name midusbot --interpreter python3; }
   sleep 2
   pm2 show midusbot 2>/dev/null || pm2 list
   ```

3. **Tail logs to confirm startup**:
   ```bash
   sleep 3
   tail -20 logs/bot_$(date +%Y-%m-%d).log 2>/dev/null
   ```

4. Report: which method was used, process status, any startup errors.
