#!/usr/bin/env bash
# Replace old indicator bot with model-driven agent for virtual T-Invest Sandbox only.
set -euo pipefail
if [[ "$(id -u)" == "0" ]]; then
  echo "Install as the normal Ubuntu user; do not use sudo." >&2
  exit 1
fi
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
CONF="$HOME/.config/tinvest-readonly"
STATE="$HOME/.local/state/tinvest-sandbox-trader"
UNITS="$HOME/.config/systemd/user"

mkdir -p "$DEST" "$CONF" "$STATE" "$UNITS"
chmod 700 "$DEST" "$CONF" "$STATE"

if [[ ! -f "$CONF/sandbox-token" || ! -f "$CONF/ca-bundle.pem" || ! -f "$CONF/autotrader.json" ]]; then
  echo "Sandbox v1.2 not configured: missing sandbox-token, ca-bundle.pem, or autotrader.json." >&2
  exit 1
fi
if ! command -v flock >/dev/null; then
  echo "Missing flock. Install util-linux." >&2
  exit 1
fi

cd "$ROOT"
python3 -m py_compile collector.py sandbox.py sandbox_autotrader.py ai_sandbox_agent.py
python3 -m unittest discover -s tests -v

# Replace old strategy: never allow simultaneous bot timers on the same virtual account.
systemctl --user disable --now tinvest-sandbox-autotrader.timer 2>/dev/null || true
systemctl --user disable --now tinvest-sandbox-ai.timer 2>/dev/null || true

install -m 700 collector.py "$DEST/collector.py"
install -m 700 sandbox.py "$DEST/sandbox.py"
install -m 700 sandbox_autotrader.py "$DEST/sandbox_autotrader.py"
install -m 700 ai_sandbox_agent.py "$DEST/ai_sandbox_agent.py"

if [[ ! -f "$CONF/ai-agent.json" ]]; then
  cat >"$CONF/ai-agent.json" <<'JSON'
{
  "enabled": true,
  "model": "gpt-6.1-sol",
  "watchlist": ["SBER", "MOEX", "YDEX", "ROSN", "LKOH", "NVTK", "T", "VTBR"],
  "minimum_confidence": 0.65,
  "max_ai_calls_per_day": 110,
  "max_trade_fraction": 0.42,
  "max_exposure_fraction": 0.82,
  "minimum_cash_rub": 850,
  "max_orders_per_day": 8,
  "cooldown_minutes": 20,
  "max_daily_loss_fraction": 0.08
}
JSON
  chmod 600 "$CONF/ai-agent.json"
fi

cat >"$UNITS/tinvest-sandbox-ai.service" <<'UNIT'
[Unit]
Description=Model-driven autonomous trading for T-Invest Sandbox ONLY
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/tinvest-collector
Environment=SSL_CERT_FILE=%h/.config/tinvest-readonly/ca-bundle.pem
ExecStart=/usr/bin/flock -n %h/.local/state/tinvest-sandbox-trader/run.lock /usr/bin/python3 %h/tinvest-collector/ai_sandbox_agent.py run
TimeoutStartSec=180
NoNewPrivileges=true
PrivateTmp=true
UNIT

cat >"$UNITS/tinvest-sandbox-ai.timer" <<'UNIT'
[Unit]
Description=Ask an AI model for a NEW virtual trade decision every 5 minutes

[Timer]
OnCalendar=*-*-* *:00/5:00
AccuracySec=5s
Persistent=true
Unit=tinvest-sandbox-ai.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
if [[ ! -s "$CONF/openai-api-key" ]]; then
  echo
  echo "Old indicator bot DISABLED; model agent code is installed but is NOT running."
  echo "Create an OpenAI API key at https://platform.openai.com/api-keys and configure separate API billing."
  echo "Then enter it ONLY on Ubuntu: python3 ~/tinvest-collector/ai_sandbox_agent.py setup"
  echo "Finally start the new agent: systemctl --user enable --now tinvest-sandbox-ai.timer"
  exit 0
fi
echo "Installed patched AI agent. Sandbox timer is DISABLED until you verify one dry-run."
echo "Test: SSL_CERT_FILE=$HOME/.config/tinvest-readonly/ca-bundle.pem python3 $DEST/ai_sandbox_agent.py dry-run"
echo "Then enable: systemctl --user enable --now tinvest-sandbox-ai.timer"
echo "Logs: journalctl --user -u tinvest-sandbox-ai.service -n 60 --no-pager"
echo "STOP: systemctl --user disable --now tinvest-sandbox-ai.timer"
