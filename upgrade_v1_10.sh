#!/usr/bin/env bash
# Add read-only offline market audit to an existing v1.9.0 installation.
set -euo pipefail
[[ "$(id -u)" -ne 0 ]] || { echo "Run as normal Ubuntu user, without sudo" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/tinvest-collector"
[[ -f "$DEST/market_recorder.py" && -f "$DEST/v19_health.py" ]] || {
  echo "Existing v1.9.0 installation not detected; refusing upgrade" >&2; exit 2; }
[[ -s "$ROOT/offline_market_audit.py" ]] || { echo "Archive incomplete" >&2; exit 2; }
cd "$ROOT"
python3 -m py_compile offline_market_audit.py
python3 -m unittest discover -s tests -v
install -m 700 "$ROOT/offline_market_audit.py" "$DEST/offline_market_audit.py"
python3 -m py_compile "$DEST/offline_market_audit.py"
echo "Read-only offline audit installed. No services, timers, credentials, proxies or databases changed."
echo "python3 ~/tinvest-collector/offline_market_audit.py --hours 24"
