#!/usr/bin/env bash
# Compatibility entry point: v1.11 installs every dependency and never enables trading.
set -euo pipefail
TASK_SOURCE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$TASK_SOURCE/install_project.py" "$@"
