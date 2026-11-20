"""Whole-database snapshots via the SQLite backup API, with a checksum manifest."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config, db


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def create(label: str = "manual") -> Path:
    src = db.connect()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = config.backups_dir() / f"zepp-{stamp}-{label}.db"
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    counts = _counts(dst)
    dst.close()
    src.close()
    if not ok:
        target.unlink()  # never leave a broken backup that looks usable
        raise RuntimeError("backup failed its integrity check and was discarded")
    target.chmod(0o600)
    manifest = {"file": target.name, "created": stamp, "sha256": _sha256(target), "rows": counts,
                "bytes": target.stat().st_size}
    target.with_suffix(".json").write_text(json.dumps(manifest, indent=2))
    return target


def list_backups() -> list[dict]:
    out = []
    for manifest in sorted(config.backups_dir().glob("*.json")):
        info = json.loads(manifest.read_text())
        db_file = manifest.with_suffix(".db")
        info["valid"] = db_file.exists() and _sha256(db_file) == info.get("sha256")
        out.append(info)
    return out


def diff(backup_file: Path) -> dict[str, tuple[int, int]]:
    """Per-table (current, snapshot) row counts, shown before a restore."""
    current = _counts(db.connect())
    snap = _counts(sqlite3.connect(f"file:{backup_file}?mode=ro", uri=True))
    return {t: (current.get(t, 0), snap.get(t, 0)) for t in sorted(set(current) | set(snap))}


def restore(backup_file: Path) -> Path:
    """Replace the live database with a snapshot. The current one is saved first."""
    manifest = backup_file.with_suffix(".json")
    if manifest.exists() and json.loads(manifest.read_text())["sha256"] != _sha256(backup_file):
        raise RuntimeError("snapshot checksum does not match its manifest")
    safety = create("pre-restore")
    live = config.db_path()
    for suffix in ("-wal", "-shm"):
        Path(str(live) + suffix).unlink(missing_ok=True)
    shutil.copy2(backup_file, live)
    live.chmod(0o600)
    return safety
