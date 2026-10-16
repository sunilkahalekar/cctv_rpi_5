#!/bin/bash
# CCTV_Rpi_5 - step 2 (on the Pi, once): AI HAT+ 2 driver, firmware, HailoRT and its Python module.
# Do NOT install hailo-all: that is for the older AI Kit / AI HAT+ and cannot sit beside hailo-h10-all.
set -euo pipefail
if dpkg -s hailo-all >/dev/null 2>&1; then
    echo "STOP: the package hailo-all is installed. Remove it first:  sudo apt remove hailo-all"
    exit 1
fi
sudo apt update
sudo apt install -y dkms
sudo apt install -y hailo-h10-all
echo
echo "Installed. Now:  sudo reboot"
echo "After the reboot check (all must pass):"
echo "  lspci | grep -i hailo"
echo "  hailortcli fw-control identify          # HAILO10H"
echo "  hailortcli --version                    # 5.3 or newer for YOLO26 - write it down for compiling"
echo "  python3 -c 'import hailo_platform; print(hailo_platform.__version__)'"
echo "Then: bash deploy/rpi5/03_app.sh"
