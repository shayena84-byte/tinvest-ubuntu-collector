#!/usr/bin/env bash
# Install an independent read-only market recorder. NOT a trading bot.
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Запускайте от обычного пользователя Ubuntu, без sudo." >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
CONF="$HOME/.config/tinvest-readonly"
MARKET="$HOME/.local/share/tinvest-market"
UNITS="$HOME/.config/systemd/user"

for exe in python3 proxychains4 flock systemctl; do
  if ! command -v "$exe" >/dev/null 2>&1; then
    echo "Не найдена команда: $exe" >&2
    exit 2
  fi
done

if [[ ! -s "$CONF/sandbox-token" || ! -s "$CONF/ca-bundle.pem" || ! -s "$CONF/proxychains.conf" ]]; then
  echo "Не найдены sandbox-token, ca-bundle.pem либо proxychains.conf." >&2
  echo "Сначала проверьте существующее подключение T-Invest Sandbox через SOCKS5." >&2
  exit 1
fi
if ! grep -Eq '^[[:space:]]*socks5[[:space:]]+192[.]168[.]202[.]50[[:space:]]+1080([[:space:]]|$)' "$CONF/proxychains.conf"; then
  echo "Ожидаю SOCKS5 192.168.202.50:1080 в конфигурации." >&2
  exit 1
fi

mkdir -p "$DEST" "$CONF" "$MARKET" "$UNITS"
chmod 700 "$CONF" "$MARKET"
chmod 600 "$CONF/sandbox-token" "$CONF/ca-bundle.pem" "$CONF/proxychains.conf"
cd "$ROOT"

echo "Проверка кода и всех автономных тестов..."
python3 -m py_compile collector.py sandbox.py market_recorder.py
python3 -m unittest discover -s tests -v

install -m 700 "$ROOT/collector.py" "$DEST/collector.py"
install -m 700 "$ROOT/sandbox.py" "$DEST/sandbox.py"
install -m 700 "$ROOT/market_recorder.py" "$DEST/market_recorder.py"

if [[ ! -e "$CONF/market-recorder.json" ]]; then
  (
    umask 077
    cat > "$CONF/market-recorder.json" <<'JSON'
{
  "depth": 10,
  "instruments": [
    {"symbol": "SBER", "board": "TQBR", "kind": "share"},
    {"symbol": "RU000A1092T7", "board": "TQCB", "kind": "bond"}
  ]
}
JSON
  )
fi
chmod 600 "$CONF/market-recorder.json"

# Create separate systemd user units, no global network change or service override.
cat > "$UNITS/tinvest-sandbox-market.service" <<'UNIT'
[Unit]
Description=Read-only T-Invest Sandbox market history (SQLite)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/tinvest-collector
Environment=SSL_CERT_FILE=%h/.config/tinvest-readonly/ca-bundle.pem
ExecStart=/usr/bin/flock -n %h/.local/share/tinvest-market/run.lock /usr/bin/proxychains4 -q -f %h/.config/tinvest-readonly/proxychains.conf /usr/bin/python3 %h/tinvest-collector/market_recorder.py collect
TimeoutStartSec=240
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
UNIT

cat > "$UNITS/tinvest-sandbox-market.timer" <<'UNIT'
[Unit]
Description=Record market data every 5 minutes (Sandbox; read-only)

[Timer]
OnCalendar=*-*-* *:00/5:00
AccuracySec=5s
Persistent=true
Unit=tinvest-sandbox-market.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload

# Explicitly keep BOTH AI/trading bots disabled. Data collection is independent.
systemctl --user disable --now tinvest-sandbox-ai.timer 2>/dev/null || true
systemctl --user disable --now tinvest-sandbox-autotrader.timer 2>/dev/null || true

echo "Пробный сбор через SOCKS5 192.168.202.50:1080 (не совершает сделок)..."
if SSL_CERT_FILE="$CONF/ca-bundle.pem" \
  proxychains4 -q -f "$CONF/proxychains.conf" \
  python3 "$DEST/market_recorder.py" collect; then
  systemctl --user enable --now tinvest-sandbox-market.timer
else
  echo "Пробный сбор не прошёл: таймер останется выключенным." >&2
  echo "Проверьте сертификаты, прокси и ошибки market_recorder.py." >&2
  exit 1
fi

echo
echo "УСТАНОВЛЕНО: T-Invest Sandbox Market Recorder."
echo "БД: $MARKET/market.sqlite3"
echo "Таймер: systemctl --user list-timers 'tinvest-sandbox-market*'"
echo "История: python3 $DEST/market_recorder.py report --hours 24"
echo "Журнал: journalctl --user -u tinvest-sandbox-market.service -n 60 --no-pager"
echo "CSV: python3 $DEST/market_recorder.py export --hours 168"
echo "Остановка: systemctl --user disable --now tinvest-sandbox-market.timer"
echo "Для автономной работы после SSH: sudo loginctl enable-linger \"$(whoami)\""
