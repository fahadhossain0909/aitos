#!/bin/bash
# Send hourly Telegram status update
AITOS_DIR="${AITOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$AITOS_DIR"
source "$AITOS_DIR/.venv/bin/activate"
python3 -c "
from aitos.telegram.status_bot import send_hourly_status
import os
token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
if token and chat_id:
    send_hourly_status(token, chat_id)
    print('Telegram update sent')
else:
    print('TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set')
"
