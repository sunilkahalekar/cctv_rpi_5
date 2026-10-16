"""AI mode rules: persistence, forecasts, hysteresis, recovery, timers, stale data."""
from datetime import datetime, timedelta

from iaqcam.fan_policy import FanPolicy

T0 = datetime(2026, 10, 9, 10, 0, 0)
CFG = {"pollutants": {"pm10": {"start": 150, "stop": 100}, "co2": {"start": 1500, "stop": 1000}},
       "activation_seconds": 120, "forecast_trigger": True, "forecast_confirmations": 3,
       "forecast_requires_model": True, "recovery_seconds": 300, "min_on_seconds": 600,
       "min_off_seconds": 180, "stale_seconds": 300, "stale_action": "off"}


def at(sec):
    return T0 + timedelta(seconds=sec)


def readings(sec, pm10, co2=600):
    return {"pm10": (pm10, at(sec)), "co2": (co2, at(sec))}


def run(policy, series, fan_on=False, last_change=None, forecasts=None, step=15):
    """series: list of pm10 values, one per check. Returns the decisions."""
    out = []
    for i, v in enumerate(series):
        d = policy.decide(at(i * step), readings(i * step, v), (forecasts or (lambda i: {}))(i), fan_on, last_change)
        if d["on"] is not None and d["on"] != fan_on:
            fan_on, last_change = d["on"], at(i * step)
        out.append((i * step, d["on"], fan_on, d["reason"]))
    return out


def first_switch(out, state):
    return next((t for t, on, _, _ in out if on is state), None)


def test_single_spike_does_not_start():
    out = run(FanPolicy(CFG), [90, 200, 90, 90, 90, 90, 90, 90, 90, 90])
    assert first_switch(out, True) is None


def test_persistent_exceedance_starts_after_activation_time():
    out = run(FanPolicy(CFG), [90] + [160] * 12)
    assert first_switch(out, True) == 15 + 120             # above since 15 s, confirmed 120 s later
    assert "PM10" in out[9][3] or "pm10" in out[9][3]


def test_hysteresis_and_recovery_and_min_on():
    # on at 135 s; between stop and start it keeps running; below stop it needs 300 s and 600 s run time
    series = [90] + [160] * 10 + [120] * 20 + [80] * 40
    out = run(FanPolicy(CFG), series)
    on_t = first_switch(out, True)
    off_t = first_switch(out, False)
    assert on_t == 135
    below_from = 31 * 15
    assert off_t >= below_from + 300 and off_t - on_t >= 600


def test_all_pollutants_must_recover():
    p = FanPolicy(CFG)
    fan_on, lc = True, at(-3600)
    for i in range(60):
        d = p.decide(at(i * 15), {"pm10": (50, at(i * 15)), "co2": (1200, at(i * 15))}, {}, fan_on, lc)
        assert d["on"] is None, d["reason"]                 # CO2 still above its stop threshold
    assert "CO2" in d["reason"].upper()


def test_trusted_forecast_needs_confirmations():
    def fc(i):
        return {"pm10": (170.0, f"issue-{i}", True)}
    out = run(FanPolicy(CFG), [90] * 6, last_change=at(-3600), forecasts=fc)
    assert first_switch(out, True) == 2 * 15                 # third forecast in a row


def test_untrusted_forecast_ignored_and_min_off_respected():
    out = run(FanPolicy(CFG), [90] * 6, last_change=at(-3600), forecasts=lambda i: {"pm10": (170.0, f"i{i}", False)})
    assert first_switch(out, True) is None
    out = run(FanPolicy(CFG), [90] * 20, last_change=at(0), forecasts=lambda i: {"pm10": (170.0, f"i{i}", True)})
    assert first_switch(out, True) == 180                    # waits for min_off_seconds


def test_falling_forecast_does_not_stop_the_fan():
    p = FanPolicy(CFG)
    for i in range(10):
        d = p.decide(at(i * 15), readings(i * 15, 140), {"pm10": (20.0, f"i{i}", True)}, True, at(-3600))
        assert d["on"] is None


def test_stale_data_applies_fail_safe_action():
    p = FanPolicy(CFG)
    old = {"pm10": (300, at(-1000)), "co2": (600, at(-1000))}
    d = p.decide(at(0), old, {}, True, at(-60))
    assert d["on"] is False and d["stale"]                    # the lab's choice: off
    d = FanPolicy({**CFG, "stale_action": "on"}).decide(at(0), old, {}, False, None)
    assert d["on"] is True
    d = FanPolicy({**CFG, "stale_action": "hold"}).decide(at(0), {}, {}, True, None)
    assert d["on"] is None
