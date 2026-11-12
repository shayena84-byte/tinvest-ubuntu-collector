#!/usr/bin/env bash
# T-Invest v1.7: select existing OpenAI API key as autonomous Sandbox AI backend.
# No Ollama install, no Cloud proxy configuration, no production brokerage endpoint.
set -euo pipefail

if [[ "$(id -u)" == "0" ]]; then
  echo "Запускайте от своего пользователя Ubuntu, без sudo" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
CONF="$HOME/.config/tinvest-readonly"
UNITS="$HOME/.config/systemd/user"
KEY="$CONF/openai-api-key"

for name in python3 systemctl flock; do
  command -v "$name" >/dev/null || { echo "Отсутствует: $name" >&2; exit 2; }
done
for file in ai_sandbox_agent.py openai_quality_trader.py \
  ollama_sandbox_agent.py sandbox_autotrader.py sandbox.py \
  market_recorder.py market_dashboard.py dashboard.js dashboard.html; do
  [[ -s "$ROOT/$file" ]] || { echo "Не найден $file в ZIP" >&2; exit 2; }
done

echo "Шаг 1. Офлайн-проверка кода, изоляции Sandbox и безопасности исполнения."
cd "$ROOT"
python3 -m py_compile ai_sandbox_agent.py openai_quality_trader.py \
  ollama_sandbox_agent.py market_recorder.py market_dashboard.py
python3 -m unittest discover -s tests -v

# Reuse existing safe backup/migration/setup for read-only market recorder.
echo "Шаг 2. Обновление рыночного дашборда и резервная копия базы."
bash "$ROOT/install_market_quality.sh"

mkdir -p "$DEST" "$CONF" "$UNITS"
chmod 700 "$CONF"
for file in collector.py sandbox.py sandbox_autotrader.py \
  ai_sandbox_agent.py ollama_sandbox_agent.py \
  openai_quality_trader.py market_recorder.py market_dashboard.py dashboard.html dashboard.js; do
  install -m 700 "$ROOT/$file" "$DEST/$file"
done
# Existing dashboard user service remains active unless explicitly restarted.
systemctl --user restart tinvest-sandbox-dashboard.service

# Do not accidentally run two bots against the same virtual account.
systemctl --user disable --now tinvest-sandbox-ai.timer 2>/dev/null || true
systemctl --user disable --now tinvest-sandbox-autotrader.timer 2>/dev/null || true
systemctl --user disable --now tinvest-sandbox-local-ai.timer 2>/dev/null || true
systemctl --user disable --now tinvest-sandbox-openai.timer 2>/dev/null || true

cat >"$UNITS/tinvest-sandbox-openai.service" <<'UNIT'
[Unit]
Description=OpenAI autonomous virtual decisions - T-Invest Sandbox ONLY
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/tinvest-collector
Environment=SSL_CERT_FILE=%h/.config/tinvest-readonly/ca-bundle.pem
ExecStart=/usr/bin/flock -n %h/.local/state/tinvest-sandbox-trader/run.lock /usr/bin/python3 %h/tinvest-collector/openai_quality_trader.py run
TimeoutStartSec=245
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
UNIT

cat >"$UNITS/tinvest-sandbox-openai.timer" <<'UNIT'
[Unit]
Description=OpenAI analysis of virtual Sandbox portfolio every 5 minutes

[Timer]
# Run 60 seconds after recorder's 0/5/10/15-minute collection.
OnCalendar=*-*-* *:01/5:00
AccuracySec=15s
Persistent=false
Unit=tinvest-sandbox-openai.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload

if [[ ! -s "$KEY" ]]; then
  echo "Не найден существующий ключ OpenAI: $KEY" >&2
  echo "Создайте его безопасным вводом: python3 ~/tinvest-collector/ai_sandbox_agent.py setup" >&2
  echo "Таймер останется выключенным." >&2
  exit 1
fi
chmod 600 "$KEY"
if [[ ! -s "$CONF/sandbox-token" || ! -s "$CONF/ca-bundle.pem" ]]; then
  echo "Не настроен T-Invest Sandbox-токен или набор корневых сертификатов." >&2
  echo "Таймер останется выключенным." >&2
  exit 1
fi

echo "Шаг 3. GET /v1/models для уже сохранённого OpenAI API-ключа; без заявок."
if SSL_CERT_FILE="$CONF/ca-bundle.pem" /usr/bin/python3 \
   "$DEST/openai_quality_trader.py" doctor; then
  systemctl --user enable --now tinvest-sandbox-openai.timer
  echo "УСПЕШНО: таймер OpenAI Sandbox ВКЛЮЧЁН (каждые 5 минут)."
else
  echo "Диагностика OpenAI API не прошла: торговый таймер ОСТАЛСЯ ВЫКЛЮЧЕННЫМ." >&2
  echo "Проверьте причину. Не отключайте проверки доступа и риска." >&2
  exit 1
fi

echo
echo "Дашборд: http://127.0.0.1:8787 (через существующий SSH-туннель)"
echo "AI status: python3 $DEST/openai_quality_trader.py status"
echo "Тест без сделок: python3 $DEST/openai_quality_trader.py dry-run"
echo "Расписание: systemctl --user list-timers 'tinvest-sandbox-*'"
echo "Журнал: journalctl --user -u tinvest-sandbox-openai.service -n 80 --no-pager"
echo "СТОП: systemctl --user disable --now tinvest-sandbox-openai.timer"
