"""Keep every detection for the long term, without letting the database grow without end.

The database keeps the per-second tables (camera_second: one row per camera per analysed
picture; person_obs: one row per tracked person per picture, with box, confidence, motion)
for pipeline.keep_seconds_days (7). Every night, before those rows are deleted, this writes
each finished day to compressed CSV files on the USB drive:

    /mnt/usbdata/cctv/archive/2026-10-21/camera_second.csv.gz
    /mnt/usbdata/cctv/archive/2026-10-21/person_obs.csv.gz

Minute tables (camera_minute, zone_person_minute, door, sensors, forecasts, physics) stay in
the database for ever and are small (a few MB per day).

    .venv/bin/python tools/archive_seconds.py                 # all finished days not archived yet
    .venv/bin/python tools/archive_seconds.py --day 2026-10-21
Runs every night from deploy/rpi5/backup.sh. Read with pandas:  pd.read_csv("person_obs.csv.gz")
"""
import argparse
import csv
import gzip
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

TABLES = ("camera_second", "person_obs")


def days_in_db(con):
    rows = con.execute("SELECT DISTINCT substr(Timestamp, 1, 10) FROM camera_second "
                       "UNION SELECT DISTINCT substr(Timestamp, 1, 10) FROM person_obs").fetchall()
    return sorted(r[0] for r in rows if r[0])


def archive_day(con, day, out_dir):
    """Write one day of both tables; returns {table: rows}. Files appear only when complete."""
    folder = Path(out_dir) / day
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    for table in TABLES:
        target = folder / f"{table}.csv.gz"
        if target.exists():
            counts[table] = None
            continue
        cur = con.execute(f"SELECT * FROM {table} WHERE Timestamp >= ? AND Timestamp < ? ORDER BY Timestamp",
                          (day, nxt))
        tmp = target.with_suffix(".tmp")
        n = 0
        with gzip.open(tmp, "wt", newline="", encoding="utf-8", compresslevel=6) as fh:
            w = csv.writer(fh)
            w.writerow([d[0] for d in cur.description])
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                w.writerows(rows)
                n += len(rows)
        os.replace(tmp, target)
        counts[table] = n
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "data" / "iaq_cctv.db"))
    ap.add_argument("--out", default="/mnt/usbdata/cctv/archive")
    ap.add_argument("--day", help="YYYY-MM-DD (default: every finished day not yet archived)")
    args = ap.parse_args()
    if not Path(args.db).is_file():
        sys.exit(f"database not found: {args.db}")
    out = Path(args.out)
    if str(out).startswith("/mnt/usbdata") and not os.path.ismount("/mnt/usbdata"):
        out = Path(args.db).parent / "archive"            # no USB drive: next to the database
    con = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    today = date.today().isoformat()
    days = [args.day] if args.day else [d for d in days_in_db(con) if d < today]
    for day in days:
        counts = archive_day(con, day, out)
        done = ", ".join(f"{t} {'already archived' if n is None else f'{n} rows'}" for t, n in counts.items())
        print(f"{day}: {done}")
    size = sum(f.stat().st_size for f in out.rglob("*.csv.gz")) / 1e6 if out.exists() else 0
    print(f"archive: {out}  ({size:.0f} MB in total)")


if __name__ == "__main__":
    main()
