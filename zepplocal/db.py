"""SQLite schema. One file, on this machine, readable by any SQLite tool."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Every API response, exactly as received (gzip JSON). Normalised tables are
-- derived from these, so a parser fix can be re-applied offline (`reparse`).
CREATE TABLE IF NOT EXISTS raw_records (
    id          INTEGER PRIMARY KEY,
    stream      TEXT NOT NULL,
    source_key  TEXT NOT NULL UNIQUE,
    fetched_at  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    payload     BLOB NOT NULL
);

-- Point-in-time readings: heart_rate (per minute), spo2, stress, hrv ...
CREATE TABLE IF NOT EXISTS samples (
    metric TEXT NOT NULL,
    ts     INTEGER NOT NULL,          -- unix seconds, UTC
    value  REAL NOT NULL,
    raw_id INTEGER,
    PRIMARY KEY (metric, ts)
) WITHOUT ROWID;

-- One value per calendar day (local date as the watch recorded it).
CREATE TABLE IF NOT EXISTS daily (
    date   TEXT NOT NULL,             -- YYYY-MM-DD
    metric TEXT NOT NULL,
    value  REAL NOT NULL,
    unit   TEXT,
    raw_id INTEGER,
    PRIMARY KEY (date, metric)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sleep (
    sleep_id     TEXT PRIMARY KEY,
    date         TEXT NOT NULL,       -- wake-up day
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    duration_min INTEGER,
    deep_min     INTEGER,
    light_min    INTEGER,
    rem_min      INTEGER,
    awake_min    INTEGER,
    score        INTEGER,
    wake_count   INTEGER,
    raw_id       INTEGER
);

CREATE TABLE IF NOT EXISTS sleep_stages (
    sleep_id TEXT NOT NULL,
    stage    TEXT NOT NULL,           -- deep | light | rem | awake | unknown
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    PRIMARY KEY (sleep_id, start_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS workouts (
    workout_id     TEXT PRIMARY KEY,  -- Zepp trackid
    source         TEXT,
    type_code      INTEGER,
    type           TEXT,
    start_ts       INTEGER NOT NULL,
    end_ts         INTEGER NOT NULL,
    distance_m     REAL,
    calories       REAL,
    avg_hr         REAL,
    max_hr         REAL,
    min_hr         REAL,
    moving_s       REAL,
    steps          REAL,
    training_load  REAL,
    training_effect REAL,
    vo2max         REAL,
    elevation_gain_m REAL,
    avg_cadence    REAL,
    auto_detected  INTEGER,           -- 1 = the watch recognised it, not started by you
    anaerobic_te   REAL,
    rpe            REAL,              -- your perceived effort 1-10
    hr_zones       TEXT,              -- JSON: seconds in Zepp's 6 HR zones
    swim_strokes   REAL,
    swolf          REAL,
    pool_length_m  REAL,
    pool_laps      REAL,
    detail_status  TEXT,              -- NULL | ok | unavailable | failed
    raw_id         INTEGER
);

CREATE TABLE IF NOT EXISTS workout_samples (
    workout_id TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    hr         REAL,
    speed      REAL,                  -- m/s
    cadence    REAL,
    power      REAL,
    PRIMARY KEY (workout_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS route_points (
    workout_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    alt_m      REAL,
    PRIMARY KEY (workout_id, seq)
) WITHOUT ROWID;

-- History ledger: one row per calendar month and stream group.
-- status: written | empty (cloud returned nothing) | failed (retry later)
CREATE TABLE IF NOT EXISTS coverage (
    month      TEXT NOT NULL,
    stream     TEXT NOT NULL,
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note       TEXT,
    PRIMARY KEY (month, stream)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_workouts_start ON workouts(start_ts);
CREATE INDEX IF NOT EXISTS idx_sleep_date ON sleep(date);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_ADDED_COLUMNS = {
    "workouts": ["auto_detected INTEGER", "anaerobic_te REAL", "rpe REAL", "hr_zones TEXT",
                 "swim_strokes REAL", "swolf REAL", "pool_length_m REAL", "pool_laps REAL"],
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created (run `reparse` to fill them)."""
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")


def connect(path: Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    path = path or config.db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        conn.commit()
        path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    return conn


def store_raw(conn: sqlite3.Connection, stream: str, source_key: str, payload) -> int:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    digest = hashlib.sha256(body).hexdigest()
    conn.execute(
        """INSERT INTO raw_records(stream, source_key, fetched_at, sha256, payload)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_key) DO UPDATE SET
             fetched_at = excluded.fetched_at, sha256 = excluded.sha256, payload = excluded.payload""",
        (stream, source_key, now_iso(), digest, gzip.compress(body)),
    )
    return conn.execute("SELECT id FROM raw_records WHERE source_key = ?", (source_key,)).fetchone()[0]


def load_raw(blob: bytes):
    return json.loads(gzip.decompress(blob))


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None
