#!/bin/bash
# AITOS 24/7 Self-Running Launcher
AITOS_DIR="${AITOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export AITOS_HOME="$AITOS_DIR"
cd "$AITOS_DIR"
source "$AITOS_DIR/.venv/bin/activate"

# Start paper trading if not running
if ! pgrep -f "run_paper_trading.py" > /dev/null; then
    echo "Starting paper trading..."
    python3 run_paper_trading.py > /tmp/aitos-paper.log 2>&1 &
    echo $! > /tmp/aitos-paper.pid
    sleep 5
fi

# Start monitoring bot
echo "Starting AITOS Bot monitor..."
python3 native-env/aitos_bot.py &
echo $! > /tmp/aitos-bot.pid
echo "AITOS Bot launched (PID: $(cat /tmp/aitos-bot.pid))"
