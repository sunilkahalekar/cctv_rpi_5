"""
Paper figures from the output of greybox_eval.py.

    python research/greybox_eval.py --out research_out        # first: the evaluation
    python research/make_figures.py research_out              # then: the figures

Writes research_out/figures/figN_*.png (300 dpi) and .pdf (vector, for the journal):
  fig1_typical_day        pollutants, persons in the areas, motion and fan over one busy day
  fig2_decays             empty-room decays: ln(C - C_b) against time, fan off vs on (tracer-gas method)
  fig3_source_vs_persons  source the sensor must have received vs persons seen (CO2: L/min per person)
  fig4_skill              forecast skill vs "no change" per model and test split (rung)
  fig5_transit_kernels    pollution in transit: how long each camera zone takes to reach the sensor
  fig6_warning_case       one worked early warning: actual sensor vs the forecast paths
  fig7_lead_times         early warning: warned in time, false alarms, median lead per method
Each figure is skipped (with a note) when its input is missing, e.g. no fan minutes yet.
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from iaqcam import physics as P  # noqa: E402

# categorical order (fixed, never cycled); text in ink colours, not series colours
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
INK, INK2, GRID = "#1f1f1e", "#5f5e58", "#e4e3de"
LABEL = {"co2": "CO₂ (ppm)", "pm2_5": "PM2.5 (µg/m³)", "pm10": "PM10 (µg/m³)", "pm1": "PM1 (µg/m³)",
         "voc": "VOC index", "temperature": "Temperature (°C)", "humidity": "RH (%)"}
MODEL = {"persistence": "No change", "physics": "Physics only", "ridge": "Data only (ridge)",
         "ridge_greybox": "Grey-box (ridge)", "ridge_pinn": "Physics-penalised ridge",
         "transit": "Pollution in transit", "bilstm": "Data only (BiLSTM)",
         "bilstm_greybox": "Grey-box (BiLSTM)", "bilstm_pinn": "PINN (BiLSTM)", "film_greybox": "Grey-box + FiLM"}
RUNG = {"rung0": "Chronological", "rung1": "Leave one day out", "rung2": "Fan never seen in training",
        "rung4": "Forward chaining"}

plt.rcParams.update({"font.size": 9, "axes.edgecolor": INK2, "axes.labelcolor": INK, "xtick.color": INK2,
                     "ytick.color": INK2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
                     "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
                     "font.family": "DejaVu Sans", "lines.linewidth": 1.6, "savefig.bbox": "tight",
                     "axes.axisbelow": True})


def save(fig, out, name):
    for ext in ("png", "pdf"):
        fig.savefig(out / f"{name}.{ext}", dpi=300)
    plt.close(fig)
    print("written", name)


def load_dataset(path):
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return None
    d = {"time": [datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S") for r in rows]}
    for k in rows[0]:
        if k != "time":
            d[k] = np.array([float(r[k]) if r[k] not in ("", "None") else np.nan for r in rows])
    return d


def pollutants(d):
    return [k for k in ("co2", "pm2_5", "pm10", "pm1", "voc") if k in d and np.isfinite(d[k]).sum() > 100]


# ---------------------------------------------------------------- figures
def fig1(d, out):
    pols = pollutants(d)[:2]
    if not pols or "persons" not in d:
        return print("skip fig1: no pollutant or persons")
    day = np.array([t.date() for t in d["time"]])
    days = sorted(set(day))
    busiest = max(days, key=lambda x: np.nansum(d["persons"][day == x]))
    m = day == busiest
    t = [x for x, k in zip(d["time"], m) if k]
    hrs = np.array([x.hour + x.minute / 60 for x in t])
    n = len(pols) + 2
    fig, ax = plt.subplots(n, 1, figsize=(7, 1.5 * n), sharex=True)
    fan = d.get("fan", np.zeros(len(m)))[m]
    for i, k in enumerate(pols):
        ax[i].plot(hrs, d[k][m], color=C[i])
        ax[i].set_ylabel(LABEL.get(k, k))
    ax[-2].step(hrs, d["persons"][m], color=C[2], where="post")
    ax[-2].set_ylabel("Persons\nin areas")
    ax[-1].plot(hrs, d["motion"][m], color=C[3])
    ax[-1].set_ylabel("Motion")
    for a in ax:
        on = np.flatnonzero(np.diff(np.concatenate([[0], fan > 0, [0]]).astype(int)))
        for s, e in zip(on[::2], on[1::2]):
            a.axvspan(hrs[s], hrs[min(e, len(hrs) - 1)], color=GRID, zorder=0, lw=0)
    ax[-1].set_xlim(0, 24)
    ax[-1].set_xticks(range(0, 25, 3))
    ax[-1].set_xlabel(f"Hour of day ({busiest}); grey bands = exhaust fan on")
    ax[0].set_title("A typical working day in MS 101", loc="left", color=INK)
    save(fig, out, "fig1_typical_day")


def fig2(d, out, phys):
    pols = [k for k in pollutants(d) if k in ("co2", "pm2_5")]
    if not pols or "persons" not in d:
        return print("skip fig2")
    fig, axes = plt.subplots(1, len(pols), figsize=(3.6 * len(pols), 3), squeeze=False)
    for ax, k in zip(axes[0], pols):
        y = d[k]
        cb = P.background(P.smooth(y, 5), k)
        segs = P.decay_segments(y, d["persons"], cb)
        fan = d.get("fan", np.zeros(len(y)))
        shown = {0: 0, 1: 0}
        for s in segs:
            i, j = s[0], s[1]
            f = int(np.nanmean(fan[i:j]) > 0.5)
            ex = y[i:j] - cb
            ok = ex > 0
            if ok.sum() < 5 or shown[f] >= 8:
                continue
            tt = np.arange(j - i)[ok]
            ax.plot(tt, np.log(ex[ok]) - np.log(ex[ok][0]), color=C[f], lw=1, alpha=0.7,
                    label=("fan on" if f else "fan off") if shown[f] == 0 else None)
            shown[f] += 1
        info = (phys or {}).get(k, {})
        for f, key in ((0, "removal_per_hour_fan_off"), (1, "removal_per_hour_fan_on")):
            r = info.get(key)
            if r:
                tt = np.linspace(0, 60, 50)
                ax.plot(tt, -r / 60 * tt, color=C[f], lw=2.4, ls="--")
                ax.text(tt[-1], -r, f" {r:.1f} /h", color=INK, va="center", fontsize=8)
        ax.set_xlim(0, 75)
        ax.set_xlabel("Minutes after the room emptied")
        ax.set_ylabel("ln[(C − C_b) / (C₀ − C_b)]")
        ax.set_title(LABEL.get(k, k).split(" (")[0], loc="left", color=INK)
        ax.legend(loc="lower left")
    fig.suptitle("Removal rate from empty-room decays (slope = air change + deposition)", x=0.01, y=1.06,
                 ha="left", color=INK, fontsize=9)
    save(fig, out, "fig2_decays")


def fig3(d, out, phys):
    pols = [k for k in pollutants(d) if k in ("co2", "pm2_5")]
    if not pols or "persons" not in d:
        return print("skip fig3")
    fig, axes = plt.subplots(1, len(pols), figsize=(3.6 * len(pols), 3), squeeze=False)
    fan = d.get("fan", np.zeros(len(d["persons"])))
    for ax, k in zip(axes[0], pols):
        info = (phys or {}).get(k, {})
        y = d[k]
        cb = info.get("background") or P.background(P.smooth(y, 5), k)
        r_off = (info.get("removal_per_hour_fan_off") or 1.0) / 60
        r_on = (info.get("removal_per_hour_fan_on") or info.get("removal_per_hour_fan_off") or 1.0) / 60
        r = np.where(fan > 0, r_on, r_off)
        s = P.observed_source(y, cb, r)
        s = np.convolve(np.nan_to_num(s), np.ones(15) / 15, "same")        # 15-min mean, per-minute is noisy
        p = d["persons"]
        ks = np.arange(0, int(np.nanmax(p)) + 1)
        mean = [np.nanmedian(s[p == q]) if (p == q).sum() > 20 else np.nan for q in ks]
        lo = [np.nanpercentile(s[p == q], 25) if (p == q).sum() > 20 else np.nan for q in ks]
        hi = [np.nanpercentile(s[p == q], 75) if (p == q).sum() > 20 else np.nan for q in ks]
        ax.errorbar(ks, mean, yerr=[np.subtract(mean, lo), np.subtract(hi, mean)], fmt="o", color=C[0],
                    ms=5, capsize=0, elinewidth=1.2)
        good = np.isfinite(mean)
        if good.sum() >= 2:
            a, b = np.polyfit(ks[good], np.array(mean)[good], 1)
            ax.plot(ks, a * ks + b, color=C[1], ls="--")
            V = info.get("volume_m3") or 300
            note = (f"slope {a:.2f} ppm/min per person\n= {a * 1e-6 * V * 1000:.2f} L/min per person (V = {V:.0f} m³)"
                    if k == "co2" else f"slope {a:.3f} µg/m³/min per person\n= {a * V:.1f} µg/min per person")
            ax.text(0.02, 0.97, note, transform=ax.transAxes, va="top", fontsize=8, color=INK)
        ax.set_xlabel("Persons seen by the cameras (inside the areas)")
        ax.set_ylabel("Source S per minute (median, IQR)")
        ax.set_title(LABEL.get(k, k).split(" (")[0], loc="left", color=INK)
    save(fig, out, "fig3_source_vs_persons")


def fig4(metrics_path, out):
    if not metrics_path.exists():
        return print("skip fig4: no metrics.csv")
    with open(metrics_path) as fh:
        rows = [r for r in csv.DictReader(fh) if r["skill_vs_persistence"] not in ("", "None")]
    fields = list(dict.fromkeys(r["field"] for r in rows))
    if not fields:
        return print("skip fig4: empty")
    models = [m for m in MODEL if m != "persistence" and any(r["model"] == m for r in rows)]
    rungs = [g for g in RUNG if any(r["rung"] == g for r in rows)]
    fig, axes = plt.subplots(len(fields), 1, figsize=(7.2, 2.4 * len(fields) + 0.8), squeeze=False, sharex=True)
    w = 0.8 / max(1, len(rungs))
    for ax, f in zip(axes[:, 0], fields):
        for j, g in enumerate(rungs):
            vals = []
            for m in models:
                sk = [float(r["skill_vs_persistence"]) for r in rows if r["field"] == f and r["rung"] == g and r["model"] == m]
                vals.append(np.mean(sk) if sk else np.nan)
            x = np.arange(len(models)) + (j - (len(rungs) - 1) / 2) * w
            ax.bar(x, vals, width=w * 0.92, color=C[j], label=RUNG[g])
        ax.axhline(0, color=INK2, lw=0.8)
        ax.set_xticks(np.arange(len(models)))
        ax.set_xticklabels([MODEL[m] for m in models], rotation=20, ha="right")
        ax.grid(axis="x", visible=False)
        ax.set_ylabel("Skill vs no change")
        ax.set_title(LABEL.get(f, f).split(" (")[0], loc="left", color=INK)
        ax.set_ylim(min(-0.3, np.nanmin(ax.get_ylim()[0])), 1)
    axes[0, 0].legend(ncol=len(rungs), loc="upper left", bbox_to_anchor=(0, 1.32), fontsize=8)
    fig.text(0.01, -0.12 / len(fields), "Skill = 1 − MAE / MAE of 'no change' (higher is better; below 0 = worse than doing nothing).",
             fontsize=8, color=INK2)
    save(fig, out, "fig4_skill")


def fig5(phys, out):
    tr = [(k, v["transit"]) for k, v in (phys or {}).items() if isinstance(v, dict) and v.get("transit")]
    if not tr:
        return print("skip fig5: no transit model (needs camera zones)")
    k, t = tr[0]
    from iaqcam.transit import BLOCKS
    labels = [f"{a}–{b}" for a, b in BLOCKS]
    fig, ax = plt.subplots(figsize=(5.2, 3))
    cams = sorted(t["cameras"], key=lambda c: -c["share"])[:6]
    w = 0.8 / max(1, len(cams))
    for i, c in enumerate(cams):
        kern = np.array(c["kernel"], float)
        kern = kern / kern.sum() if kern.sum() > 0 else kern
        x = np.arange(len(BLOCKS)) + (i - (len(cams) - 1) / 2) * w
        tm = f", travel ≈ {c['travel_min']} min" if c.get("travel_min") is not None else ""
        ax.bar(x, kern, width=w * 0.92, color=C[i], label=f"{c['name']} ({c['share'] * 100:.0f} % of source{tm})")
    ax.set_xticks(np.arange(len(BLOCKS)))
    ax.set_xticklabels(labels)
    ax.set_xlabel("Activity this many minutes ago")
    ax.set_ylabel("Share of the camera's effect")
    ax.set_title(f"Pollution in transit: {LABEL.get(k, k).split(' (')[0]} (R² = {t['r2']})", loc="left", color=INK)
    ax.legend(fontsize=7.5, loc="upper right")
    save(fig, out, "fig5_transit_kernels")


def fig6(path, out):
    if not path.exists():
        return print("skip fig6: no transit_example.json")
    ex = json.loads(path.read_text())[0]
    act = np.array([np.nan if v is None else v for v in ex["actual"]], float)
    t = np.arange(-30, 31)
    fig, ax = plt.subplots(figsize=(6.2, 3.2))
    ax.plot(t, act, color=INK, lw=2, label="Sensor (what really happened)")
    order = ["in transit: committed (certain)", "in transit: expected", "in transit: activity held",
             "well-mixed physics", "sensor trend"]
    names = {"in transit: committed (certain)": "Committed (already emitted)", "in transit: expected": "Expected",
             "in transit: activity held": "Activity continues", "well-mixed physics": "Well-mixed physics",
             "sensor trend": "Sensor trend"}
    for i, k in enumerate([k for k in order if k in ex["paths"]]):
        p = np.array([np.nan if v is None else v for v in ex["paths"][k]], float)
        ax.plot(np.arange(1, len(p) + 1), p, color=C[i], ls="-" if i < 3 else "--", lw=1.4, label=names[k])
    ax.axhline(ex["threshold"], color=C[1], lw=0.8, ls=":")
    ax.text(-29, ex["threshold"], " fan start threshold", va="bottom", fontsize=8, color=INK2)
    ax.axvline(0, color=INK2, lw=0.8)
    ax.text(0.5, ax.get_ylim()[0], " forecast made here", fontsize=8, color=INK2, va="bottom")
    ax.set_xlabel("Minutes from the forecast")
    ax.set_ylabel(LABEL.get(ex["field"], ex["field"]))
    ax.set_title("One early warning, step by step", loc="left", color=INK)
    ax.legend(fontsize=7.5, loc="upper left")
    save(fig, out, "fig6_warning_case")


def fig7(path, out):
    if not path.exists():
        return print("skip fig7: no lead_times.csv")
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return print("skip fig7: empty")
    f = rows[0]["field"]
    rows = [r for r in rows if r["field"] == f]
    names = [r["method"].replace("in transit: ", "") for r in rows]
    y = np.arange(len(rows))[::-1]
    fig, ax = plt.subplots(1, 3, figsize=(7.6, 0.45 * len(rows) + 1.2), sharey=True)
    cross = [int(r["crossings"]) for r in rows]
    vals = [[int(r["warned_in_time"]) / max(1, c) * 100 for r, c in zip(rows, cross)],
            [int(r["false_alarms"]) for r in rows],
            [float(r["median_lead_min"]) if r["median_lead_min"] not in ("", "None") else 0 for r in rows]]
    titles = ["Warned in time (%)", "False alarms (count)", "Median lead (min)"]
    for i, (a, v, ttl) in enumerate(zip(ax, vals, titles)):
        a.barh(y, v, color=C[0], height=0.6)
        for yy, vv in zip(y, v):
            a.text(vv, yy, f" {vv:.0f}", va="center", fontsize=8, color=INK)
        a.set_title(ttl, loc="left", color=INK, fontsize=9)
        a.grid(axis="y", visible=False)
        a.set_xlim(0, max(v) * 1.25 + 1e-9)
    ax[0].set_yticks(y)
    ax[0].set_yticklabels(names)
    fig.suptitle(f"Early warning on {LABEL.get(f, f).split(' (')[0]}: {cross[0]} threshold crossings, "
                 f"{rows[0]['test_days']} test days", x=0.01, ha="left", color=INK, fontsize=9)
    save(fig, out, "fig7_lead_times")


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "research_out")
    out = src / "figures"
    out.mkdir(parents=True, exist_ok=True)
    d = load_dataset(src / "dataset.csv") if (src / "dataset.csv").exists() else None
    phys = json.loads((src / "physics.json").read_text()) if (src / "physics.json").exists() else {}
    if d is not None:
        fig1(d, out)
        fig2(d, out, phys)
        fig3(d, out, phys)
    else:
        print("skip fig1-3: no dataset.csv")
    fig4(src / "metrics.csv", out)
    fig5(phys, out)
    fig6(src / "transit_example.json", out)
    fig7(src / "lead_times.csv", out)
    print(f"Figures in {out}")


if __name__ == "__main__":
    main()
