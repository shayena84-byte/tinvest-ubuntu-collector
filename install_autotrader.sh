#!/usr/bin/env bash
# One-time installer for self-running T-Invest Sandbox bot.
set -euo pipefail
if [[ $# -ne 1 || ! "$1" =~ ^tb_[0-9a-f]{12}$ ]]; then
  echo "Использование: bash install_autotrader.sh tb_XXXXXXXXXXXX" >&2
  exit 2
fi
if [[ "$(id -u)" == 0 ]]; then
  echo "Не запускайте от root и не используйте sudo." >&2
  exit 2
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/tinvest-collector"
CONFIG_DIR="$HOME/.config/tinvest-readonly"
STATE_DIR="$HOME/.local/state/tinvest-sandbox-trader"
USER_UNITS="$HOME/.config/systemd/user"
mkdir -p "$DEST" "$CONFIG_DIR" "$STATE_DIR" "$USER_UNITS"
chmod 700 "$CONFIG_DIR" "$STATE_DIR"
if [[ ! -f "$CONFIG_DIR/sandbox-token" || ! -f "$CONFIG_DIR/ca-bundle.pem" ]]; then
  echo "Нет Sandbox-токена или доверенного TLS bundle. Сначала настройте sandbox.py." >&2
  exit 1
fi
install -m 700 "$ROOT/collector.py" "$DEST/collector.py"
install -m 700 "$ROOT/sandbox.py" "$DEST/sandbox.py"
install -m 700 "$ROOT/sandbox_autotrader.py" "$DEST/sandbox_autotrader.py"

if [[ -f "$CONFIG_DIR/autotrader.json" ]]; then
  echo "Конфигурация уже существует — не перезаписываю её."
else
  python3 - "$1" "$CONFIG_DIR/autotrader.json" <<'PY'
import json, os, sys
a, path = sys.argv[1:]
obj={"account_ref":a,"enabled":True,"symbols":["SBER","MOEX","YDEX"],
     "max_position_fraction":0.42,"max_gross_fraction":0.82,
     "min_cash_rub":850,"max_orders_per_day":8,"daily_loss_limit":0.08,
     "stop_loss":0.025,"take_profit":0.045,"trailing_stop":0.025,
     "cooldown_minutes":20}
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w") as f:
    json.dump(obj,f,indent=2)
PY
fi

cat >"$USER_UNITS/tinvest-sandbox-autotrader.service" <<'EOF'
[Unit]
Description=T-Invest Sandbox equity bot (virtual funds ONLY)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/tinvest-collector
Environment=SSL_CERT_FILE=%h/.config/tinvest-readonly/ca-bundle.pem
ExecStart=/usr/bin/flock -n %h/.local/state/tinvest-sandbox-trader/run.lock /usr/bin/python3 %h/tinvest-collector/sandbox_autotrader.py run
TimeoutStartSec=115
NoNewPrivileges=true
PrivateTmp=true
EOF

cat >"$USER_UNITS/tinvest-sandbox-autotrader.timer" <<'EOF'
[Unit]
Description=Poll T-Invest Sandbox every 5 minutes

[Timer]
OnCalendar=*-*-* *:00/5:00
AccuracySec=5s
Persistent=true
Unit=tinvest-sandbox-autotrader.service

[Install]
WantedBy=timers.target
EOF

# Use ONLY offline tests before enabling the timer.
cd "$ROOT"
python3 -m py_compile collector.py sandbox.py sandbox_autotrader.py
python3 -m unittest discover -s tests -v
systemctl --user daemon-reload
systemctl --user enable --now tinvest-sandbox-autotrader.timer
echo
echo "Готово! Теперь Ubuntu каждые 5 минут проверяет рынок и может совершать ТОЛЬКО ВИРТУАЛЬНЫЕ сделки."
echo "Проверить расписание: systemctl --user list-timers 'tinvest-sandbox-*'"
echo "Журнал: journalctl --user -u tinvest-sandbox-autotrader.service -n 60 --no-pager"
echo "Приостановить: systemctl --user disable --now tinvest-sandbox-autotrader.timer"
echo "Для продолжения работы после выхода из SSH: sudo loginctl enable-linger $(whoami)"
