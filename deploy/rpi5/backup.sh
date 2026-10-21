#!/bin/bash
# Every night (cron 02:40, set by 04_service.sh):
#  1. per-second detections of each finished day -> compressed CSV on the USB drive (kept for ever;
#     the database itself keeps them only pipeline.keep_seconds_days = 7 days)
#  2. a safe copy of the whole database (SQLite .backup works while the app writes); keeps KEEP_DAYS.
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEEP_DAYS="${KEEP_DAYS:-7}"
DEST=/mnt/usbdata/backups/cctv
mountpoint -q /mnt/usbdata || DEST="$APP/data/backups"
mkdir -p "$DEST"
"$APP/cctv_rpi/bin/python" "$APP/tools/archive_seconds.py" --db "$APP/data/iaq_cctv.db" --out /mnt/usbdata/cctv/archive || echo "archive failed"
sqlite3 "$APP/data/iaq_cctv.db" ".backup '$DEST/iaq_cctv-$(date +%F).db'"
find "$DEST" -name 'iaq_cctv-*.db' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') backup -> $DEST ($(du -sh "$DEST" | cut -f1) in backups); free on drive: $(df -h "$DEST" | awk 'NR==2 {print $4}')"
