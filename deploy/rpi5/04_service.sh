#!/bin/bash
# CCTV_Rpi_5 - step 4 (on the Pi): start the system at boot, keep its secrets in one root-only
# file, nightly database backup. Run only after `cctv_rpi/bin/python run.py`
# worked by hand.
set -euo pipefail
APP="${APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$APP"
ENVF=/etc/cctv-rpi5.env
[ -x cctv_rpi/bin/python ] || { echo "STOP: run 03_app.sh first"; exit 1; }

# refuse to enable a system whose model is missing
cctv_rpi/bin/python - <<'PY'
import sys
from iaqcam.config import load_config
from pathlib import Path
cfg = load_config("config.yaml")
m = cfg["model"]
if m["backend"] == "hailo" and not Path(m["hef"]).is_file():
    sys.exit(f"STOP: model.hef not found: {m['hef']}")
print("model found:", m.get("hef") or m["weights"])
PY

# secrets: NVR password and ESP32 token, readable only by root and systemd
if ! sudo test -f "$ENVF"; then
    read -r -p  "NVR user [admin]: " NU; NU=${NU:-admin}
    read -r -s -p "NVR password: " NP; echo
    read -r -s -p "ESP32 token (TOKEN in esp32_fan_agent/secrets.h, Enter = later): " ET; echo
    printf 'NVR_USER=%s\nNVR_PASSWORD=%s\nESP32_AGENT_TOKEN=%s\nPYTHONUNBUFFERED=1\n' "$NU" "$NP" "$ET" \
        | sudo tee "$ENVF" >/dev/null
    sudo chmod 600 "$ENVF"
    echo "secrets saved in $ENVF (edit: sudo nano $ENVF, then sudo systemctl restart cctv-rpi5)"
fi

# the earlier event_aware_aqi service also uses the HAT and the CPU: say so
if systemctl is-active --quiet event_aware_aqi 2>/dev/null; then
    echo "NOTE: event_aware_aqi (port 8090) is running on this Pi too. Both can share the AI HAT+ 2,"
    echo "      but CPU and decoding are shared. To stop it:  sudo systemctl disable --now event_aware_aqi"
fi

sed -e "s|@APP@|$APP|g" -e "s|@USER@|$(id -un)|g" deploy/rpi5/cctv-rpi5.service \
    | sudo tee /etc/systemd/system/cctv-rpi5.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now cctv-rpi5

LINE="40 2 * * * $APP/deploy/rpi5/backup.sh >> $APP/logs/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -vF "deploy/rpi5/backup.sh" || true; echo "$LINE" ) | crontab -

sleep 10
systemctl --no-pager --lines=15 status cctv-rpi5 || true
echo
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8001"
echo "Live log : journalctl -u cctv-rpi5 -f"
