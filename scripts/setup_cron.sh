#!/usr/bin/env bash
# setup_cron.sh — Install the auto_improve cron job
# Run once on the server: bash scripts/setup_cron.sh

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/auto_improve.sh"

chmod +x "$SCRIPT"

# Add cron job: runs every 4 hours
# Adjust the schedule as needed:
#   */4 = every 4 hours  |  0 * = every hour  |  0 */2 = every 2 hours
CRON_LINE="0 * * * * $SCRIPT >> $REPO_DIR/logs/cron.log 2>&1"

# Check if already installed
if crontab -l 2>/dev/null | grep -q "auto_improve.sh"; then
    echo "Cron job already installed:"
    crontab -l | grep auto_improve
else
    # Add to crontab
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "Cron job installed: $CRON_LINE"
fi

echo ""
echo "Current crontab:"
crontab -l
