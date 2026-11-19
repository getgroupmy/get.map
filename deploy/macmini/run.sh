#!/bin/bash
# Started by launchd (com.getride.getmap.plist): loads getmap.env and runs the API.
set -euo pipefail
HOME_DIR="${GETMAP_HOME:-$HOME/getmap}"
set -a
# shellcheck disable=SC1091
source "$HOME_DIR/getmap.env"
set +a
mkdir -p "$SPOOL_DIR"
cd "$HOME_DIR/app"
exec "$HOME_DIR/venv/bin/uvicorn" getmap.server:app --host 0.0.0.0 --port "${PORT:-8090}" --log-level info
