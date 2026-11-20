"""The only module in ZeppLocal that touches the network.

Everything that leaves this machine goes through `ZeppClient.get`, and that
method can only reach the Zepp / Huami regional API hosts. `tests/test_audit.py`
fails if any other module imports a networking library, so this file is the
whole network surface you need to read.

Hardening:
- host allow-list: only https://api-mifit*.zepp.com / *.huami.com, no port,
  path, query, fragment or user:password@ part;
- system and environment proxies are ignored, so HTTP_PROXY cannot route the
  token through a third party;
- redirects are never followed, so the `apptoken` header cannot be forwarded
  to another host;
- bounded timeouts, retries and response size.
"""

from __future__ import annotations

import gzip
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import __version__

_HOST_RE = re.compile(r"^api-mifit(-[a-z]{2}[0-9]?)?\.(zepp|huami)\.com$")

# Regional API origins seen in the wild. Used when the sign-in page gives no
# region hint: we try each until one accepts the token.
REGION_HOSTS = [
    "https://api-mifit-de2.huami.com",
    "https://api-mifit-us2.huami.com",
    "https://api-mifit-us3.zepp.com",
    "https://api-mifit-de.zepp.com",
    "https://api-mifit-de.huami.com",
    "https://api-mifit-sg.huami.com",
    "https://api-mifit-sg2.huami.com",
    "https://api-mifit-in.huami.com",
    "https://api-mifit-ru.huami.com",
    "https://api-mifit-us.huami.com",
    "https://api-mifit.zepp.com",
    "https://api-mifit.huami.com",
    "https://api-mifit-cn.huami.com",
    "https://api-mifit-cn2.huami.com",
    "https://api-mifit-cn.zepp.com",
    "https://api-mifit-cn2.zepp.com",
    "https://api-mifit-cn3.zepp.com",
]

MAX_BODY = 32 * 1024 * 1024
TIMEOUT = 30


class ZeppError(Exception):
    pass


class NeedsReauth(ZeppError):
    """401/403: the token was rejected. Sign in again."""


class Unavailable(ZeppError):
    """404: this endpoint does not exist for this account / region."""


def validate_region_host(value: str) -> str:
    """Return the canonical `https://host` origin, or raise ValueError."""
    raw = value.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "https":
        raise ValueError("region host must use https")
    if parts.username or parts.password or parts.port:
        raise ValueError("region host must not carry credentials or a port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("region host must be a bare origin")
    host = (parts.hostname or "").lower()
    if not _HOST_RE.match(host):
        raise ValueError(f"not a Zepp API host: {host!r}")
    return f"https://{host}"


def _validate_path(path: str) -> None:
    if (
        not path.startswith("/")
        or path.startswith("//")
        or any(ch in path for ch in "?#\\")
        or ".." in path
    ):
        raise ValueError(f"invalid API path: {path!r}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # surfaces as HTTPError 3xx instead of following


def _build_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    try:  # uv/standalone Pythons may lack a CA bundle; use the OS trust store.
        import truststore  # type: ignore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),  # ignore HTTP(S)_PROXY and system proxies
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ctx),
    )


_OPENER = _build_opener()


