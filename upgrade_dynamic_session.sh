#!/usr/bin/env bash
# Safe in-place v1.8.0 update: dynamic GetTradingStatus in OpenAI Sandbox trader.
# Leaves SOCKS5 override, credentials, SQLite, dashboard and timer state intact.
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Запускайте от обычного пользователя Ubuntu, без sudo." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
BACKUPS="$HOME/.local/share/tinvest-market/code-backups"
UNIT="tinvest-sandbox-openai"

for file in "$ROOT/openai_quality_trader.py" "$ROOT/trading_session.py"; do
  if [[ ! -s "$file" ]]; then
    echo "В установочном архиве не найден $file" >&2
    exit 1
  fi
done
if [[ ! -f "$DEST/openai_quality_trader.py" || ! -f "$DEST/ai_sandbox_agent.py" ]]; then
  echo "Не обнаружен ранее установленный OpenAI Trader v1.7." >&2
  exit 1
fi

echo "1. Останавливаем только расписание OpenAI Sandbox; остальные службы не меняем."
systemctl --user disable --now "$UNIT.timer" 2>/dev/null || true
if systemctl --user is-active --quiet "$UNIT.service"; then
  echo "Служба уже выполняет цикл. Не заменяем код во время операции." >&2
  echo "После завершения текущего запуска повторите установку." >&2
  exit 1
fi

echo "2. Проверяем код и офлайн-тесты из релиза."
cd "$ROOT"
python3 -m py_compile trading_session.py openai_quality_trader.py
python3 -m unittest discover -s tests -v

echo "3. Резервная копия программы и обновление."
mkdir -p "$BACKUPS"
chmod 700 "$BACKUPS"
stamp="$(date -u +%Y%m%d_%H%M%S)"
cp -p "$DEST/openai_quality_trader.py" "$BACKUPS/openai_quality_trader_$stamp.py"
chmod 600 "$BACKUPS/openai_quality_trader_$stamp.py"
install -m 700 "$ROOT/trading_session.py" "$DEST/trading_session.py"
install -m 700 "$ROOT/openai_quality_trader.py" "$DEST/openai_quality_trader.py"

echo "4. Проверяем syntax и показываем сохранённые настройки запуска."
python3 -m py_compile "$DEST/trading_session.py" "$DEST/openai_quality_trader.py"
systemctl --user daemon-reload
systemctl --user show "$UNIT.service" -p ExecStart --no-pager | fold -w 140

echo
echo "Обновление v1.8.0 установлено. Торговый таймер намеренно ОСТАВЛЕН выключенным."
echo "SOCKS5 override не менялся; ваш ProxyChains сохранён."
echo "Проверка статусов (не делает сделок):"
echo 'SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" proxychains4 -f "$HOME/.config/tinvest-readonly/proxychains.conf" python3 ~/tinvest-collector/openai_quality_trader.py session'
echo
echo "Затем сделайте dry-run и только после успешной проверки включите таймер:"
echo "systemctl --user enable --now tinvest-sandbox-openai.timer"
echo "Остановка: systemctl --user disable --now tinvest-sandbox-openai.timer"
