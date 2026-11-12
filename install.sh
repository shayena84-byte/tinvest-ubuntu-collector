#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" == 0 ]]; then
  echo "Запускайте установку от обычного пользователя Ubuntu, без sudo." >&2
  exit 1
fi
command -v python3 >/dev/null || { echo 'Сначала установите Python 3: sudo apt update && sudo apt install -y python3' >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/tinvest-collector"
mkdir -p "$DEST" "$HOME/tinvest-reports" "$HOME/.config/tinvest-readonly"
chmod 700 "$DEST" "$HOME/tinvest-reports" "$HOME/.config/tinvest-readonly"
install -m 700 "$ROOT/collector.py" "$DEST/collector.py"
if [[ -f "$ROOT/sandbox.py" ]]; then
  install -m 700 "$ROOT/sandbox.py" "$DEST/sandbox.py"
fi
echo "Сборщик установлен в $DEST"
echo "1. Выпустите READ-ONLY токен Т-Банка."
echo "2. Запустите: python3 $DEST/collector.py setup"
echo "3. Проверьте: python3 $DEST/collector.py accounts"
echo "4. Выполните: python3 $DEST/collector.py snapshot --days 90"
echo "5. Отчёт будет в каталоге ~/tinvest-reports/"
