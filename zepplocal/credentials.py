"""The app token lives in the OS credential store, never in a plain file by default.

macOS: Keychain. Linux desktop: Secret Service. A 0600 file is used only when
you pass `--token-file` explicitly (e.g. a headless Raspberry Pi with no
keyring); it is never a silent fallback.
"""

from __future__ import annotations

import json
import os

from . import config

SERVICE = "zepplocal"


def _file():
    return config.data_dir() / "credentials.json"


def save(user_id: str, token: str, use_file: bool = False) -> str:
    if use_file:
        path = _file()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({user_id: token}, fh)
        return "file"
    import keyring

    keyring.set_password(SERVICE, user_id, token)
    return "keyring"


def load(user_id: str, store: str) -> str | None:
    if store == "file":
        try:
            return json.loads(_file().read_text()).get(user_id)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
    import keyring

    return keyring.get_password(SERVICE, user_id)


def delete(user_id: str, store: str) -> None:
    if store == "file":
        _file().unlink(missing_ok=True)
        return
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, user_id)
    except PasswordDeleteError:
        pass


def mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]} ({len(token)} chars)" if len(token) > 12 else "****"
