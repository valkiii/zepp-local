"""Fetch from Zepp, store raw, normalise. Organised by calendar month so that
re-fetching a month replaces its raw records instead of piling up copies, and
so the coverage ledger can say exactly which months you hold.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from . import db, normalize
from .net import NeedsReauth, Unavailable, ZeppClient, ZeppError

V2_EVENTS = [
    ("hrv_sdnn", "real_data"),
    ("readiness", "watch_score"),
    ("Charge", "real_data"),
    ("DailyHealth", "summary"),
    ("RespiratoryRate", "real_data"),
]
USER_EVENTS = [("blood_oxygen", None), ("all_day_stress", None), ("PaiHealthInfo", None)]
WATCH_STATS = ["VO2_MAX", "SPORT_LOAD"]
PAUSE_S = 0.25  # be gentle with the API


def month_starts(first: date, last: date) -> list[date]:
    months, cursor = [], first.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months


def _month_end(m: date) -> date:
    return (m + timedelta(days=32)).replace(day=1) - timedelta(days=1)


class Syncer:
    def __init__(self, conn: sqlite3.Connection, client: ZeppClient, tz_name: str, log: Callable[[str], None] = print):
        self.conn, self.client, self.log = conn, client, log
        self.tz_name = tz_name
        self.tz = ZoneInfo(tz_name)
        self.errors: list[str] = []
        self.written = 0

    # one request -> raw -> rows ------------------------------------------------
    def _fetch(self, stream: str, key: str, call: Callable[[], dict]) -> int | None:
        """Returns rows written, or None if the fetch failed."""
        try:
            payload = call()
        except NeedsReauth:
            raise
        except Unavailable:
            return 0
        except ZeppError as exc:
            self.errors.append(f"{key}: {exc}")
            self.log(f"  ! {key}: {exc}")
            return None
        finally:
            time.sleep(PAUSE_S)
        raw_id = db.store_raw(self.conn, stream, key, payload)
        rows = normalize.parse(stream, key, payload)
        normalize.apply(self.conn, raw_id, rows)
        self.conn.commit()
        self.written += rows.count()
        return rows.count()

    def _ms(self, d: date) -> int:
        return int(datetime.combine(d, datetime.min.time(), self.tz).timestamp() * 1000)

    # one calendar month (clipped to [first, last]) -------------------------------
    def sync_month(self, month: date, first: date, last: date) -> str:
        lo, hi = max(month, first), min(_month_end(month), last)
        label = month.strftime("%Y-%m")
        results: list[int | None] = []

        # band_data in 7-day slices aligned to the month (stable raw keys)
        d = month
        while d <= _month_end(month):
            a, b = d, min(d + timedelta(days=6), _month_end(month))
            if b >= lo and a <= hi:
                results.append(
                    self._fetch(
                        "band_data",
                        f"band_data:{a}:{b}",
                        lambda a=a, b=b: self.client.band_data(a.isoformat(), b.isoformat()),
                    )
                )
            d = b + timedelta(days=1)

        m_from, m_to = self._ms(month), self._ms(_month_end(month) + timedelta(days=1))
        for et, st in V2_EVENTS:
            results.append(
                self._fetch(
                    f"ev:{et}/{st}", f"ev:{et}/{st}:{label}", lambda et=et, st=st: self.client.events_v2(et, st, m_from, m_to)
                )
            )
        for et, st in USER_EVENTS:
            results.append(
                self._fetch(f"uev:{et}", f"uev:{et}:{label}", lambda et=et, st=st: self.client.user_events(et, st, m_from, m_to))
            )
        results.append(
            self._fetch(
                "uev:blood_oxygen/odi",
                f"uev:blood_oxygen/odi:{label}",
                lambda: self.client.user_events_by_date(
                    "blood_oxygen", "odi", month.isoformat(), _month_end(month).isoformat(), self.tz_name
                ),
            )
        )
        for stat in WATCH_STATS:
            results.append(
                self._fetch(
                    f"stat:{stat}",
                    f"stat:{stat}:{label}",
                    lambda stat=stat: self.client.watch_statistics(stat, month.isoformat(), _month_end(month).isoformat()),
                )
            )

        if any(r is None for r in results):
            status = "failed"
        elif sum(results) > 0:
            status = "written"
        else:
            status = "empty"
        # A month that is still running can't be "complete"; keep it re-fetchable.
        complete = hi == _month_end(month) and lo == month and _month_end(month) < date.today()
        self.conn.execute(
            "INSERT OR REPLACE INTO coverage(month, stream, status, updated_at, note) VALUES (?, 'all', ?, ?, ?)",
            (label, status if complete or status == "failed" else "partial", db.now_iso(), f"{lo}..{hi}"),
        )
        self.conn.commit()
        return status

    # workouts -------------------------------------------------------------------
    def sync_workouts(self, first: date, last: date) -> None:
        start = int(datetime.combine(first, datetime.min.time(), self.tz).timestamp())
        stop = int(datetime.combine(last + timedelta(days=1), datetime.min.time(), self.tz).timestamp())
        cursor = stop
        for _ in range(200):  # page guard
            try:
                payload = self.client.sport_history(start, cursor)
            except Unavailable:
                break
            except ZeppError as exc:
                if isinstance(exc, NeedsReauth):
                    raise
                self.errors.append(f"workouts: {exc}")
                break
            key = f"workouts:{start}:{cursor}"
            raw_id = db.store_raw(self.conn, "workouts", key, payload)
            rows = normalize.parse("workouts", key, payload)
            normalize.apply(self.conn, raw_id, rows)
            self.conn.commit()
            self.written += rows.count()
            nxt = (payload.get("data") or {}).get("next", -1) if isinstance(payload, dict) else -1
            nxt = normalize.num(nxt) or -1
            if nxt <= 0 or nxt >= cursor or nxt <= start:
                break
            cursor = int(nxt)
            time.sleep(PAUSE_S)

        pending = self.conn.execute(
            """SELECT workout_id, source FROM workouts
               WHERE start_ts >= ? AND start_ts < ? AND (detail_status IS NULL OR detail_status = 'failed')
               ORDER BY start_ts""",
            (start, stop),
        ).fetchall()
        for i, w in enumerate(pending, 1):
            self.log(f"  workout detail {i}/{len(pending)}")
            status = "ok"
            if not w["source"]:
                status = "unavailable"
            else:
                key = f"workout_detail:{w['workout_id']}"
                try:
                    result = self._fetch(
                        "workout_detail", key, lambda w=w: self.client.sport_detail(w["workout_id"], w["source"])
                    )
                    status = "failed" if result is None else "ok"
                except Unavailable:
                    status = "unavailable"
            self.conn.execute("UPDATE workouts SET detail_status = ? WHERE workout_id = ?", (status, w["workout_id"]))
            self.conn.commit()

    # entry point ------------------------------------------------------------------
    def run(self, first: date, last: date, skip_done: bool = False) -> dict:
        log_id = self.conn.execute(
            "INSERT INTO sync_log(started_at, detail) VALUES (?, ?)", (db.now_iso(), f"{first}..{last}")
        ).lastrowid
        self.conn.commit()
        outcome = "ok"
        try:
            done = set()
            if skip_done:
                done = {
                    r[0]
                    for r in self.conn.execute("SELECT month FROM coverage WHERE status IN ('written', 'empty')")
                }
            for m in month_starts(first, last):
                label = m.strftime("%Y-%m")
                if label in done:
                    self.log(f"{label}: already complete, skipping")
                    continue
                self.log(f"{label}: fetching…")
                status = self.sync_month(m, first, last)
                self.log(f"{label}: {status}")
            self.log("workouts: fetching…")
            self.sync_workouts(first, last)
            if self.errors:
                outcome = "partial"
            db.set_meta(self.conn, "last_sync_day", last.isoformat())
        except NeedsReauth:
            outcome = "needs_reauth"
            raise
        except KeyboardInterrupt:
            outcome = "cancelled"
            raise
        finally:
            self.conn.execute(
                "UPDATE sync_log SET finished_at = ?, outcome = ?, detail = ? WHERE id = ?",
                (db.now_iso(), outcome, f"{first}..{last}; rows={self.written}; errors={len(self.errors)}", log_id),
            )
            self.conn.commit()
        return {"outcome": outcome, "rows": self.written, "errors": self.errors, "requests": self.client.requests_made}
