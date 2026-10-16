"""
Report machine / enclosure states to the lab app (E_mach of the mass balance).

Run it on any computer on the lab network that knows a machine's state (the laser PC, a
Raspberry Pi reading a smart plug or a lid switch). Standard library only.

    set MACHINE_AGENT_TOKEN=long-random-text        (the same value on the camera PC)
    python report_machine.py --app http://10.185.101.216:8001 --machine laser1 --on --power 850
    python report_machine.py --app http://10.185.101.216:8001 --machine laser1_lid --kind enclosure --off

Repeat it when the state changes (or every minute from a loop / scheduled task): the app
keeps the last reported state for up to 12 hours. Once enough minutes are recorded, the
forecast uses "machines on" as a source of the mass balance and the Physics page shows
the machines as live.
"""
import argparse
import json
import os
import sys
import urllib.request


def main():
    ap = argparse.ArgumentParser(description="report a machine state to the lab app")
    ap.add_argument("--app", required=True, help="dashboard address, e.g. http://10.185.101.216:8001")
    ap.add_argument("--machine", required=True, help="name, e.g. laser1, printer2, laser1_lid")
    ap.add_argument("--kind", default="machine", choices=("machine", "enclosure", "extractor", "other"))
    state = ap.add_mutually_exclusive_group(required=True)
    state.add_argument("--on", action="store_true", help="running / open")
    state.add_argument("--off", action="store_true", help="off / closed")
    ap.add_argument("--power", type=float, help="electrical power in W, if measured")
    a = ap.parse_args()
    token = os.environ.get("MACHINE_AGENT_TOKEN", "")
    if not token:
        sys.exit("Set MACHINE_AGENT_TOKEN first (the same value as on the camera PC).")
    body = {"machines": [{"machine": a.machine, "kind": a.kind, "on": bool(a.on), "power_w": a.power}]}
    req = urllib.request.Request(a.app.rstrip("/") + "/api/machines", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(r.read().decode())
    except Exception as exc:                   # network / refused / wrong token
        sys.exit(f"Not stored: {exc}")


if __name__ == "__main__":
    main()
