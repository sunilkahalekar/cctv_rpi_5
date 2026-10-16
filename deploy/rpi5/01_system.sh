#!/bin/bash
# CCTV_Rpi_5 - step 1 (on the Pi, once): base packages, ffmpeg, log cap, watchdog, clock, data drive.
# Before it:  sudo apt update && sudo apt full-upgrade -y && sudo reboot
# OS: Raspberry Pi OS (64-bit) Trixie - needed by the AI HAT+ 2 packages. Safe to run again.
set -euo pipefail
APP="${APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

sudo apt update
sudo apt install -y git python3-venv python3-dev build-essential ffmpeg sqlite3 htop curl jq
# Python libraries from apt: built for this OS and the same numpy that hailo_platform uses
sudo apt install -y python3-numpy python3-opencv python3-flask python3-yaml python3-requests

# journal never larger than 200 MB (protects the SD card)
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/size.conf >/dev/null
sudo systemctl restart systemd-journald
# hardware watchdog: reboot if the kernel hangs
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15s\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null
sudo systemctl daemon-reexec
sudo timedatectl set-ntp true
sudo timedatectl set-timezone Asia/Kolkata

# data folder: on the USB drive when it is mounted at /mnt/usbdata (as on the earlier Pi 5 setup),
# else on the SD card. The app always writes to $APP/data.
if mountpoint -q /mnt/usbdata; then
    sudo mkdir -p /mnt/usbdata/cctv /mnt/usbdata/backups/cctv
    sudo chown -R "$(id -un)":"$(id -gn)" /mnt/usbdata/cctv /mnt/usbdata/backups/cctv
    if [ ! -e "$APP/data" ]; then ln -s /mnt/usbdata/cctv "$APP/data"; fi
    echo "data folder  : $APP/data -> /mnt/usbdata/cctv (USB drive)"
else
    mkdir -p "$APP/data"
    echo "data folder  : $APP/data on the SD card. For a USB SSD/drive: mount it at /mnt/usbdata"
    echo "               (fstab with nofail, guide step 2), delete the empty data folder, run this again."
fi

echo
echo "---- checks ----"
echo "architecture : $(uname -m)   (must be aarch64)"
echo "OS           : $(. /etc/os-release; echo "$PRETTY_NAME")   (Trixie needed for hailo-h10-all)"
echo "memory       : $(free -h | awk '/Mem:/ {print $2}')"
echo "power/heat   : $(vcgencmd get_throttled)   (must be throttled=0x0)"
echo "temperature  : $(vcgencmd measure_temp)"
echo "ffmpeg       : $(ffmpeg -hide_banner -hwaccels 2>/dev/null | tr '\n' ' ')"
timedatectl | grep -E "Time zone|synchronized"
echo "Next: bash deploy/rpi5/02_hailo.sh"
