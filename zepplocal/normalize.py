"""Turn raw Zepp API responses into tidy rows.

Rules followed throughout:
- never invent data: a missing value stays missing (no zeros, no
  interpolation, no estimated curves);
- sentinel values (0 bpm, 255 scores, 65535 posture) are dropped;
- anything we cannot decode stays in raw_records only.

Field names were cross-checked against ZeppBridge's open-source decoder, but
these functions are written defensively because Zepp's payloads vary by
firmware and region.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import date, datetime, timezone

from .sports import sport_name

# ---------------------------------------------------------------- helpers


def num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return out if out == out and abs(out) != float("inf") else None


def first(obj: dict | None, names, *fallbacks: dict | None):
    for source in (obj, *fallbacks):
        if isinstance(source, dict):
            for name in names:
                if source.get(name) not in (None, ""):
                    return source[name]
    return None


def first_num(obj: dict | None, names, *fallbacks, lo=None, hi=None) -> float | None:
    value = num(first(obj, names, *fallbacks))
    if value is None:
        return None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        return None
    return value


def parse_ts(value) -> int | None:
    """Unix seconds from seconds, milliseconds, YYYYMMDD or ISO text."""
    n = num(value)
    if n is not None:
        if 19000101 <= n <= 21001231:
            try:
                d = datetime.strptime(str(int(n)), "%Y%m%d").replace(tzinfo=timezone.utc)
                return int(d.timestamp())
            except ValueError:
                return None
        return int(n / 1000) if abs(n) >= 1e10 else int(n)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def tz_offset(value) -> int | None:
    """Seconds east of UTC from Zepp's assorted tz encodings."""
    if isinstance(value, str):
        text = value.strip()
        if "," in text:
            return tz_offset(text.split(",")[0]) or tz_offset(text.split(",")[1])
        if text.startswith(("GMT", "UTC")):
            text = text[3:]
        if text[:1] in "+-" and len(text) > 1:
            sign = -1 if text[0] == "-" else 1
            rest = text[1:]
            try:
                if ":" in rest:
                    h, m = rest.split(":", 1)
                    secs = int(h) * 3600 + int(m) * 60
                elif len(rest) == 4:
                    secs = int(rest[:2]) * 3600 + int(rest[2:]) * 60
                else:
                    secs = int(rest) * 3600
            except ValueError:
                return None
            return sign * secs if secs <= 18 * 3600 else None
        n = num(text)
    else:
        n = num(value)
    if n is None:
        return None
    raw = int(round(n))
    if abs(raw) > 18 * 3600:
        if raw % 1000:
            return None
        raw //= 1000
    if raw % 900 or abs(raw) > 18 * 3600:
        return None
    return raw


