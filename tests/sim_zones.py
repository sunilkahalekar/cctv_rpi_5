"""
Synthetic lab with two camera zones at different distances from the sensor, for the
pollution-in-transit model: zone 1 next to the sensor (travel about 1.5 min), zone 2 at
the far end (travel about 9 min). Work sessions in zone 2 are bursty (sanding / cutting).
"""
import numpy as np

V = 300.0
R = 2.1 / 60          # removal per minute (air change + deposition), fan off
R_FAN = 6.6 / 60
CB = 25.0
DELAYS = {1: 1.5, 2: 9.0}


def kernel(mean_delay, n=30):
    """Discrete transport kernel: a gamma-like bump with the given mean delay, unit area."""
    t = np.arange(n) + 0.5
    k = 4.0
    theta = mean_delay / k
    h = t ** (k - 1) * np.exp(-t / theta)
    return h / h.sum()


def simulate(days=8, seed=4, fan_hours=()):
    rng = np.random.default_rng(seed)
    n = days * 1440
    hour = (np.arange(n) // 60) % 24
    zones = {}
    for z, base in ((1, 3), (2, 4)):
        pers = np.zeros(n)
        p = 0.0
        for t in range(n):
            if 9 <= hour[t] < 18:
                p = float(np.clip(p + rng.choice([-1, 0, 0, 0, 0, 1]), 0, 8))
                if hour[t] == 9 and p < base / 2:
                    p += 1
            else:
                p = max(0.0, p - 1)
            pers[t] = p
        motion = pers * rng.uniform(0.02, 0.06, n)
        # work sessions: bursts of vigorous activity lasting 5-25 minutes
        burst = np.zeros(n)
        t = 0
        while t < n:
            t += int(rng.integers(30, 120))
            if t < n and 9 <= hour[t] < 18 and pers[t] > 0:
                L = int(rng.integers(5, 25))
                burst[t:t + L] = rng.uniform(0.3, 0.8) * (2.0 if z == 2 else 0.6)
                t += L
        motion = motion + burst * (pers > 0)
        var = (burst * (pers > 0)) ** 2 * 0.5 + rng.uniform(0, 0.002, n) * (pers > 0)
        zones[z] = np.column_stack([pers, motion, var])
    fan = np.zeros(n)
    for h in fan_hours:
        fan[hour == h] = 1
    r = np.where(fan == 1, R_FAN, R)
    # emission per zone and arrival at the sensor through each zone's kernel
    arrive = np.zeros(n)
    for z, X in zones.items():
        e = 0.9 * X[:, 1] + 0.03 * X[:, 0] + 1.5 * X[:, 2]
        arrive += np.convolve(e, kernel(DELAYS[z]))[:n]
    c = np.zeros(n)
    c[0] = CB
    for t in range(1, n):
        c[t] = CB + (c[t - 1] - CB) * np.exp(-r[t - 1]) + arrive[t - 1] * (1 - np.exp(-r[t - 1])) / r[t - 1]
    return {"pm2_5": c + rng.normal(0, 0.3, n), "zones": zones, "fan": fan, "r": r, "arrive": arrive,
            "persons": zones[1][:, 0] + zones[2][:, 0]}
