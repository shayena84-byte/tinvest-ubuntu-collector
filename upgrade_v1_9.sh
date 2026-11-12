#!/usr/bin/env bash
# Safe 1.9.0 upgrade. Does not start automated virtual trading.
set -euo pipefail
[[ "$(id -u)" -ne 0 ]] || { echo "Run without sudo" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
UNIT="tinvest-sandbox-openai"
for file in openai_quality_trader.py v19_guard.py v19_health.py; do
  [[ -s "$ROOT/$file" ]] || { echo "Missing $file" >&2; exit 2; }
done
[[ -f "$DEST/openai_quality_trader.py" ]] || { echo "Install v1.8.0 first" >&2; exit 2; }
systemctl --user disable --now "$UNIT.timer" 2>/dev/null || true
if systemctl --user is-active --quiet "$UNIT.service"; then
  echo "OpenAI service still running: abort." >&2
  exit 1
fi
cd "$ROOT"
python3 -m py_compile openai_quality_trader.py v19_guard.py v19_health.py
python3 -m unittest discover -s tests -v
BACKUP="$HOME/.local/share/tinvest-market/code-backups"
mkdir -p "$BACKUP"
chmod 700 "$BACKUP"
cp -p "$DEST/openai_quality_trader.py" "$BACKUP/openai_quality_trader_$(date -u +%Y%m%d_%H%M%S).py"
for file in openai_quality_trader.py v19_guard.py v19_health.py; do
  install -m 700 "$ROOT/$file" "$DEST/$file"
done
python3 -m py_compile "$DEST/openai_quality_trader.py" "$DEST/v19_guard.py" "$DEST/v19_health.py"
echo "v1.9.0 installed. Timer remains disabled. SOCKS5, keys, SQLite unchanged."
echo "Health: python3 ~/tinvest-collector/v19_health.py"
echo "AI: python3 ~/tinvest-collector/openai_quality_trader.py status"
echo "After verifying API billing, explicit quota reset:"
echo "python3 ~/tinvest-collector/openai_quality_trader.py reset-api-block"
