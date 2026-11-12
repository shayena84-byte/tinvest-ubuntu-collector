#!/usr/bin/env bash
# v1.6.0: extend market analytics and optionally activate local Ollama Sandbox AI.
# SAFETY: no production tokens or production order routes. Never enable old OpenAI bot.
set -euo pipefail

if [[ "$(id -u)" == "0" ]]; then
  echo "Используйте обычного пользователя Ubuntu, без sudo." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
CONF="$HOME/.config/tinvest-readonly"
UNITS="$HOME/.config/systemd/user"

for file in market_dashboard.py market_recorder.py dashboard.js dashboard.html ollama_sandbox_agent.py; do
  if [[ ! -s "$ROOT/$file" ]]; then
    echo "Файл $file отсутствует в релизе. Прекращаю установку." >&2
    exit 1
  fi
done

echo "ШАГ 1. Резервное копирование и обновление локального рыночного дашборда..."
bash "$ROOT/install_market_quality.sh"
# Existing enabled service otherwise keeps the OLD Python HTTP code in memory.
systemctl --user restart tinvest-sandbox-dashboard.service

echo "ШАГ 2. Добавление мониторинга дополнительных акций и облигаций..."
python3 - "$CONF/market-recorder.json" <<'PY'
import json, os, sys
from pathlib import Path
path=Path(sys.argv[1])
conf=json.loads(path.read_text(encoding="utf-8"))
extra=[
 {"symbol":"MOEX","board":"TQBR","kind":"share"},
 {"symbol":"YDEX","board":"TQBR","kind":"share"},
 {"symbol":"ROSN","board":"TQBR","kind":"share"},
 {"symbol":"LKOH","board":"TQBR","kind":"share"},
 {"symbol":"NVTK","board":"TQBR","kind":"share"},
 {"symbol":"RU000A106MV2","board":"TQCB","kind":"bond"},
 {"symbol":"SU26232RMFS7","board":"TQCB","kind":"bond"},
 {"symbol":"SU26251RMFS7","board":"TQCB","kind":"bond"},
]
seen={(x["symbol"],x["board"]) for x in conf["instruments"]}
for spec in extra:
    if len(conf["instruments"])>=10:break
    if (spec["symbol"],spec["board"]) not in seen:
        conf["instruments"].append(spec)
        seen.add((spec["symbol"],spec["board"]))
temp=path.with_suffix(".tmp")
temp.write_text(json.dumps(conf,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
os.chmod(temp,0o600)
temp.replace(path)
print("Отслеживаемых бумаг:",len(conf["instruments"]))
print("Список:",", ".join(x["symbol"] for x in conf["instruments"]))
PY

echo "ШАГ 3. Установка локального ИИ-агента для T-Invest Sandbox..."
install -m 700 "$ROOT/ollama_sandbox_agent.py" "$DEST/ollama_sandbox_agent.py"

if [[ ! -f "$CONF/ollama-sandbox.json" ]]; then
  (
    umask 077
    cat >"$CONF/ollama-sandbox.json" <<'JSON'
{
  "enabled":true,
  "model":"qwen3:4b",
  "minimum_confidence":0.70,
  "max_trade_fraction":0.35,
  "max_exposure_fraction":0.80,
  "minimum_cash_rub":850,
  "max_orders_per_day":6,
  "cooldown_minutes":20,
  "max_daily_loss_fraction":0.07,
  "watchlist":["SBER","MOEX","YDEX","ROSN","LKOH","NVTK"],
  "max_quote_age_seconds":900,
  "max_snapshot_age_seconds":600,
  "max_orderbook_spread_pct":0.8
}
JSON
  )
fi
chmod 600 "$CONF/ollama-sandbox.json"

cat >"$UNITS/tinvest-sandbox-local-ai.service" <<'UNIT'
[Unit]
Description=Local Ollama AI trade decision/virtual order (T-Invest Sandbox ONLY)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/tinvest-collector
Environment=SSL_CERT_FILE=%h/.config/tinvest-readonly/ca-bundle.pem
ExecStart=/usr/bin/flock -n %h/.local/state/tinvest-sandbox-trader/run.lock /usr/bin/python3 %h/tinvest-collector/ollama_sandbox_agent.py run
TimeoutStartSec=260
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
UNIT

cat >"$UNITS/tinvest-sandbox-local-ai.timer" <<'UNIT'
[Unit]
Description=Local AI selects virtual Sandbox trades every 5 minutes

[Timer]
OnCalendar=*-*-* *:00/5:00
AccuracySec=8s
Persistent=true
Unit=tinvest-sandbox-local-ai.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user disable --now tinvest-sandbox-ai.timer 2>/dev/null || true
systemctl --user disable --now tinvest-sandbox-autotrader.timer 2>/dev/null || true
# Require available model before enabling the timer.
systemctl --user disable --now tinvest-sandbox-local-ai.timer 2>/dev/null || true

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama пока не установлен. Инструменты аналитики УЖЕ работают, торговый AI-таймер выключен."
  echo "Установите Ollama с официального сайта: https://ollama.com/download/linux"
  echo "Потом: ollama pull qwen3:4b"
  echo "Проверка: python3 ~/tinvest-collector/ollama_sandbox_agent.py doctor"
  echo "Старт: systemctl --user enable --now tinvest-sandbox-local-ai.timer"
  exit 0
fi

echo "ШАГ 4. Проверка доступности модели перед стартом..."
if /usr/bin/python3 "$DEST/ollama_sandbox_agent.py" doctor; then
  systemctl --user enable --now tinvest-sandbox-local-ai.timer
  echo "Локальная ИИ-модель ответила корректно, торговый таймер Sandbox ВКЛЮЧЕН."
else
  echo "AI-таймер НЕ включён: Ollama/model пока не прошли проверку."
  echo "После устранения ошибки: python3 ~/tinvest-collector/ollama_sandbox_agent.py doctor"
  echo "Затем: systemctl --user enable --now tinvest-sandbox-local-ai.timer"
fi

echo
echo "Аналитика: http://127.0.0.1:8787 (через существующий SSH-туннель)"
echo "Статус: systemctl --user list-timers 'tinvest-sandbox-*'"
echo "Журнал: journalctl --user -u tinvest-sandbox-local-ai.service -n 80 --no-pager"
echo "Капитал Sandbox: python3 ~/tinvest-collector/ollama_sandbox_agent.py status"
echo "STOP: systemctl --user disable --now tinvest-sandbox-local-ai.timer"
