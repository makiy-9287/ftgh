#!/usr/bin/env bash
# Install dependencies and start the bot.
#   ./setup.sh            install + run
#   ./setup.sh --no-run   install only
set -e

cd "$(dirname "$0")"

echo "[1/4] system packages"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3 python3-venv python3-dev build-essential
fi

echo "[2/4] virtualenv"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --upgrade -q pip wheel

echo "[3/4] python packages (this takes a few minutes)"
./.venv/bin/pip install -q -r requirements.txt

echo "[4/4] config"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo
  echo "Created .env — add your Telegram token and chat ID:"
  echo "    nano .env"
  echo "Then run:  ./setup.sh"
  exit 0
fi

if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' .env; then
  echo "TELEGRAM_BOT_TOKEN is empty in .env — fill it in first."
  exit 1
fi

if [ "$1" = "--no-run" ]; then
  echo "Done. Start with:  ./.venv/bin/python main.py"
  exit 0
fi

echo
echo "Starting. Ctrl+C to stop."
exec ./.venv/bin/python main.py