class ZeppClient:
    def __init__(self, user_id: str, app_token: str, region_host: str, retries: int = 3):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user_id or ""):
            raise ValueError("invalid user id")
        if not app_token or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in app_token):
            raise ValueError("invalid app token")
        self.user_id = user_id
        self._token = app_token
        self.base = validate_region_host(region_host)
        self.retries = retries
        self.requests_made = 0

    def __repr__(self) -> str:  # never print the token
        return f"ZeppClient(user_id={self.user_id!r}, base={self.base!r})"

    def _headers(self) -> dict[str, str]:
        # The same header set the official phone app sends; the API rejects
        # requests without `appname` / `appplatform`.
        return {
            "apptoken": self._token,
            "appname": "com.huami.midong",
            "appplatform": "ios_phone",
            "v": "2.0",
            "vn": "10.2.5",
            "cv": "1722_10.2.5",
            "lang": "en",
            "timezone": "UTC",
            "accept": "application/json",
            "accept-encoding": "gzip",
            "user-agent": f"ZeppLocal/{__version__}",
        }

    def get(self, path: str, params: dict | None = None) -> dict:
        _validate_path(path)
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        # Defence in depth: the final URL must still be on the validated origin.
        if not url.startswith(self.base + "/"):
            raise ValueError("URL escaped the region origin")

        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                self.requests_made += 1
                with _OPENER.open(req, timeout=TIMEOUT) as resp:
                    body = resp.read(MAX_BODY + 1)
                    if len(body) > MAX_BODY:
                        raise ZeppError("response larger than 32 MiB")
                    if resp.headers.get("content-encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise ZeppError(f"non-JSON response from {path}") from exc
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise NeedsReauth(f"HTTP {exc.code} on {path}") from None
                if exc.code == 404:
                    raise Unavailable(f"HTTP 404 on {path}") from None
                if exc.code == 429 or exc.code >= 500:
                    last = ZeppError(f"HTTP {exc.code} on {path}")
                else:
                    raise ZeppError(f"HTTP {exc.code} on {path}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = ZeppError(f"network error on {path}: {getattr(exc, 'reason', exc)}")
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        raise last or ZeppError("request failed")

    # ---- endpoints -------------------------------------------------------

    def devices(self) -> dict:
        return self.get(
            f"/users/{self.user_id}/devices",
            {"enableMultiDevice": "true", "device_type": "android_phone"},
        )

    def band_data(self, from_date: str, to_date: str) -> dict:
        """Per-day summary (sleep, steps), minute heart rate. Dates are local YYYY-MM-DD."""
        return self.get(
            "/v1/data/band_data.json",
            {
                "userid": self.user_id,
                "from_date": from_date,
                "to_date": to_date,
                "query_type": "detail",
                "byteLength": "8",
                "device_type": "0",
            },
        )

    def sport_history(self, start_ts: int, stop_track_id: int) -> dict:
        """Account-wide workout list (the 'run' path covers every sport type)."""
        return self.get(
            "/v1/sport/run/history.json",
            {
                "userid": self.user_id,
                "startTrackId": str(start_ts),
                "stopTrackId": str(stop_track_id),
                "need_sub_data": "1",
                "type": "",
            },
        )

    def sport_detail(self, track_id: str, source: str) -> dict:
        if not re.fullmatch(r"[0-9]{1,20}", str(track_id)):
            raise ValueError("invalid track id")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", source or ""):
            raise ValueError("invalid workout source")
        return self.get("/v1/sport/run/detail.json", {"trackid": str(track_id), "source": source})

    def events_v2(self, event_type: str, sub_type: str | None, from_ms: int, to_ms: int) -> dict:
        params = {"eventType": event_type}
        if sub_type:
            params["subType"] = sub_type
        params.update({"from": str(from_ms), "to": str(to_ms), "limit": "2000", "reverse": "0"})
        return self.get("/v2/users/me/events", params)

    def user_events(self, event_type: str, sub_type: str | None, from_ms: int, to_ms: int) -> dict:
        params = {
            "eventType": event_type,
            "from": str(from_ms),
            "to": str(to_ms),
            "limit": "2000",
            "reverse": "0",
            "userId": self.user_id,
        }
        if sub_type:
            params["subType"] = sub_type
        return self.get(f"/users/{self.user_id}/events", params)

    def user_events_by_date(self, event_type: str, sub_type: str, from_day: str, to_day: str, tz: str) -> dict:
        return self.get(
            f"/users/{self.user_id}/events/dateString",
            {
                "eventType": event_type,
                "subType": sub_type,
                "from": from_day,
                "to": to_day,
                "timeZone": tz,
                "limit": "2000",
                "reverse": "0",
                "userId": self.user_id,
            },
        )

    def watch_statistics(self, statistic: str, start_day: str, end_day: str) -> dict:
        if statistic not in ("SPORT_LOAD", "VO2_MAX"):
            raise ValueError("unknown statistic")
        return self.get(
            f"/v2/watch/users/{self.user_id}/WatchSportStatistics/{statistic}",
            {"startDay": start_day, "endDay": end_day, "limit": "900", "isReverse": "false"},
        )


def probe_region(user_id: str, app_token: str, hint: str | None = None) -> str:
    """Find the regional host that accepts this token. Returns the origin.

    Only allow-listed Zepp hosts are contacted; the request is the harmless
    device list.
    """
    candidates: list[str] = []
    if hint:
        try:
            candidates.append(validate_region_host(hint))
        except ValueError:
            token = hint.strip().lower()
            candidates += [h for h in REGION_HOSTS if f"-{token}." in h]
    for host in REGION_HOSTS:
        if host not in candidates:
            candidates.append(host)

    def attempt(host: str) -> tuple[str, str]:
        try:
            ZeppClient(user_id, app_token, host, retries=0).devices()
            return host, "ok"
        except NeedsReauth:
            return host, "rejected"
        except Exception as exc:  # noqa: BLE001
            return host, f"error: {exc}"

    # Try the hinted host alone first, then the rest in parallel.
    if hint and candidates:
        host, status = attempt(candidates[0])
        if status == "ok":
            return host
        candidates = candidates[1:]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, candidates))
    for host, status in results:
        if status == "ok":
            return host
    if all(status == "rejected" for _, status in results):
        raise NeedsReauth("every Zepp region rejected this token")
    raise ZeppError("no Zepp region accepted the token: " + "; ".join(f"{h}: {s}" for h, s in results[:4]))