def as_date(value, offset: int | None = None) -> str | None:
    if isinstance(value, str) and len(value) == 10 and value[4] == "-":
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    n = num(value)
    if n is not None and 19000101 <= n <= 21001231:
        try:
            return datetime.strptime(str(int(n)), "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    ts = parse_ts(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts + (offset or 0), timezone.utc).date().isoformat()


def item_date(obj: dict, nested: dict | None = None) -> str | None:
    value = first(obj, ("date", "day", "dayId", "dateString", "localDate"), nested)
    if value is not None:
        return as_date(value)
    offset = tz_offset(first(obj, ("timeZone", "time_zone", "tz"), nested))
    value = first(obj, ("timestamp", "time", "startTime"), nested)
    return as_date(value, offset) if value is not None else None


def items(payload) -> list:
    """The list of records inside a response envelope (possibly empty)."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "records", "results", "list"):
        if isinstance(payload.get(key), list):
            return payload[key]
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "records", "results", "list", "summary"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _b64json(text):
    try:
        return json.loads(base64.b64decode(text))
    except Exception:  # noqa: BLE001
        return None


def _json_text(text):
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        return None


class Rows:
    """Collected output of one raw record."""

    def __init__(self):
        self.samples: list[tuple[str, int, float]] = []
        self.daily: list[tuple[str, str, float, str]] = []
        self.sleep: list[dict] = []
        self.stages: list[tuple[str, str, int, int]] = []
        self.workouts: list[dict] = []
        self.workout_samples: list[tuple] = []
        self.route: list[tuple] = []

    def day(self, d: str | None, metric: str, value: float | None, unit: str) -> None:
        if d and value is not None:
            self.daily.append((d, metric, float(value), unit))

    def count(self) -> int:
        return sum(
            len(x) for x in (self.samples, self.daily, self.sleep, self.workouts, self.workout_samples, self.route)
        )


# ---------------------------------------------------------- band_data

_STAGE = {4: "light", 5: "deep", 7: "awake", 8: "rem", 11: "rem"}


def band_data(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        day = as_date(first(item, ("date_time", "date", "dayId")))
        summary = _b64json(item["summary"]) if isinstance(item.get("summary"), str) else None
        summary = summary if isinstance(summary, dict) else {}
        offset = tz_offset(summary.get("tz"))

        slp = summary.get("slp") if isinstance(summary.get("slp"), dict) else None
        if slp:
            _sleep_from_band(day, slp, offset, out)
            out.day(day, "resting_hr", first_num(slp, ("rhr",), lo=20, hi=250), "bpm")
        stp = summary.get("stp") if isinstance(summary.get("stp"), dict) else None
        if stp:
            out.day(day, "steps", first_num(stp, ("ttl",), lo=0), "steps")
            out.day(day, "active_calories", first_num(stp, ("cal",), lo=0), "kcal")
            out.day(day, "distance", first_num(stp, ("dis",), lo=0), "m")

        # data_hr: 1440 bytes, one per minute from local midnight; 20..240 valid.
        # Without a tz we cannot place minutes in time, so we skip rather than guess.
        if day and offset is not None and isinstance(item.get("data_hr"), str):
            try:
                raw = base64.b64decode(item["data_hr"])
            except Exception:  # noqa: BLE001
                raw = b""
            midnight = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()) - offset
            for minute, bpm in enumerate(raw[:1440]):
                if 20 <= bpm <= 240:
                    out.samples.append(("heart_rate", midnight + minute * 60, float(bpm)))


def _sleep_from_band(day: str | None, slp: dict, offset: int | None, out: Rows) -> None:
    start, end = parse_ts(slp.get("st")), parse_ts(slp.get("ed"))
    if not day or start is None or end is None or end <= start:
        return
    stages = [s for s in slp.get("stage") or [] if isinstance(s, dict)]

    def minutes(mode_set) -> int | None:
        if not stages:
            return None
        total = 0
        for s in stages:
            mode, a, b = num(s.get("mode")), num(s.get("start")), num(s.get("stop"))
            if mode in mode_set and a is not None and b is not None and b >= a:
                total += int(b - a + 1)
        return total

    deep = first_num(slp, ("dp",), lo=0)
    light = first_num(slp, ("lt",), lo=0)
    awake = first_num(slp, ("wk",), lo=0)
    deep = deep if deep is not None else minutes({5})
    light = light if light is not None else minutes({4})
    awake = awake if awake is not None else minutes({7})
    rem = first_num(slp, ("rm", "rem"), lo=0)
    if rem is None:
        rem = minutes({8, 11}) or None
    span = (end - start) // 60
    duration = first_num(slp, ("duration",), lo=0)
    if duration is None:
        duration = max(span - int(awake or 0), 0)
    score = first_num(slp, ("ss", "score"), lo=0, hi=100)
    sleep_id = f"sleep:{start}:{end}"
    out.sleep.append(
        dict(
            sleep_id=sleep_id,
            date=day,
            start_ts=start,
            end_ts=end,
            duration_min=int(duration),
            deep_min=_int(deep),
            light_min=_int(light),
            rem_min=_int(rem),
            awake_min=_int(awake),
            score=_int(score),
            wake_count=_int(first_num(slp, ("wc",), lo=0, hi=200)),
        )
    )

    # Stage start/stop are minutes from a local midnight. For night sleep that
    # is the evening *before* `day` (stop can exceed 1440); naps use `day`.
    # Pick whichever anchor overlaps the session's [st, ed] best.
    if offset is None or not stages:
        return
    day_midnight = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()) - offset

    def build(anchor: int):
        built = []
        for s in stages:
            mode, a, b = num(s.get("mode")), num(s.get("start")), num(s.get("stop"))
            if mode is None or a is None or b is None or b < a:
                continue
            built.append((_STAGE.get(int(mode), "unknown"), anchor + int(a) * 60, anchor + int(b + 1) * 60))
        return built

    def overlap(slices) -> int:
        return sum(max(0, min(e, end) - max(s, start)) for _, s, e in slices)

    candidates = [build(day_midnight - 86400), build(day_midnight)]
    best = max(candidates, key=overlap)
    for stage, s, e in best:
        out.stages.append((sleep_id, stage, s, e))


def _int(value) -> int | None:
    return None if value is None else int(round(value))


# ---------------------------------------------------------- workouts


def workout_history(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        track = first(item, ("trackid", "trackId"))
        start = parse_ts(first(item, ("start_time", "startTime", "trackid")))
        end = parse_ts(first(item, ("end_time", "endTime")))
        if track is None or start is None or end is None or end <= start:
            continue
        code = num(first(item, ("type", "sport_mode")))
        code = int(code) if code is not None else None
        gain = first_num(item, ("elevationGain",), lo=0)
        out.workouts.append(
            dict(
                workout_id=str(track),
                source=first(item, ("source",)),
                type_code=code,
                type=sport_name(code),
                start_ts=start,
                end_ts=end,
                distance_m=first_num(item, ("dis", "distance"), lo=0),
                calories=first_num(item, ("calorie", "calories"), lo=0),
                avg_hr=first_num(item, ("avg_heart_rate", "avgHr"), lo=1, hi=300),
                max_hr=first_num(item, ("max_heart_rate", "maxHr"), lo=1, hi=300),
                min_hr=first_num(item, ("min_heart_rate", "minHeartRate"), lo=1, hi=300),
                moving_s=first_num(item, ("run_time", "runTime"), lo=1),
                steps=first_num(item, ("total_step", "totalStep"), lo=1),
                training_load=first_num(item, ("exercise_load", "training_load", "trainingLoad"), lo=0),
                training_effect=(lambda v: v / 10 if v else None)(first_num(item, ("te",), lo=0)),
                vo2max=first_num(item, ("vo2_max", "vo2max", "VO2_MAX"), lo=1),
                elevation_gain_m=gain / 100 if gain is not None else first_num(item, ("altitude_ascend",), lo=0),
                avg_cadence=first_num(item, ("avg_frequency",), lo=1),
                auto_detected=(lambda v: None if v is None else int(bool(v)))(item.get("auto_recognition")),
                anaerobic_te=(lambda v: v / 10 if v else None)(first_num(item, ("anaerobic_te",), lo=0)),
                rpe=first_num(item, ("rpe",), lo=1, hi=10),
                hr_zones=_zones(item.get("heart_range")),
                swim_strokes=first_num(item, ("total_strokes",), lo=1),
                swolf=first_num(item, ("swolf",), lo=1),
                pool_length_m=first_num(item, ("swim_pool_length",), lo=1),
                pool_laps=first_num(item, ("total_trips",), lo=1),
            )
        )


def _zones(text) -> str | None:
    """'secs,lower_bpm;…' (6 zones) → JSON list of seconds, or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        secs = [int(part.split(",")[0]) for part in text.split(";") if part]
    except ValueError:
        return None
    return json.dumps(secs) if any(secs) else None


def _list(text) -> list[int | None]:
    if not isinstance(text, str):
        return []
    result = []
    for part in text.split(";"):
        if part == "":
            continue
        try:
            result.append(int(part.strip()))
        except ValueError:
            result.append(None)
    return result


def _pairs(text, empty_delta_is_one=True) -> list[tuple[int, float]]:
    """Parse 'dt,value;dt,value;...'. Unparseable rows are dropped, not zeroed."""
    if not isinstance(text, str):
        return []
    result = []
    for part in text.split(";"):
        if not part:
            continue
        bits = part.split(",")
        raw_dt = bits[0].strip()
        if raw_dt == "":
            if not empty_delta_is_one:
                continue
            dt = 1
        else:
            try:
                dt = int(raw_dt)
            except ValueError:
                continue
        if len(bits) < 2:
            continue
        try:
            value = float(bits[1])
        except ValueError:
            continue
        if dt >= 0:
            result.append((dt, value))
    return result


def workout_detail(payload, workout_id: str, out: Rows) -> None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    start = parse_ts(data.get("trackid")) or parse_ts(workout_id)
    if not start:
        return

    # GPS: `time` is per-fix second deltas, `longitude_latitude` cumulative
    # 1e-8 degree deltas. Stop at the first unreadable link: everything after
    # it would be shifted.
    times = _list(data.get("time"))
    coords = data.get("longitude_latitude") if isinstance(data.get("longitude_latitude"), str) else ""
    alts = _list(data.get("altitude"))
    ts, lat, lon = start, 0, 0
    for i, part in enumerate(p for p in coords.split(";") if p):
        if i >= len(times) or times[i] is None:
            break
        bits = part.split(",")
        try:
            dlat, dlon = int(bits[0]), int(bits[1])
        except (ValueError, IndexError):
            break
        ts += max(times[i], 0)
        lat, lon = lat + dlat, lon + dlon
        la, lo = lat / 1e8, lon / 1e8
        if abs(la) > 90 or abs(lo) > 180:
            break
        alt = alts[i] if i < len(alts) else None
        alt_m = alt / 100 if alt is not None and -50000 <= alt <= 900000 else None
        out.route.append((workout_id, i, ts, la, lo, alt_m))

    # Per-second series. heart_rate values are deltas from the previous value.
    series: dict[int, dict] = {}
    t, hr = start, 0.0
    for dt, delta in _pairs(data.get("heart_rate")):
        t += dt
        hr += delta
        if 20 <= hr <= 250:
            series.setdefault(t, {})["hr"] = hr
    t = start
    for dt, speed in _pairs(data.get("speed"), empty_delta_is_one=False):
        t += dt
        if 0 <= speed < 100:
            series.setdefault(t, {})["speed"] = speed
    t = start
    for dt, power in _pairs(data.get("power_meter")):
        t += dt
        if 0 < power < 3000:
            series.setdefault(t, {})["power"] = power
    t = start
    for part in (data.get("gait") or "").split(";") if isinstance(data.get("gait"), str) else []:
        bits = part.split(",")
        if len(bits) < 4:
            continue
        try:
            dt, cadence = int(bits[0]), float(bits[3])
        except ValueError:
            continue
        t += max(dt, 0)
        if 0 < cadence < 400:
            series.setdefault(t, {})["cadence"] = cadence
    for when, row in sorted(series.items()):
        out.workout_samples.append(
            (workout_id, when, row.get("hr"), row.get("speed"), row.get("cadence"), row.get("power"))
        )


# ---------------------------------------------------------- events

# Keys that hold a per-day value in DailyHealth / readiness / watch statistics.
_DAILY_FIELDS = [
    ("steps", ("steps", "stepCount", "totalSteps"), "steps", 0, None),
    ("calories", ("calories", "totalCalories"), "kcal", 0, None),
    ("active_minutes", ("activeMinutes", "totalBurningDuration"), "min", 0, 1440),
    ("resting_hr", ("restingHeartRate", "restingHr", "rhr"), "bpm", 20, 250),
    ("readiness", ("readiness", "readinessScore", "watchScore", "rdnsScore"), "score", 0, 100),
    ("physical_readiness", ("phyScore",), "score", 0, 100),
    ("mental_readiness", ("mentScore",), "score", 0, 100),
    ("hrv_readiness", ("hrvScore",), "score", 0, 100),
    ("sleep_hrv", ("sleepHRV",), "ms", 1, 254),
    ("sleep_rhr", ("sleepRHR",), "bpm", 25, 120),
    ("hrv_baseline", ("hrvBaseline",), "ms", 1, 254),
    ("training_load", ("training_load", "trainingLoad", "wtlSum", "currnetDayTrainLoad"), "load", 0, None),
    ("vo2max", ("vo2max", "vo2Max", "VO2_MAX", "vo2_max_run"), "ml/kg/min", 1, 100),
]


def daily_generic(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        nested = item.get("value") if isinstance(item.get("value"), dict) else None
        d = item_date(item, nested)
        if not d:
            continue
        for metric, keys, unit, lo, hi in _DAILY_FIELDS:
            out.day(d, metric, first_num(item, keys, nested, lo=lo, hi=hi), unit)


def charge(payload, out: Rows) -> None:
    """Charge/real_data: value.samples[] with total/physical/mental; keep the latest."""
    for item in items(payload):
        if not isinstance(item, dict) or not isinstance(item.get("value"), dict):
            continue
        value = item["value"]
        d = item_date(item, value)
        samples = [s for s in value.get("samples") or [] if isinstance(s, dict)]
        valid = [s for s in samples if first_num(s, ("total",), lo=0, hi=100) is not None]
        if not d or not valid:
            continue
        latest = max(valid, key=lambda s: num(first(s, ("s", "offset"))) or 0)
        out.day(d, "charge", first_num(latest, ("total",), lo=0, hi=100), "score")
        out.day(d, "physical_charge", first_num(latest, ("physical",), lo=0, hi=100), "score")
        out.day(d, "mental_charge", first_num(latest, ("mental",), lo=0, hi=100), "score")


def hrv(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        value = item.get("value") if isinstance(item.get("value"), dict) else None
        if value and isinstance(value.get("samples"), list):
            base = parse_ts(first(value, ("startTime", "start_time")))
            for s in value["samples"]:
                if not isinstance(s, dict):
                    continue
                ts = parse_ts(first(s, ("timestamp", "time")))
                if ts is None and base is not None and num(first(s, ("s", "offset"))) is not None:
                    ts = base + int(num(first(s, ("s", "offset"))) / 1000)
                v = first_num(s, ("sdnn", "rmssd", "hrv", "value"), lo=1, hi=500)
                if ts is not None and v is not None:
                    out.samples.append(("hrv", ts, v))
            continue
        ts = parse_ts(first(item, ("timestamp", "time")))
        v = first_num(item, ("value", "hrv", "sdnn", "rmssd"), lo=1, hi=500)
        if ts is not None and v is not None:
            out.samples.append(("hrv", ts, v))


def respiratory_rate(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        encoded = (item.get("value") or {}).get("measurements") if isinstance(item.get("value"), dict) else None
        if not isinstance(encoded, str):
            continue
        try:
            readings = [b for b in base64.b64decode(encoded) if 4 <= b <= 60]
        except Exception:  # noqa: BLE001
            continue
        d = item_date(item)
        if readings and d:
            out.day(d, "respiratory_rate", round(sum(readings) / len(readings), 1), "brpm")


def blood_oxygen(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        sub = item.get("subType")
        if sub == "odi":
            d = item_date(item)
            out.day(d, "spo2_odi", first_num(item, ("odi",), lo=0, hi=100), "events/h")
            out.day(d, "spo2_night_score", first_num(item, ("score",), lo=0, hi=100), "score")
            continue
        extra = _json_text(item.get("extra"))
        if not isinstance(extra, dict):
            continue
        ts = parse_ts(first(extra, ("timestamp",), item))
        if ts is None:
            continue
        if sub == "osa_event":
            v = first_num(extra, ("spo2_decrease", "spo2Decrease"), lo=50, hi=100)
            if v is not None:
                out.samples.append(("spo2_apnea_low", ts, v))
            continue
        v = first_num(extra, ("spo2", "value"), lo=50, hi=100)
        if v is not None:
            out.samples.append(("spo2", ts, v))


def all_day_stress(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        nested = item.get("value") if isinstance(item.get("value"), dict) else None
        d = item_date(item, nested)
        out.day(d, "stress", first_num(item, ("avgStress", "averageStress", "stress"), nested, lo=0, hi=100), "score")
        out.day(d, "stress_min", first_num(item, ("minStress",), nested, lo=0, hi=100), "score")
        out.day(d, "stress_max", first_num(item, ("maxStress",), nested, lo=0, hi=100), "score")
        points = _json_text(first(item, ("data",), nested))
        for p in points if isinstance(points, list) else []:
            if not isinstance(p, dict):
                continue
            ts = parse_ts(first(p, ("time", "timestamp")))
            v = first_num(p, ("value",), lo=1, hi=100)
            if ts is not None and v is not None:
                out.samples.append(("stress", ts, v))


def pai(payload, out: Rows) -> None:
    for item in items(payload):
        if not isinstance(item, dict):
            continue
        d = item_date(item)
        out.day(d, "pai_daily", first_num(item, ("dailyPai",), lo=0, hi=500), "pai")
        out.day(d, "pai_total", first_num(item, ("totalPai",), lo=0, hi=1000), "pai")
        out.day(d, "device_max_hr", first_num(item, ("maxHr",), lo=100, hi=240), "bpm")
        out.day(d, "device_resting_hr", first_num(item, ("restHr",), lo=25, hi=120), "bpm")


# ---------------------------------------------------------- dispatch

STREAM_PARSERS = {
    "band_data": band_data,
    "workouts": workout_history,
    "ev:DailyHealth/summary": daily_generic,
    "ev:readiness/watch_score": daily_generic,
    "ev:Charge/real_data": charge,
    "ev:hrv_sdnn/real_data": hrv,
    "ev:RespiratoryRate/real_data": respiratory_rate,
    "uev:blood_oxygen": blood_oxygen,
    "uev:blood_oxygen/odi": blood_oxygen,
    "uev:all_day_stress": all_day_stress,
    "uev:PaiHealthInfo": pai,
    "stat:VO2_MAX": daily_generic,
    "stat:SPORT_LOAD": daily_generic,
}


def parse(stream: str, source_key: str, payload) -> Rows:
    out = Rows()
    if stream == "workout_detail":
        workout_detail(payload, source_key.split(":")[1], out)
    elif stream in STREAM_PARSERS:
        STREAM_PARSERS[stream](payload, out)
    return out


def apply(conn: sqlite3.Connection, raw_id: int, rows: Rows) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO samples(metric, ts, value, raw_id) VALUES (?, ?, ?, ?)",
        [(*s, raw_id) for s in rows.samples],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO daily(date, metric, value, unit, raw_id) VALUES (?, ?, ?, ?, ?)",
        [(*d, raw_id) for d in rows.daily],
    )
    for s in rows.sleep:
        conn.execute(
            """INSERT OR REPLACE INTO sleep(sleep_id, date, start_ts, end_ts, duration_min, deep_min,
               light_min, rem_min, awake_min, score, wake_count, raw_id)
               VALUES (:sleep_id, :date, :start_ts, :end_ts, :duration_min, :deep_min, :light_min,
               :rem_min, :awake_min, :score, :wake_count, :raw_id)""",
            {**s, "raw_id": raw_id},
        )
    sleep_ids = {s[0] for s in rows.stages}
    for sid in sleep_ids:
        conn.execute("DELETE FROM sleep_stages WHERE sleep_id = ?", (sid,))
    conn.executemany("INSERT OR REPLACE INTO sleep_stages VALUES (?, ?, ?, ?)", rows.stages)
    for w in rows.workouts:
        cols = list(w.keys()) + ["raw_id"]
        conn.execute(
            f"""INSERT INTO workouts({", ".join(cols)}) VALUES ({", ".join(":" + c for c in cols)})
                ON CONFLICT(workout_id) DO UPDATE SET
                {", ".join(f"{c} = excluded.{c}" for c in cols if c != "workout_id")}""",
            {**w, "raw_id": raw_id},
        )
    if rows.workout_samples or rows.route:
        wid = (rows.workout_samples or rows.route)[0][0]
        conn.execute("DELETE FROM workout_samples WHERE workout_id = ?", (wid,))
        conn.execute("DELETE FROM route_points WHERE workout_id = ?", (wid,))
    conn.executemany("INSERT OR REPLACE INTO workout_samples VALUES (?, ?, ?, ?, ?, ?)", rows.workout_samples)
    conn.executemany("INSERT OR REPLACE INTO route_points VALUES (?, ?, ?, ?, ?, ?)", rows.route)


def reparse_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Rebuild every normalised row from raw_records. No network."""
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT id, stream, source_key, payload FROM raw_records ORDER BY id").fetchall():
        from .db import load_raw

        rows = parse(row["stream"], row["source_key"], load_raw(row["payload"]))
        apply(conn, row["id"], rows)
        counts[row["stream"]] = counts.get(row["stream"], 0) + rows.count()
    conn.commit()
    return counts

