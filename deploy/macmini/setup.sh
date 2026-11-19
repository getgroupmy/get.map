#!/bin/bash
# One-time setup of get.map on a Mac mini (Apple silicon). Safe to run again.
#
#   git clone https://github.com/getgroupmy/get.map.git ~/getmap/app
#   bash ~/getmap/app/deploy/macmini/setup.sh
#
# It installs Postgres 17 (Homebrew), tunes it for get.map, creates the
# database, applies the migrations, makes a Python environment and starts
# get.map and its nightly backup with launchd.
set -euo pipefail

HOME_DIR="$HOME/getmap"
APP="$HOME_DIR/app"
HERE="$APP/deploy/macmini"
[ -d "$APP/getmap" ] || { echo "Clone get.map to $APP first (see the top of this file)."; exit 1; }
mkdir -p "$HOME_DIR/logs" /Users/Shared/getmap/spool

# 1. Homebrew packages
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
brew install postgresql@17 python@3.12
brew services start postgresql@17
PG_BIN="$(brew --prefix postgresql@17)/bin"
for _ in $(seq 1 30); do "$PG_BIN/pg_isready" -q && break; sleep 1; done

# 2. Tune Postgres (an include line, so Homebrew upgrades keep it)
PGDATA="$("$PG_BIN/psql" -d postgres -Atc 'show data_directory')"
cp "$HERE/postgresql-getmap.conf" "$PGDATA/getmap.conf"
grep -q "include_if_exists = 'getmap.conf'" "$PGDATA/postgresql.conf" \
  || echo "include_if_exists = 'getmap.conf'" >> "$PGDATA/postgresql.conf"
brew services restart postgresql@17
for _ in $(seq 1 30); do "$PG_BIN/pg_isready" -q && break; sleep 1; done

# 3. Database, role, migrations
"$PG_BIN/psql" -d postgres -tc "select 1 from pg_roles where rolname = 'getmap'" | grep -q 1 \
  || "$PG_BIN/createuser" getmap
"$PG_BIN/psql" -d postgres -tc "select 1 from pg_database where datname = 'getmap'" | grep -q 1 \
  || "$PG_BIN/createdb" -O getmap getmap
"$PG_BIN/psql" -v ON_ERROR_STOP=1 -q -U getmap -d getmap -f "$APP/db/migrations/001_map_capture.sql"
if brew install postgis 2>/dev/null && "$PG_BIN/psql" -d getmap -c "create extension if not exists postgis" 2>/dev/null; then
  "$PG_BIN/psql" -v ON_ERROR_STOP=1 -q -U getmap -d getmap -f "$APP/db/migrations/002_postgis.sql" \
    && echo "PostGIS: on"
else
  echo "PostGIS: not available for this Postgres; skipped (optional)."
fi

# 4. Python environment
"$(brew --prefix python@3.12)/bin/python3.12" -m venv "$HOME_DIR/venv"
"$HOME_DIR/venv/bin/pip" install -q --upgrade pip
"$HOME_DIR/venv/bin/pip" install -q -r "$APP/requirements.txt"

# 5. Settings
if [ ! -f "$HOME_DIR/getmap.env" ]; then
  ui_password="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
  sed -e "s|^CAPTURE_API_KEY=.*|CAPTURE_API_KEY=$(openssl rand -hex 32)|" \
      -e "s|^UI_PASSWORD=.*|UI_PASSWORD=$ui_password|" "$HERE/getmap.env.example" > "$HOME_DIR/getmap.env"
  chmod 600 "$HOME_DIR/getmap.env"
  echo "Wrote $HOME_DIR/getmap.env (a new CAPTURE_API_KEY and viewer password are in it)."
  echo "Data viewer: http://$(ipconfig getifaddr en0 2>/dev/null || echo localhost):8090/ui  user: admin  password: $ui_password"
fi

# 6. launchd: get.map (always on) and the nightly backup
mkdir -p "$HOME/Library/LaunchAgents"
for name in com.getride.getmap com.getride.getmap-backup; do
  sed "s|__HOME__|$HOME|g" "$HERE/$name.plist" > "$HOME/Library/LaunchAgents/$name.plist"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$name.plist"
done

sleep 3
curl -fsS "http://localhost:8090/health" && echo && echo "get.map is running on port 8090."
echo "Next: the power settings and the NAS / tunnel steps in deploy/macmini/README.md."
