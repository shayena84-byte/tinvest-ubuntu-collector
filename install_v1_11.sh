#!/usr/bin/env bash
set -euo pipefail
TASK_SOURCE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$TASK_SOURCE/install_project.py" "$@"
