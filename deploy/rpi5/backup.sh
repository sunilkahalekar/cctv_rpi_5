#!/bin/bash
# Nightly safe copy of the database (SQLite .backup works while the app writes); keeps 30 days.
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST=/mnt/usbdata/backups/cctv
mountpoint -q /mnt/usbdata || DEST="$APP/data/backups"
mkdir -p "$DEST"
sqlite3 "$APP/data/iaq_cctv.db" ".backup '$DEST/iaq_cctv-$(date +%F).db'"
find "$DEST" -name 'iaq_cctv-*.db' -mtime +30 -delete
echo "$(date '+%F %T') backup -> $DEST"
