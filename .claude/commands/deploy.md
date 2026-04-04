# /deploy — Deploy & Restart Bot

Syntax-check all files, commit staged changes, and provide restart instructions.

## Instructions

1. **Syntax check everything**:
   ```bash
   for f in src/*.py config.py main.py; do
     python3 -m py_compile "$f" && echo "OK: $f" || echo "FAIL: $f"
   done
   ```
   Stop and report any failures before continuing.

2. **Show git diff** of staged + unstaged changes:
   ```bash
   git diff HEAD
   ```

3. **Commit with descriptive message** (only if changes are staged):
   ```bash
   git add -p  # review interactively, or specify files
   git commit -m "your message"
   ```

4. **Push to current branch**:
   ```bash
   git push -u origin $(git branch --show-current)
   ```

5. **Restart instructions** — output to user:
   ```
   To restart the bot:
     docker-compose down && docker-compose up -d   # if using Docker
     # or
     pkill -f "python main.py" && python main.py --no-dashboard &
   
   Monitor with:
     tail -f logs/bot_$(date +%Y-%m-%d).log
   ```

**NOTE**: Never start the bot automatically. Provide the command for the user to run manually.
