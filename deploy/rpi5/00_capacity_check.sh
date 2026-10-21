#!/bin/bash
# cctv_rpi_5 - step 0 (on the Pi, BEFORE installing): can this Pi take the 10-camera system next to
# what already runs on it (e.g. event_aware_aqi, one camera + YOLO on the AI HAT+ 2)?
# Changes nothing. Measures for 60 s (change with: bash 00_capacity_check.sh 120).
SECS="${1:-60}"
line() { printf '%-22s %s\n' "$1" "$2"; }
echo "==== this Pi"
line "model"        "$(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
line "OS"           "$(. /etc/os-release; echo "$PRETTY_NAME")  (Trixie needed for hailo-h10-all)"
line "kernel / arch" "$(uname -r) / $(uname -m)"
line "python"       "$(python3 --version 2>&1)"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
line "RAM total"    "${MEM_MB} MB"
line "swap"         "$(free -m | awk '/Swap/ {print $2" MB total, "$3" MB used"}')"
line "power/heat"   "$(vcgencmd get_throttled 2>/dev/null)  $(vcgencmd measure_temp 2>/dev/null)   (want throttled=0x0)"
line "IP address"   "$(hostname -I)"

echo; echo "==== what runs now"
for s in event_aware_aqi cctv-rpi5 hailort; do
    st=$(systemctl is-active "$s" 2>/dev/null); [ "$st" = "inactive" ] && [ "$s" != "event_aware_aqi" ] && continue
    mem=$(systemctl show "$s" -p MemoryCurrent --value 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {printf "%.0f MB", $1/1048576}')
    line "service $s" "$st ${mem}"
done
echo "ports in use (8001 = this system, 8090 = event_aware_aqi):"
ss -ltnp 2>/dev/null | awk 'NR>1 {print "   " $4}' | grep -E ":(8001|8090|8095|22)$" || echo "   (none of 8001/8090)"
ss -ltn 2>/dev/null | grep -q ":8001 " && echo "   !! port 8001 is taken: change dashboard.port in config.yaml"
echo "top programs by memory:"
ps -eo rss,pcpu,comm --sort=-rss | head -8 | awk 'NR==1 {print "   MB     %CPU  program"; next} {printf "   %-6d %-5s %s\n", $1/1024, $2, $3}'

echo; echo "==== AI HAT+ 2"
lspci 2>/dev/null | grep -i hailo || echo "   NO Hailo device on PCIe"
hailortcli fw-control identify 2>&1 | grep -E "Device Architecture|Firmware Version" | sed 's/^/   /'
line "HailoRT" "$(hailortcli --version 2>/dev/null | tr '\n' ' ')  (5.3+ for YOLO26)"
users=$(sudo fuser /dev/hailo0 2>/dev/null | wc -w)
line "programs using it" "${users}  $(for p in $(sudo fuser /dev/hailo0 2>/dev/null); do ps -o comm= -p "$p"; done | tr '\n' ' ')"
line "multi-process svc" "$(systemctl is-active hailort 2>/dev/null) (needed when two programs share the HAT)"

echo; echo "==== disk"
df -h / /mnt/usbdata 2>/dev/null | sed 's/^/   /'
mountpoint -q /mnt/usbdata && DATA_FREE_GB=$(df -BG --output=avail /mnt/usbdata | tail -1 | tr -dc 0-9) \
                           || DATA_FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
mountpoint -q /mnt/usbdata || echo "   !! no USB drive at /mnt/usbdata: the data would go to the SD card (not recommended)"

echo; echo "==== load over ${SECS} s (the existing system as it runs now)"
read -r _ u n s i w x y z _ < /proc/stat; T0=$((u+n+s+i+w+x+y+z)); I0=$((i+w))
MIN_AVAIL=999999; MAXT=0
for ((k=0; k<SECS; k+=5)); do
    sleep 5
    a=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo); [ "$a" -lt "$MIN_AVAIL" ] && MIN_AVAIL=$a
    t=$(awk '{print int($1/1000)}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null); t=${t:-0}; [ "$t" -gt "$MAXT" ] && MAXT=$t
done
read -r _ u n s i w x y z _ < /proc/stat; T1=$((u+n+s+i+w+x+y+z)); I1=$((i+w))
CPU=$(( 100 * ( (T1-T0) - (I1-I0) ) / (T1-T0) ))
line "CPU used (4 cores)" "${CPU} %"
line "RAM available (min)" "${MIN_AVAIL} MB"
line "temperature (max)" "${MAXT} C"

echo; echo "==== verdict for adding cctv_rpi_5 (10 cameras, sub streams)"
# estimates for the new system: ~1.2 GB RAM (app + 10 ffmpeg decoders), 35-60 % CPU (decoding H.264
# sub streams, optical flow, dashboard); confirmed later by tools/stream_probe.py --load 60
NEED_MB=1500; NEED_CPU=45; ok=1
if [ "$MIN_AVAIL" -lt "$NEED_MB" ]; then ok=0; echo "  RAM : only ${MIN_AVAIL} MB free, the new system needs ~${NEED_MB} MB -> stop event_aware_aqi, or use an 8 GB Pi"
else echo "  RAM : OK (${MIN_AVAIL} MB free, ~${NEED_MB} MB needed)"; fi
if [ "$CPU" -gt $((100-NEED_CPU)) ]; then ok=0; echo "  CPU : ${CPU} % already used, the new system needs ~${NEED_CPU} % -> stop event_aware_aqi or lower its camera rate"
else echo "  CPU : OK (${CPU} % used now, ~${NEED_CPU} % more needed)"; fi
if [ "$MAXT" -ge 75 ]; then ok=0; echo "  HEAT: ${MAXT} C already -> improve cooling first (Active Cooler, open case)"; else echo "  HEAT: OK (${MAXT} C)"; fi
if [ "$users" -gt 0 ]; then echo "  HAT : in use by another program -> both programs need multi_process_service (guide section 0, step 0.4), or stop the other one"; fi
# storage of the detections (measured on the PC app's database, 10 cameras, busy weekday, scaled from
# ~8 to 60 pictures per camera per minute):
#   database: per-second rows ~330 MB per busy day, kept 7 days -> ~2 GB; minute rows ~3.5 MB/day -> ~1.3 GB/year
#   archive : per-second rows as csv.gz ~70 MB per busy day -> ~18 GB/year;  backups: 7 x ~3 GB = ~21 GB
NEED_GB=45
if [ "${DATA_FREE_GB:-0}" -lt "$NEED_GB" ]; then ok=0; echo "  DISK: ${DATA_FREE_GB} GB free on the data drive; plan ~${NEED_GB} GB for the first year (database 3 + backups 21 + archive 18)"
else echo "  DISK: OK (${DATA_FREE_GB} GB free, ~${NEED_GB} GB needed in the first year)"; fi
[ "$ok" = 1 ] && echo "  => both systems can run together (confirm with stream_probe --load 60)" \
              || echo "  => fix the lines above first (or choose B/C in guide section 0.4)"
