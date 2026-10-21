#!/bin/bash
# CCTV_Rpi_5 - step 3 (on the Pi): Python environment of the app. No torch, no Ultralytics:
# the AI HAT+ 2 runs the .hef through HailoRT (apt's hailo_platform, seen through
# --system-site-packages). Safe to run again after every update.
set -euo pipefail
APP="${APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$APP"
[ -f run.py ] && [ -f config.yaml ] || { echo "STOP: $APP is not the app folder (run.py, config.yaml)"; exit 1; }
# the virtual environment "cctv_rpi" lives in this folder (cctv_rpi/); --system-site-packages lets it
# see apt's hailo_platform (AI HAT+ 2) and the Python libraries installed by 01_system.sh
if [ ! -x cctv_rpi/bin/python ]; then
    python3 -m venv --system-site-packages --prompt cctv_rpi cctv_rpi
    echo "virtual environment created: $APP/cctv_rpi"
fi
cctv_rpi/bin/pip install --upgrade pip
cctv_rpi/bin/pip install -r requirements.txt
cctv_rpi/bin/pip freeze > requirements.lock.txt
mkdir -p models logs
[ -e data ] || mkdir -p data

echo
echo "---- checks ----"
cctv_rpi/bin/python -c "import cv2, numpy, flask, yaml, requests; print('python packages :', 'ok, opencv', cv2.__version__)"
cctv_rpi/bin/python -c "import lap; print('lap             : ok')" || echo "lap             : missing (tracking needs it): cctv_rpi/bin/pip install lap"
cctv_rpi/bin/python -c "import hailo_platform; print('hailo_platform  :', hailo_platform.__version__)" \
    || echo "hailo_platform  : NOT importable - run 02_hailo.sh and reboot"
for f in models/yolo26s.hef models/yolo26n.hef; do
    [ -f "$f" ] && echo "model           : $f found" || echo "model           : $f MISSING (tools/hailo_compile/README.md)"
done
for f in roi.json door_line.json users.json; do
    [ -f "$f" ] && echo "settings        : $f found" || echo "settings        : $f missing - copy it from the PC (guide step 'Copy settings')"
done
cctv_rpi/bin/python -m pytest -q tests 2>&1 | tail -1
echo
echo "Use it:  cd $APP && source cctv_rpi/bin/activate     (prompt shows (cctv_rpi))"
echo "         python run.py                               (deactivate to leave)"
echo "Next: models (step 6), streams (step 7), first run by hand (step 8), then 04_service.sh"
