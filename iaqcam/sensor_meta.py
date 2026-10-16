"""
Display names, units and reference bands for the sensor fields, matched by the
field name on the ThingSpeak channel (pm1, pm2_5, pm10, co2, voc, temp, hum).

The bands are the ones used by the dashboards of iaq-edge-pipeline: PM bands
follow US EPA AQI breakpoints, CO2 bands follow commonly cited indoor guidance,
VOC uses the SGP40 index scale. They colour the page; they are not a certified
safety limit.

Levels: 0 good, 1 moderate, 2 serious, 3 critical.
"""

META = {
    "pm1":    {"label": "PM1",         "unit": "µg/m³", "digits": 1},
    "pm2_5":  {"label": "PM2.5",       "unit": "µg/m³", "digits": 1},
    "pm10":   {"label": "PM10",        "unit": "µg/m³", "digits": 1},
    "co2":    {"label": "CO₂",         "unit": "ppm",   "digits": 0},
    "voc":    {"label": "VOC",         "unit": "index", "digits": 0},
    "temp":   {"label": "Temperature", "unit": "°C",    "digits": 1},
    "hum":    {"label": "Humidity",    "unit": "% RH",  "digits": 0},
    "rawvoc": {"label": "Raw VOC",     "unit": "raw",   "digits": 0},
}
POLLUTANTS = ("pm1", "pm2_5", "pm10", "co2", "voc")      # shown as tiles, in this order

# (upper bound, label, level), ascending
_PM_FINE = [(12, "Good", 0), (35, "Moderate", 1), (55, "Unhealthy for sensitive", 2),
            (150, "Unhealthy", 3), (9e9, "Very unhealthy", 3)]
BANDS = {
    "pm1": _PM_FINE,
    "pm2_5": _PM_FINE,
    "pm10": [(54, "Good", 0), (154, "Moderate", 1), (254, "Unhealthy for sensitive", 2),
             (354, "Unhealthy", 3), (9e9, "Very unhealthy", 3)],
    "co2": [(1000, "Good", 0), (1500, "Moderate", 1), (2500, "Poor", 2), (9e9, "Very poor", 3)],
    "voc": [(100, "Good", 0), (200, "Elevated", 1), (400, "High", 2), (9e9, "Very high", 3)],
}


def canonical(name):
    """'PM2.5', 'pm2_5', 'PM 2.5 (ug/m3)' -> 'pm2_5'; unknown names -> lower-case text."""
    import re
    text = re.sub(r"pm\s*1\.0(?![0-9])", "pm1 ", str(name or "").lower())    # 'PM1.0' is PM1, not PM10
    n = "".join(ch for ch in text if ch.isalnum())
    for key, probes in (("pm2_5", ("pm25",)), ("pm10", ("pm10",)), ("pm1", ("pm1",)),
                        ("rawvoc", ("rawvoc", "vocraw")), ("co2", ("co2",)), ("voc", ("voc",)),
                        ("temp", ("temp",)), ("hum", ("hum",))):
        if any(n.startswith(p) or n == p for p in probes):
            return key
    return n


def describe(name):
    key = canonical(name)
    meta = META.get(key, {"label": str(name), "unit": "", "digits": 2})
    return {"kind": key, **meta}


def classify(name, value):
    """{'label', 'level'} for a value, or None when the field has no bands."""
    bands = BANDS.get(canonical(name))
    if value is None or bands is None:
        return None
    for upper, label, level in bands:
        if value <= upper:
            return {"label": label, "level": level}
    return None


def thresholds(name):
    """Lower bounds of the bands above 'good', for the lines on the chart."""
    bands = BANDS.get(canonical(name)) or []
    return [{"value": bands[i - 1][0], "label": bands[i][1], "level": bands[i][2]}
            for i in range(1, len(bands))]


def band_note():
    parts = [f"{META[k]['label']} good up to {BANDS[k][0][0]:g} {META[k]['unit']}" for k in POLLUTANTS]
    return ("Categories use reference bands: " + ", ".join(parts) +
            ". They colour this page; they are not a certified safety limit.")
