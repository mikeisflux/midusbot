# /deploy — Deploy & Restart Bot

Syntax-check all files, commit, push, and restart the bot via PM2.

## Instructions

1. **Syntax check everything** — stop if any file fails:
   ```bash
   FAILED=0
   for f in src/*.py config.py main.py; do
     python3 -m py_compile "$f" && echo "OK: $f" || { echo "FAIL: $f"; FAILED=1; }
   done
   [ $FAILED -eq 1 ] && echo "SYNTAX ERRORS — aborting deploy" && exit 1
   echo "All files OK"
   ```

2. **Commit all modified tracked files**:
   ```bash
   git add -u
   git diff --cached --stat
   git commit -m "auto-deploy: <describe changes>"
   ```

3. **Push to current branch**:
   ```bash
   git push -u origin $(git branch --show-current)
   ```

4. **Restart via PM2** (try each method until one succeeds):
   ```bash
   # Try PM2 first (most likely on a server)
   if command -v pm2 &>/dev/null; then
     pm2 restart midusbot 2>/dev/null \
       || pm2 restart all 2>/dev/null \
       || pm2 start main.py --name midusbot --interpreter python3
     pm2 logs midusbot --lines 10 --nostream
   # Fall back to docker-compose
   elif command -v docker-compose &>/dev/null; then
     docker-compose down && docker-compose up -d
     docker-compose logs --tail=10
   # Fall back to pkill + relaunch
   else
     pkill -f "python.*main.py" || true
     nohup python3 main.py --no-dashboard >> logs/bot.log 2>&1 &
     echo "Bot relaunched as background process (PID $!)"
   fi
   ```

5. **Confirm it's running**:
   ```bash
   sleep 3
   if command -v pm2 &>/dev/null; then
     pm2 show midusbot | grep -E "status|pid|uptime"
   fi
   tail -20 logs/bot_$(date +%Y-%m-%d).log 2>/dev/null || echo "Log not yet written"
   ```

6. **Report back**: state which restart method was used, whether it succeeded, and the last few log lines.
