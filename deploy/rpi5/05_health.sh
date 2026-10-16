#!/bin/bash
# CCTV_Rpi_5 - health check (any time). Changes nothing.
APP="${APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DB="$APP/data/iaq_cctv.db"
echo "==== power, heat, clock"
vcgencmd get_throttled; vcgencmd measure_temp
timedatectl | grep -E "Local time|synchronized"
echo; echo "==== AI HAT+ 2"
lspci | grep -i hailo || echo "NO Hailo device on PCIe"
hailortcli --version 2>/dev/null || echo "hailortcli not installed"
hailortcli fw-control identify 2>&1 | grep -E "Board Name|Firmware Version|Device Architecture" || true
echo; echo "==== CPU, memory, disk"
uptime
free -h | head -2
df -h / "$APP/data/" 2>/dev/null | sort -u
echo; echo "==== service"
printf "%-12s %s\n" cctv-rpi5 "$(systemctl is-active cctv-rpi5)"
echo "ffmpeg decoders running: $(pgrep -c -f 'ffmpeg .*rtsp' || echo 0)"
journalctl -u cctv-rpi5 -n 300 --no-pager 2>/dev/null | grep -iE "error|cannot|lost|stopped" | tail -5
if [ -f "$DB" ]; then
    echo; echo "==== newest rows (should be minutes old at most)"
    sqlite3 -readonly "$DB" "SELECT 'camera minute : ' || MAX(window_start) FROM camera_minute;
                             SELECT 'camera second : ' || MAX(Timestamp) FROM camera_second;
                             SELECT 'door minute   : ' || MAX(window_start) FROM door_minute;
                             SELECT 'sensor        : ' || MAX(ts_local) FROM sensor_readings;
                             SELECT 'forecast made : ' || MAX(predicted_at) FROM forecasts;
                             SELECT 'last warning  : ' || COALESCE(MAX(ts), '-') FROM events WHERE level IN ('warning','critical');"
    echo; echo "==== cameras in the last 10 minutes"
    sqlite3 -readonly -column "$DB" "SELECT camera, COUNT(*) AS minutes, MAX(n_person) AS max_persons FROM camera_minute
                                     WHERE window_start >= datetime('now','localtime','-10 minutes') GROUP BY camera;"
fi
