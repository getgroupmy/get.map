#!/bin/bash
# Nightly backup of the map database to the NAS (com.getride.getmap-backup.plist, 03:30).
# Raw answers are left out (they are kept 7 days and are the bulkiest part).
# Keeps the newest $BACKUP_KEEP dumps.
set -euo pipefail
HOME_DIR="${GETMAP_HOME:-$HOME/getmap}"
set -a
# shellcheck disable=SC1091
source "$HOME_DIR/getmap.env"
set +a
PG_BIN="$(brew --prefix postgresql@17)/bin"

if [ ! -d "$BACKUP_DIR" ]; then
  echo "$(date) backup folder $BACKUP_DIR is not mounted; skipped" >&2
  exit 1
fi
stamp=$(date +%Y%m%d-%H%M)
tmp="$BACKUP_DIR/getmap-$stamp.dump.partial"
"$PG_BIN/pg_dump" --format=custom --compress=zstd:3 \
  --exclude-table-data='map.raw_responses*' \
  --file="$tmp" "$DATABASE_URL"
mv "$tmp" "$BACKUP_DIR/getmap-$stamp.dump"
# shellcheck disable=SC2012
ls -1t "$BACKUP_DIR"/getmap-*.dump | tail -n +$(( ${BACKUP_KEEP:-7} + 1 )) | xargs -r rm -f
echo "$(date) backup ok: getmap-$stamp.dump ($(du -h "$BACKUP_DIR/getmap-$stamp.dump" | cut -f1))"
