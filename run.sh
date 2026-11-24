#!/bin/sh
# Starts the bot detached outside Docker (venv), unless it's already running.
cd "$(dirname "$0")"
# The pid must still be ours: after a reboot an old pid can belong to another process.
pid=$(cat data/bot.pid 2>/dev/null)
if [ -n "$pid" ] && { tr '\0' ' ' < /proc/"$pid"/cmdline; } 2>/dev/null | grep -q 'python -m wallmpop'; then
  echo "ya corre (pid $pid)"; exit 0
fi
mkdir -p data
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -e .; }
setsid nohup .venv/bin/python -m wallmpop >> data/bot.log 2>&1 &
echo $! > data/bot.pid
echo "arrancado (pid $!)"
