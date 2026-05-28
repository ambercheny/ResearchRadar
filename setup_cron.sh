#!/bin/bash
# Installs (or updates) the cron job for the daily paper digest.
# Edit the schedule variables below, then re-run: bash setup_cron.sh

# ── Schedule ──────────────────────────────────────────────────────────────────
HOUR=7          # 0-23
MINUTE=11       # 0-59
DAYS="1-5"      # 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat, 0=Sun
                # Examples: "1-5" = Mon-Fri | "1,3,5" = Mon/Wed/Fri | "*" = every day
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
LOG="$SCRIPT_DIR/digest.log"
CRON_LINE="$MINUTE $HOUR * * $DAYS cd \"$SCRIPT_DIR\" && \"$VENV_PYTHON\" digest.py >> \"$LOG\" 2>&1"

# Verify venv exists
if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: venv not found. Run first:"
    echo "  python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    exit 1
fi

# Remove any existing digest.py cron entry, then add the updated one
(crontab -l 2>/dev/null | grep -v "digest.py"; echo "$CRON_LINE") | crontab -
echo "Cron job set: runs on days=$DAYS at ${HOUR}:$(printf '%02d' $MINUTE)"
echo ""
echo "Python: $VENV_PYTHON"
echo "Log:    $LOG"
echo ""
echo "To verify:   crontab -l"
echo "To view log: tail -f $LOG"
echo ""
echo "NOTE: Cron uses your system timezone."
echo "Check with: sudo systemsetup -gettimezone"
