"""Exports: JSON, CSV, GPX, and a redacted 'AI package'.

All of these write local files only. Nothing is uploaded anywhere.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

from . import __version__


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _range_ts(first: date, last: date) -> tuple[int, int]:
    a = int(datetime.combine(first, datetime.min.time(), timezone.utc).timestamp()) - 14 * 3600
    b = int(datetime.combine(last + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()) + 14 * 3600
    return a, b


def collect(conn: sqlite3.Connection, first: date, last: date, full: bool = False) -> dict:
    """Normalised data for a date range. `full` adds per-minute/second series."""
    a, b = _range_ts(first, last)
    daily: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT date, metric, value, unit FROM daily WHERE date BETWEEN ? AND ? ORDER BY date, metric",
        (first.isoformat(), last.isoformat()),
    ):
        daily.setdefault(r["date"], {})[r["metric"]] = r["value"]
    sleep = []
    for r in conn.execute("SELECT * FROM sleep WHERE date BETWEEN ? AND ? ORDER BY start_ts", (first.isoformat(), last.isoformat())):
        s = dict(r)
        s.pop("raw_id", None)
        s["start"], s["end"] = _iso(s.pop("start_ts")), _iso(s.pop("end_ts"))
        s["stages"] = [
            {"stage": st["stage"], "start": _iso(st["start_ts"]), "end": _iso(st["end_ts"])}
            for st in conn.execute("SELECT * FROM sleep_stages WHERE sleep_id = ? ORDER BY start_ts", (r["sleep_id"],))
        ]
        sleep.append(s)
    workouts = []
    for r in conn.execute("SELECT * FROM workouts WHERE start_ts BETWEEN ? AND ? ORDER BY start_ts", (a, b)):
        w = {k: r[k] for k in r.keys() if k not in ("raw_id", "detail_status")}
        w["start"], w["end"] = _iso(w.pop("start_ts")), _iso(w.pop("end_ts"))
        w["has_route"] = bool(
            conn.execute("SELECT 1 FROM route_points WHERE workout_id = ? LIMIT 1", (r["workout_id"],)).fetchone()
        )
        if full:
            w["samples"] = [
                {"t": _iso(s["ts"]), "hr": s["hr"], "speed_mps": s["speed"], "cadence": s["cadence"], "power_w": s["power"]}
                for s in conn.execute("SELECT * FROM workout_samples WHERE workout_id = ? ORDER BY ts", (r["workout_id"],))
            ]
            w["route"] = [
                {"t": _iso(p["ts"]), "lat": p["lat"], "lon": p["lon"], "alt_m": p["alt_m"]}
                for p in conn.execute("SELECT * FROM route_points WHERE workout_id = ? ORDER BY seq", (r["workout_id"],))
            ]
        workouts.append(w)
    out = {
        "exported_by": f"ZeppLocal {__version__}",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "range": {"from": first.isoformat(), "to": last.isoformat()},
        "notes": "Missing values are absent, never zero. Times are UTC ISO-8601.",
        "daily": daily,
        "sleep": sleep,
        "workouts": workouts,
    }
    if full:
        out["samples"] = {}
        for metric in ("heart_rate", "hrv", "spo2", "stress"):
            out["samples"][metric] = [
                [_iso(r["ts"]), r["value"]]
                for r in conn.execute(
                    "SELECT ts, value FROM samples WHERE metric = ? AND ts BETWEEN ? AND ? ORDER BY ts", (metric, a, b)
                )
            ]
    return out


def to_csv(data: dict) -> str:
    """One row per day: every daily metric plus that night's sleep."""
    sleep_by_day = {s["date"]: s for s in data["sleep"]}
    metrics = sorted({m for day in data["daily"].values() for m in day})
    sleep_fields = ["duration_min", "deep_min", "light_min", "rem_min", "awake_min", "score"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", *metrics, *(f"sleep_{f}" for f in sleep_fields)])
    for d in sorted(set(data["daily"]) | set(sleep_by_day)):
        day = data["daily"].get(d, {})
        s = sleep_by_day.get(d, {})
        writer.writerow(
            [d, *[day.get(m, "") for m in metrics], *["" if s.get(f) is None else s[f] for f in sleep_fields]]
        )
    return buf.getvalue()


def workouts_csv(data: dict) -> str:
    buf = io.StringIO()
    cols = ["workout_id", "type", "start", "end", "distance_m", "moving_s", "calories", "avg_hr", "max_hr",
            "training_load", "training_effect", "vo2max", "elevation_gain_m", "avg_cadence", "has_route"]
    writer = csv.writer(buf)
    writer.writerow(cols)
    for w in data["workouts"]:
        writer.writerow(["" if w.get(c) is None else w.get(c) for c in cols])
    return buf.getvalue()


def gpx(conn: sqlite3.Connection, workout_id: str) -> str | None:
    w = conn.execute("SELECT * FROM workouts WHERE workout_id = ?", (workout_id,)).fetchone()
    points = conn.execute("SELECT * FROM route_points WHERE workout_id = ? ORDER BY seq", (workout_id,)).fetchall()
    if not w or not points:
        return None
    hr = {r["ts"]: r["hr"] for r in conn.execute("SELECT ts, hr FROM workout_samples WHERE workout_id = ?", (workout_id,))}
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="ZeppLocal" xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        f"<trk><name>{escape(w['type'] or 'workout')} {escape(_iso(w['start_ts']) or '')}</name><trkseg>",
    ]
    for p in points:
        lines.append(f'<trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">')
        if p["alt_m"] is not None:
            lines.append(f"<ele>{p['alt_m']:.1f}</ele>")
        lines.append(f"<time>{datetime.fromtimestamp(p['ts'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}</time>")
        if hr.get(p["ts"]):
            lines.append(f"<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{int(hr[p['ts']])}</gpxtpx:hr>"
                         "</gpxtpx:TrackPointExtension></extensions>")
        lines.append("</trkpt>")
    lines.append("</trkseg></trk></gpx>")
    return "\n".join(lines)


# ------------------------------------------------------------- AI package

PROMPTS = {
    "summary": "Here is my wearable health data. Summarise the main trends in activity, sleep and recovery, "
               "and point out anything that changed noticeably. Treat missing values as not measured.",
    "sleep": "Here is my sleep data from a wrist wearable. Analyse duration, consistency and stage balance, "
             "and suggest what to look at. Treat missing values as not measured.",
    "training": "Here is my workout and recovery data. Assess training load and recovery balance "
                "and suggest adjustments. Treat missing values as not measured.",
}


def ai_package(data: dict, template: str = "summary") -> str:
    """Prompt + redacted JSON. Removes ids, device info and every GPS coordinate.

    Daily aggregates, sleep and workout summaries only: no raw records, no
    account identifiers, no routes.
    """
    clean = json.loads(json.dumps(data))
    clean.pop("samples", None)
    for s in clean["sleep"]:
        s.pop("sleep_id", None)
    for i, w in enumerate(clean["workouts"], 1):
        for key in ("workout_id", "source", "route", "samples"):
            w.pop(key, None)
        w["n"] = i
    clean["redaction"] = "account ids, device ids, workout/sleep ids and all GPS coordinates removed"
    return PROMPTS.get(template, PROMPTS["summary"]) + "\n\n```json\n" + json.dumps(clean, indent=1) + "\n```\n"
