"""
Synthetic lab days with a known mass balance, for checking the physics code:
occupancy and motion like the cameras report them, an exhaust fan that raises the air
change rate, and CO2 / PM2.5 that follow dC/dt = S - r (C - C_b) with sensor noise.
"""
import numpy as np

V = 300.0                       # m3
LAMBDA_OFF = 1.5 / 60           # per minute (1.5 air changes per hour)
LAMBDA_ON = 6.0 / 60            # exhaust fan on
K_DEP = 0.6 / 60                # particle deposition per minute
G_LPM = 0.30                    # CO2 per person, litres per minute
CO2_OUT = 420.0
PM_OUT = 25.0


def simulate(days=8, seed=1, fan_hours=None, lag_pm=6):
    rng = np.random.default_rng(seed)
    n = days * 24 * 60
    minute = np.arange(n)
    hour = (minute // 60) % 24
    day = minute // (24 * 60)
    persons = np.zeros(n)
    p = 0.0
    for t in range(n):
        if 9 <= hour[t] < 18 and day[t] % 7 < 6:
            p = float(np.clip(p + rng.choice([-1, 0, 0, 0, 1]), 0, 14))
            if hour[t] == 9 and p < 3:
                p += 1
        else:
            p = max(0.0, p - 1)
        persons[t] = p
    motion = persons * rng.uniform(0.03, 0.12, n)                 # total motion, BH/s
    motion[rng.random(n) < 0.02] *= 4                             # bursts of work
    fan = np.zeros(n)
    fan_hours = fan_hours if fan_hours is not None else {11, 15, 20}
    for h in fan_hours:
        fan[(hour == h)] = 1
    lam = np.where(fan == 1, LAMBDA_ON, LAMBDA_OFF)

    a = G_LPM / 1000.0 / V * 1e6                                    # ppm per minute per person
    co2 = np.zeros(n)
    pm = np.zeros(n)
    co2[0], pm[0] = CO2_OUT, PM_OUT
    src_pm = 0.8 * np.concatenate([np.zeros(lag_pm), motion[:-lag_pm]]) \
        + 0.05 * np.concatenate([np.zeros(lag_pm), persons[:-lag_pm]])
    for t in range(1, n):
        co2[t] = co2[t - 1] + a * persons[t - 1] - lam[t - 1] * (co2[t - 1] - CO2_OUT)
        pm[t] = pm[t - 1] + src_pm[t - 1] - (lam[t - 1] + K_DEP) * (pm[t - 1] - PM_OUT)
    co2_obs = co2 + rng.normal(0, 3.0, n)
    pm_obs = pm + rng.normal(0, 0.4, n)
    return {"persons": persons, "motion": motion, "fan": fan, "co2": co2_obs, "pm2_5": pm_obs,
            "lambda": lam, "a": a, "hour": hour, "day": day}
