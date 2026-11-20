"""Where things live on disk, and the non-secret auth metadata."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("ZEPPLOCAL_HOME")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "ZeppLocal"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / "zepplocal"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)  # health data: owner only
    return path


def db_path() -> Path:
    return data_dir() / "zepp.db"


def backups_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(exist_ok=True)
    os.chmod(path, 0o700)
    return path


def exports_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(exist_ok=True)
    return path


def _auth_file() -> Path:
    return data_dir() / "auth.json"


def load_auth() -> dict | None:
    """User id, region host and where the token is kept. Never the token."""
    try:
        return json.loads(_auth_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_auth(user_id: str, region_host: str, store: str) -> None:
    payload = {
        "user_id": user_id,
        "region_host": region_host,
        "token_store": store,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = _auth_file()
    path.write_text(json.dumps(payload, indent=2))
    os.chmod(path, 0o600)


def clear_auth() -> None:
    _auth_file().unlink(missing_ok=True)


def local_timezone() -> str:
    """IANA name of this machine's timezone (used for day boundaries)."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"
