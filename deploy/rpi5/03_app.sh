#!/bin/bash
# CCTV_Rpi_5 - step 3 (on the Pi): Python environment of the app. No torch, no Ultralytics:
# the AI HAT+ 2 runs the .hef through HailoRT (apt's hailo_platform, seen through
# --system-site-packages). Safe to run again after every update.
set -euo pipefail
APP="${APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$APP"
[ -f run.py ] && [ -f config.yaml ] || { echo "STOP: $APP is not the app folder (run.py, config.yaml)"; exit 1; }
if [ ! -x .venv/bin/python ]; then
    python3 -m venv --system-site-packages .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip freeze > requirements.lock.txt
mkdir -p models logs
[ -e data ] || mkdir -p data

echo
echo "---- checks ----"
.venv/bin/python -c "import cv2, numpy, flask, yaml, requests; print('python packages :', 'ok, opencv', cv2.__version__)"
.venv/bin/python -c "import lap; print('lap             : ok')" || echo "lap             : missing (tracking needs it): .venv/bin/pip install lap"
.venv/bin/python -c "import hailo_platform; print('hailo_platform  :', hailo_platform.__version__)" \
    || echo "hailo_platform  : NOT importable - run 02_hailo.sh and reboot"
for f in models/yolo26s.hef models/yolo26n.hef; do
    [ -f "$f" ] && echo "model           : $f found" || echo "model           : $f MISSING (tools/hailo_compile/README.md)"
done
for f in roi.json door_line.json users.json; do
    [ -f "$f" ] && echo "settings        : $f found" || echo "settings        : $f missing - copy it from the PC (guide step 'Copy settings')"
done
.venv/bin/python -m pytest -q tests 2>&1 | tail -1
echo "Next: models (step 6), streams (step 7), first run by hand (step 8), then 04_service.sh"
