"""Synthetic data in Zepp's wire format, for trying the dashboard and for tests.

The payloads are built the way the API encodes them (base64 summaries,
delta-encoded workout strings), then pushed through the real parsers, so the
demo exercises the same code path as a real sync. No network.
"""

from __future__ import annotations

import base64
import json
import math
import random
from datetime import date, datetime, timedelta, timezone

from . import db, normalize

TZ_OFFSET = 7200  # pretend the watch is on UTC+2


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def band_day(day: date, rng: random.Random) -> dict:
    midnight = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) - TZ_OFFSET
    bed = midnight - 3600 + rng.randint(-40, 40) * 60          # ~23:00 the evening before
    wake = midnight + 7 * 3600 + rng.randint(-50, 50) * 60     # ~07:00
    # stages: minutes from the *previous* local midnight
    cursor, stages = (bed - (midnight - 86400)) // 60, []
    end_min = (wake - (midnight - 86400)) // 60
    while cursor < end_min:
        mode = rng.choices([4, 5, 8, 7], [50, 22, 22, 6])[0]
        length = rng.randint(8, 45) if mode != 7 else rng.randint(2, 8)
        stop = min(cursor + length, end_min) - 1
        stages.append({"mode": mode, "start": cursor, "stop": stop})
        cursor = stop + 1
    hr = bytearray(1440)
    for minute in range(1440):
        t = midnight + minute * 60
        asleep = bed <= t < wake
        base = 54 if asleep else 72 + 10 * math.sin(minute / 1440 * math.pi * 2 - 1.5)
        if 18 * 60 <= minute < 18 * 60 + 40 and day.weekday() in (1, 3, 5):
            base = 150  # evening run
        hr[minute] = max(0, min(255, int(base + rng.gauss(0, 4)))) if rng.random() > 0.03 else 0
    steps = rng.randint(4000, 14000)
    summary = {
        "v": 6,
        "tz": str(TZ_OFFSET),
        "slp": {"st": bed, "ed": wake, "dp": None, "lt": None, "wk": None, "rhr": 52 + rng.randint(-3, 4),
                "ss": rng.randint(60, 92), "stage": stages},
        "stp": {"ttl": steps, "dis": int(steps * 0.75), "cal": steps // 25},
    }
    summary["slp"] = {k: v for k, v in summary["slp"].items() if v is not None}
    return {"uid": "demo", "date_time": day.isoformat(), "summary": _b64(summary),
            "data_hr": base64.b64encode(bytes(hr)).decode()}


def workout(day: date, rng: random.Random, n: int) -> tuple[dict, dict]:
    start = int(datetime(day.year, day.month, day.day, 18, tzinfo=timezone.utc).timestamp()) - TZ_OFFSET
    seconds = 2400
    lat, lon = 52.5200 + rng.uniform(-0.01, 0.01), 13.4050 + rng.uniform(-0.01, 0.01)
    times, coords, hrs, speed, gait = [], [], [], [], []
    plat = plon = 0
    prev_hr = 0
    for i in range(0, seconds, 5):
        angle = i / seconds * 2 * math.pi
        la = lat + 0.012 * math.sin(angle)
        lo = lon + 0.02 * (1 - math.cos(angle))
        ila, ilo = int(la * 1e8), int(lo * 1e8)
        times.append("0" if i == 0 else "5")
        coords.append(f"{ila - plat},{ilo - plon}")
        plat, plon = ila, ilo
        h = int(140 + 15 * math.sin(i / 400) + rng.gauss(0, 2))
        hrs.append(f"{0 if i == 0 else 5},{h - prev_hr}")
        prev_hr = h
        speed.append(f"{0 if i == 0 else 5},{2.9 + 0.3 * math.sin(i / 300):.2f}")
        gait.append(f"{0 if i == 0 else 5},14,105,{168 + rng.randint(-4, 4)}")
    distance = 7000 + rng.randint(-500, 500)
    summary = {
        "trackid": str(start), "source": "run.demo.watch", "type": rng.choice([1, 1, 1, 7]),
        "end_time": str(start + seconds), "run_time": str(seconds), "dis": str(distance),
        "calorie": str(450 + rng.randint(-40, 40)), "avg_heart_rate": "141", "max_heart_rate": "163",
        "exercise_load": str(80 + rng.randint(-10, 30)), "te": "32", "avg_frequency": "168",
    }
    detail = {"code": 1, "data": {
        "trackid": start, "source": "run.demo.watch",
        "time": ";".join(times), "longitude_latitude": ";".join(coords),
        "heart_rate": ";".join(hrs), "speed": ";".join(speed), "gait": ";".join(gait),
    }}
    return summary, detail


def populate(conn, days: int = 120, seed: int = 7) -> None:
    rng = random.Random(seed)
    today = date.today()
    first = today - timedelta(days=days - 1)
    week, workouts = [], []
    for i in range(days):
        day = first + timedelta(days=i)
        week.append(band_day(day, rng))
        if len(week) == 7 or day == today:
            key = f"band_data:{week[0]['date_time']}:{week[-1]['date_time']}"
            _ingest(conn, "band_data", key, {"code": 1, "data": week})
            week = []
        if day.weekday() in (1, 3, 5):
            s, d = workout(day, rng, i)
            workouts.append(s)
            _ingest(conn, "workout_detail", f"workout_detail:{s['trackid']}", d)
        ms = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
        _ingest(conn, "uev:all_day_stress", f"demo:stress:{day}", {"items": [{
            "timestamp": ms, "avgStress": rng.randint(20, 55), "minStress": 8, "maxStress": 80,
            "data": json.dumps([{"time": ms + k * 300000, "value": max(1, min(99, int(35 + 20 * math.sin(k / 30) + rng.gauss(0, 6))))} for k in range(288)]),
        }]})
        _ingest(conn, "uev:blood_oxygen", f"demo:spo2:{day}", {"items": [
            {"subType": "click", "timestamp": ms + 3600_000 * h, "extra": json.dumps({"spo2": rng.randint(94, 99), "timestamp": ms + 3600_000 * h})}
            for h in (3, 9, 15)]})
        _ingest(conn, "ev:hrv_sdnn/real_data", f"demo:hrv:{day}", {"items": [{"timestamp": ms + 5 * 3600_000, "value": rng.randint(35, 70)}]})
        _ingest(conn, "ev:readiness/watch_score", f"demo:rd:{day}", {"items": [{"timestamp": ms, "value": {"date": day.isoformat(), "rdnsScore": rng.randint(45, 90), "sleepRHR": 52}}]})
        _ingest(conn, "uev:PaiHealthInfo", f"demo:pai:{day}", {"items": [{"date": day.isoformat(), "dailyPai": rng.randint(0, 30), "totalPai": rng.randint(60, 130), "maxHr": 188, "restHr": 52}]})
    _ingest(conn, "workouts", "workouts:demo", {"code": 1, "data": {"next": -1, "summary": workouts}})
    conn.execute("UPDATE workouts SET detail_status = 'ok'")
    for m in sorted({(first + timedelta(days=i)).strftime("%Y-%m") for i in range(days)}):
        conn.execute("INSERT OR REPLACE INTO coverage VALUES (?, 'all', ?, ?, 'demo data')",
                     (m, "partial" if m == today.strftime("%Y-%m") else "written", db.now_iso()))
    db.set_meta(conn, "demo", "1")
    conn.commit()


def _ingest(conn, stream: str, key: str, payload) -> None:
    raw_id = db.store_raw(conn, stream, key, payload)
    normalize.apply(conn, raw_id, normalize.parse(stream, key, payload))
