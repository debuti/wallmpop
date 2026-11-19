#!/bin/sh
# Starts the bot detached outside Docker (venv), unless it's already running.
cd "$(dirname "$0")"
if [ -f data/bot.pid ] && kill -0 "$(cat data/bot.pid)" 2>/dev/null; then
  echo "ya corre (pid $(cat data/bot.pid))"; exit 0
fi
mkdir -p data
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -e .; }
setsid nohup .venv/bin/python -m wallmpop >> data/bot.log 2>&1 &
echo $! > data/bot.pid
echo "arrancado (pid $!)"
