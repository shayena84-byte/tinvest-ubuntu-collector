#!/usr/bin/env bash
# Upgrade v1.4.0 SQLite market recorder to quality-aware v1.5.0 dashboard.
# READ ONLY MARKET DATA. Does not make any virtual/real brokerage trades.
set -euo pipefail

if [[ "$(id -u)" == "0" ]]; then
  echo "Запускайте как обычный пользователь Ubuntu, без sudo." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
MARKET="$HOME/.local/share/tinvest-market"
DB="$MARKET/market.sqlite3"
BACKUP="$MARKET/backups"
UNITS="$HOME/.config/systemd/user"

if [[ ! -f "$DB" ]]; then
  echo "Не найдена база v1.4.0: $DB. Сначала установите Market Recorder v1.4.0." >&2
  exit 1
fi
if [[ ! -s "$HOME/.config/tinvest-readonly/sandbox-token" || ! -s "$HOME/.config/tinvest-readonly/ca-bundle.pem" ]]; then
  echo "Отсутствуют локальные Sandbox credentials или TLS trust bundle." >&2
  exit 1
fi
for file in market_recorder.py market_dashboard.py dashboard.html dashboard.js; do
  if [[ ! -s "$ROOT/$file" ]]; then
    echo "В скачанном релизе отсутствует $file" >&2
    exit 1
  fi
done

cd "$ROOT"
python3 -m py_compile collector.py sandbox.py market_recorder.py market_dashboard.py
python3 -m unittest discover -s tests -v

mkdir -p "$DEST" "$MARKET" "$BACKUP" "$UNITS"
chmod 700 "$MARKET" "$BACKUP"

# Pause writes before safe SQLite backup and in-place migration.
systemctl --user disable --now tinvest-sandbox-market.timer 2>/dev/null || true
systemctl --user stop tinvest-sandbox-market.service 2>/dev/null || true
# Keep the two trading bots explicitly stopped, without touching their config.
systemctl --user disable --now tinvest-sandbox-autotrader.timer 2>/dev/null || true
systemctl --user disable --now tinvest-sandbox-ai.timer 2>/dev/null || true

# SQLite online backup API is safe even if WAL files are present.
python3 - "$DB" "$BACKUP" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone
source=Path(sys.argv[1])
backup_dir=Path(sys.argv[2])
dest=backup_dir/("market_before_quality_"+datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")+".sqlite3")
old=os.umask(0o077)
try:
    src=sqlite3.connect("file:"+str(source)+"?mode=ro",uri=True)
    dst=sqlite3.connect(str(dest))
    try:src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(dest,0o600)
finally:
    os.umask(old)
print("Резервная копия SQLite:",dest)
PY

for file in collector.py sandbox.py market_recorder.py market_dashboard.py dashboard.html dashboard.js; do
  install -m 700 "$ROOT/$file" "$DEST/$file"
done
chmod 700 "$DEST"
# Trigger migration now, with the old timer safely stopped.
python3 "$DEST/market_recorder.py" report --hours 24

# Dashboard is strictly loopback-bound in Python source.
cat > "$UNITS/tinvest-sandbox-dashboard.service" <<'UNIT'
[Unit]
Description=Local read-only dashboard for T-Invest Sandbox market data
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/tinvest-collector
ExecStart=/usr/bin/python3 %h/tinvest-collector/market_dashboard.py --port 8787
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now tinvest-sandbox-dashboard.service
systemctl --user enable --now tinvest-sandbox-market.timer

echo
echo "Установлен Market Recorder + Dashboard v1.5.0."
echo "Данные: $DB"
echo "Панель ТОЛЬКО на 127.0.0.1:8787. Откройте через SSH-туннель с ПК:"
echo "ssh -L 8787:127.0.0.1:8787 user@IP_UBUNTU"
echo "Потом откройте http://127.0.0.1:8787"
echo
echo "Проверка записи: systemctl --user list-timers 'tinvest-sandbox-market*'"
echo "Проверка панели: systemctl --user status tinvest-sandbox-dashboard.service --no-pager"
echo "Отчёт: python3 $DEST/market_recorder.py report --hours 24"
echo "Остановка панели: systemctl --user disable --now tinvest-sandbox-dashboard.service"
echo "Остановка записи: systemctl --user disable --now tinvest-sandbox-market.timer"
echo "Работа после отключения SSH: sudo loginctl enable-linger \"$(whoami)\""
