"""Local dashboard: http://127.0.0.1:8765, standard library only.

- binds to 127.0.0.1 by default (not your LAN); `--host` can pick a private
  address such as the Pi's Tailscale IP;
- rejects requests whose Host header isn't localhost (blocks DNS-rebinding
  attacks from web pages you visit);
- the page's Content-Security-Policy forbids loading or sending anything to
  any other origin, so the browser itself enforces "no data leaves";
- the one write action (Sync now) needs a per-launch secret.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, db, export

STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_sync_state = {"running": False, "log": [], "result": None}
_SECRET = secrets.token_urlsafe(24)


def _parse_day(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _ts_bounds(first: date, last: date) -> tuple[int, int]:
    a = int(datetime.combine(first, datetime.min.time()).astimezone().timestamp())
    b = int(datetime.combine(last + timedelta(days=1), datetime.min.time()).astimezone().timestamp())
    return a, b


def _downsample(rows: list, limit: int = 2500) -> list:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


class Handler(BaseHTTPRequestHandler):
    server_version = "ZeppLocal"
    port = 8765

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # ---- plumbing ---------------------------------------------------------
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in {f"{h}:{self.port}" for h in self.allowed_hosts}

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _download(self, text: str, name: str, ctype: str):
        self._send(200, text.encode(), ctype, {"Content-Disposition": f'attachment; filename="{name}"'})

    # ---- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        path = url.path
        try:
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text().replace("__SECRET__", _SECRET)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if path in ("/app.js", "/app.css"):
                ctype = "text/javascript" if path.endswith(".js") else "text/css"
                return self._send(200, (STATIC / path[1:]).read_bytes(), ctype + "; charset=utf-8")
            if path.startswith("/api/"):
                return self._api(path[5:], q)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": str(exc)}, 500)
        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        if not self._host_ok() or self.headers.get("X-ZL-Secret") != _SECRET:
            return self._send(403, b"forbidden", "text/plain")
        if self.path == "/api/sync":
            return self._json(start_sync())
        self._send(404, b"not found", "text/plain")

    def _api(self, name: str, q: dict):
        conn = db.connect(readonly=True)
        today = date.today()
        last = _parse_day(q.get("to"), today)
        first = _parse_day(q.get("from"), last - timedelta(days=29))
        a, b = _ts_bounds(first, last)
        try:
            if name == "status":
                auth = config.load_auth() or {}
                counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("raw_records", "samples", "daily", "sleep", "workouts", "route_points")}
                span = conn.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
                return self._json({
                    "connected": bool(auth.get("user_id")),
                    "region": auth.get("region_host"),
                    "last_sync": [dict(r) for r in conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 5")],
                    "counts": counts,
                    "first_day": span[0], "last_day": span[1],
                    "coverage": [dict(r) for r in conn.execute("SELECT * FROM coverage ORDER BY month DESC")],
                    "metrics": [r[0] for r in conn.execute("SELECT DISTINCT metric FROM daily ORDER BY metric")],
                    "sync": {"running": _sync_state["running"], "log": _sync_state["log"][-12:],
                             "result": _sync_state["result"]},
                })
            if name == "daily":
                metrics = [m for m in q.get("metrics", "").split(",") if m]
                out = {m: [] for m in metrics}
                for r in conn.execute(
                    f"SELECT date, metric, value FROM daily WHERE date BETWEEN ? AND ? "
                    f"AND metric IN ({','.join('?' * len(metrics))}) ORDER BY date",
                    (first.isoformat(), last.isoformat(), *metrics),
                ):
                    out[r["metric"]].append([r["date"], r["value"]])
                return self._json(out)
            if name == "samples":
                # Average into time buckets so a long range keeps its shape (≤ ~2500 points).
                bucket = max(60, -(-(b - a) // 2500))
                rows = conn.execute(
                    "SELECT (ts / ?) * ? + ? / 2 AS t, ROUND(AVG(value), 1) FROM samples "
                    "WHERE metric = ? AND ts BETWEEN ? AND ? GROUP BY ts / ? ORDER BY t",
                    (bucket, bucket, bucket, q.get("metric", "heart_rate"), a, b, bucket),
                ).fetchall()
                return self._json([[r[0], r[1]] for r in rows])
            if name == "sleep":
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM sleep WHERE date BETWEEN ? AND ? ORDER BY start_ts", (first.isoformat(), last.isoformat()))]
                if q.get("stages") and rows:
                    sid = rows[-1]["sleep_id"] if q["stages"] == "latest" else q["stages"]
                    stages = [dict(r) for r in conn.execute(
                        "SELECT stage, start_ts, end_ts FROM sleep_stages WHERE sleep_id = ? ORDER BY start_ts", (sid,))]
                    return self._json({"sessions": rows, "stages": stages, "stage_session": sid})
                if q.get("compare"):  # several nights' stages for the overlay chart
                    ids = q["compare"].split(",")[:31]
                    stages = [dict(r) for r in conn.execute(
                        f"SELECT sleep_id, stage, start_ts, end_ts FROM sleep_stages WHERE sleep_id IN "
                        f"({','.join('?' * len(ids))}) ORDER BY start_ts", ids)]
                    return self._json({"sessions": rows, "stages": stages})
                return self._json({"sessions": rows})
            if name == "workouts":
                return self._json([dict(r) for r in conn.execute(
                    "SELECT w.*, (SELECT COUNT(*) FROM route_points p WHERE p.workout_id = w.workout_id) AS route_n "
                    "FROM workouts w WHERE start_ts BETWEEN ? AND ? ORDER BY start_ts DESC", (a, b))])
            if name.startswith("workout/"):
                wid = name.split("/", 1)[1]
                w = conn.execute("SELECT * FROM workouts WHERE workout_id = ?", (wid,)).fetchone()
                if not w:
                    return self._json({"error": "not found"}, 404)
                return self._json({
                    "workout": dict(w),
                    "samples": _downsample([list(r) for r in conn.execute(
                        "SELECT ts, hr, speed, cadence, power FROM workout_samples WHERE workout_id = ? ORDER BY ts", (wid,))]),
                    "route": _downsample([list(r) for r in conn.execute(
                        "SELECT ts, lat, lon, alt_m FROM route_points WHERE workout_id = ? ORDER BY seq", (wid,))], 4000),
                })
            if name == "export":
                fmt = q.get("format", "json")
                data = export.collect(conn, first, last, full=q.get("full") == "1")
                stem = f"zepp-{first}-{last}"
                if fmt == "csv":
                    return self._download(export.to_csv(data), f"{stem}-daily.csv", "text/csv")
                if fmt == "workouts_csv":
                    return self._download(export.workouts_csv(data), f"{stem}-workouts.csv", "text/csv")
                if fmt == "ai":
                    return self._send(200, export.ai_package(data, q.get("template", "summary")).encode(),
                                      "text/plain; charset=utf-8")
                return self._download(json.dumps(data, indent=1), f"{stem}.json", "application/json")
            if name.startswith("gpx/"):
                text = export.gpx(conn, name.split("/", 1)[1])
                if not text:
                    return self._json({"error": "no route"}, 404)
                return self._download(text, f"workout-{name.split('/', 1)[1]}.gpx", "application/gpx+xml")
            return self._json({"error": "unknown endpoint"}, 404)
        finally:
            conn.close()


def start_sync(days: int | None = None) -> dict:
    if _sync_state["running"]:
        return {"started": False, "reason": "already running"}
    from .cli import make_client, incremental_range
    from .sync import Syncer

    def log(msg: str):
        _sync_state["log"].append(f"{datetime.now():%H:%M:%S} {msg}")

    def run():
        _sync_state.update(running=True, log=[], result=None)
        try:
            client = make_client()
            conn = db.connect()
            first, last = incremental_range(conn, days)
            _sync_state["result"] = Syncer(conn, client, config.local_timezone(), log).run(first, last)
        except Exception as exc:  # noqa: BLE001
            _sync_state["result"] = {"outcome": "failed", "errors": [str(exc)]}
        finally:
            _sync_state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


def serve(port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1",
          allow_hosts: list[str] | None = None) -> None:
    db.connect().close()  # make sure the schema exists
    Handler.allowed_hosts = tuple({"127.0.0.1", "localhost", host, *(h.lower().rstrip(".") for h in allow_hosts or [])})
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"No free port between {port} and {port + 19}; try --port")
    port = Handler.port = candidate
    url = f"http://{host}:{port}/"
    print(f"ZeppLocal dashboard on {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
