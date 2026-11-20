import ast
import base64
import json
import os
import socket
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "zepplocal"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEPPLOCAL_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("test tried to open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


# ------------------------------------------------------------- privacy audit

NETWORK_MODULES = {"urllib.request", "http.client", "requests", "httpx", "aiohttp", "urllib3", "socket", "ftplib", "smtplib"}


def test_only_net_py_imports_network_libraries():
    """The whole outbound-network surface must be zepplocal/net.py."""
    offenders = []
    for path in PKG.rglob("*.py"):
        if path.name == "net.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                if name in NETWORK_MODULES or name.split(".")[0] in {"requests", "httpx", "aiohttp", "urllib3"}:
                    offenders.append(f"{path.name}: {name}")
    # server.py serves on 127.0.0.1 (http.server) - that is inbound and local, allowed.
    assert offenders == [], offenders


def test_static_assets_reference_no_external_origin():
    for path in (PKG / "static").iterdir():
        text = path.read_text()
        for marker in ("http://", "https://", "//cdn", "fonts.googleapis"):
            assert marker not in text, f"{path.name} references {marker}"


# ------------------------------------------------------------- host allow-list

from zepplocal import net  # noqa: E402


@pytest.mark.parametrize("good", ["https://api-mifit-us3.zepp.com", "api-mifit.huami.com", "https://api-mifit-de2.huami.com/"])
def test_region_host_accepts(good):
    assert net.validate_region_host(good).startswith("https://api-mifit")


@pytest.mark.parametrize(
    "bad",
    [
        "http://api-mifit.huami.com",
        "https://user:pass@api-mifit.huami.com",
        "https://api-mifit.huami.com:443",
        "https://api-mifit.huami.com/path",
        "https://api-mifit.huami.com?q=1",
        "https://api-mifit.huami.com.evil.example",
        "https://evil.example",
        "https://api-mifit.zepp.com.attacker.io",
    ],
)
def test_region_host_rejects(bad):
    with pytest.raises(ValueError):
        net.validate_region_host(bad)


def test_client_rejects_path_tricks():
    c = net.ZeppClient("123", "x" * 32, "https://api-mifit.huami.com")
    for path in ("//evil.example/x", "/a?b=1", "/a#f", "/a/../b", "relative"):
        with pytest.raises(ValueError):
            c.get(path)
    assert "x" * 32 not in repr(c)


def test_opener_ignores_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.attacker.example:8080")
    opener = net._build_opener()
    names = [type(h).__name__ for h in opener.handlers]
    assert "_NoRedirect" in names
    # urllib's default ProxyHandler would pick up HTTPS_PROXY; ours has no proxies,
    # so no handler can route through one.
    assert not any(getattr(h, "proxies", None) for h in opener.handlers)


# ------------------------------------------------------------- login parsing

from zepplocal import login  # noqa: E402


def test_cookie_pair_login():
    pairs = login.parse_cookie_header("foo=1; userid=1234567; apptoken=ABCdef0123456789XYZ_-token")
    found = login.parse_login_pairs(pairs)
    assert found.user_id == "1234567" and found.app_token.startswith("ABC")
    assert "ABCdef" not in repr(found)


def test_login_info_cookie():
    info = {"token_info": {"user_id": "99", "app_token": "T" * 40}, "domains": [{"cname": "api-mifit-de2.huami.com"}]}
    from urllib.parse import quote

    found = login.parse_login_pairs([("hm-user-login-info", quote(json.dumps(info)))])
    assert found.user_id == "99" and found.region_hint == "https://api-mifit-de2.huami.com"


def test_har_login(tmp_path):
    har = {"log": {"entries": [
        {"request": {"url": "https://evil.example/users/1/x", "headers": [{"name": "apptoken", "value": "E" * 40}]}},
        {"request": {"url": "https://api-mifit-us2.huami.com/users/42/devices?x=1",
                     "headers": [{"name": "apptoken", "value": "G" * 40}]}},
    ]}}
    p = tmp_path / "x.har"
    p.write_text(json.dumps(har))
    found = login.har_login(str(p))
    assert found.user_id == "42" and found.app_token == "G" * 40
    assert found.region_hint == "https://api-mifit-us2.huami.com"


# ------------------------------------------------------------- parsers

from zepplocal import db, normalize  # noqa: E402


def _b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_band_data_sleep_steps_and_minute_hr():
    hr = bytes([0] * 60 + [65] * 60 + [255] * 10)  # 0 and 255 are sentinels
    summary = {"tz": "3600", "slp": {"st": 1700000000, "ed": 1700025000, "rhr": 51, "ss": 80,
                                    "stage": [{"mode": 5, "start": 1380, "stop": 1439}, {"mode": 4, "start": 1440, "stop": 1500}]},
               "stp": {"ttl": 8000, "dis": 6000, "cal": 300}}
    out = normalize.parse("band_data", "k", {"data": [{"date_time": "2023-11-15", "summary": _b64(summary),
                                                        "data_hr": base64.b64encode(hr).decode()}]})
    daily = {m: v for _, m, v, _ in out.daily}
    assert daily["steps"] == 8000 and daily["resting_hr"] == 51
    assert len([s for s in out.samples if s[0] == "heart_rate"]) == 60  # only the valid minutes
    # 2023-11-15 local midnight at UTC+1 = 2023-11-14T23:00Z; minute 60 -> 00:00Z
    assert out.samples[0][1] == 1700006400
    assert out.sleep[0]["deep_min"] == 60 and out.sleep[0]["light_min"] == 61
    assert out.stages and out.stages[0][1] == "deep"


def test_band_data_without_tz_does_not_guess_times():
    out = normalize.parse("band_data", "k", {"data": [{"date_time": "2023-11-15", "summary": _b64({"stp": {"ttl": 5}}),
                                                        "data_hr": base64.b64encode(bytes([70] * 10)).decode()}]})
    assert out.samples == []
    assert ("2023-11-15", "steps", 5.0, "steps") in out.daily


def test_workout_detail_decoding():
    start = 1700000000
    payload = {"data": {"trackid": start, "time": "0;5;5", "longitude_latitude": "5252000000,1340500000;1000,-1000;1000,-1000",
                        "heart_rate": "0,120;5,3;5,-1", "speed": "0,2.5;5,2.7"}}
    out = normalize.parse("workout_detail", f"workout_detail:{start}", payload)
    assert [round(p[3], 5) for p in out.route] == [52.52, 52.52001, 52.52002]
    assert [p[2] for p in out.route] == [start, start + 5, start + 10]
    hr = {s[1]: s[2] for s in out.workout_samples if s[2] is not None}
    assert hr == {start: 120, start + 5: 123, start + 10: 122}


def test_route_stops_at_corrupt_link():
    payload = {"data": {"trackid": 1700000000, "time": "0;1;x;1", "longitude_latitude": "100,100;1,1;1,1;1,1"}}
    out = normalize.parse("workout_detail", "workout_detail:1700000000", payload)
    assert len(out.route) == 2


def test_spo2_and_stress():
    out = normalize.parse("uev:blood_oxygen", "k", {"items": [
        {"subType": "click", "extra": json.dumps({"spo2": 97, "timestamp": 1700000000000})},
        {"subType": "click", "extra": json.dumps({"spo2": 20, "timestamp": 1700000001000})},  # failed read
        {"subType": "odi", "date": "2023-11-15", "odi": 2.5, "score": 88},
    ]})
    assert out.samples == [("spo2", 1700000000, 97.0)]
    assert ("2023-11-15", "spo2_odi", 2.5, "events/h") in out.daily
    out = normalize.parse("uev:all_day_stress", "k", {"items": [
        {"timestamp": 1700000000000, "avgStress": 30, "data": json.dumps([{"time": 1700000000000, "value": 25}])}]})
    assert ("stress", 1700000000, 25.0) in out.samples


def test_readiness_255_is_dropped():
    out = normalize.parse("ev:readiness/watch_score", "k", {"items": [{"value": {"date": "2023-11-15", "rdnsScore": 255}}]})
    assert out.daily == []


# ------------------------------------------------------------- end to end (offline)

def test_demo_pipeline_export_and_backup(no_network):
    from zepplocal import backup, demo, export
    from datetime import date, timedelta

    conn = db.connect()
    demo.populate(conn, days=21)
    assert conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] >= 20
    assert conn.execute("SELECT COUNT(*) FROM route_points").fetchone()[0] > 100
    before = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.execute("DELETE FROM samples")
    normalize.reparse_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == before

    data = export.collect(conn, date.today() - timedelta(days=20), date.today(), full=True)
    assert data["workouts"] and data["workouts"][0]["route"]
    ai = export.ai_package(data)
    assert '"lat"' not in ai and "workout_id" not in ai and "sleep_id" not in ai and '"route"' not in ai
    wid = data["workouts"][0]["workout_id"]
    assert "<trkpt" in export.gpx(conn, wid)
    assert export.to_csv(data).startswith("date,")

    snap = backup.create()
    assert backup.list_backups()[0]["valid"]
    assert os.stat(snap).st_mode & 0o777 == 0o600


def test_server_host_check_and_csp(monkeypatch):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    from zepplocal import demo, server

    monkeypatch.setattr(net._OPENER, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    demo.populate(db.connect(), days=7)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    server.Handler.port = port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(method, path, headers):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, headers=headers)
        return c.getresponse()

    try:
        assert get("GET", "/", {"Host": "attacker.example"}).status == 403
        r = get("GET", "/api/status", {"Host": f"127.0.0.1:{port}"})
        assert r.status == 200 and "connect-src 'self'" in r.getheader("Content-Security-Policy")
        assert json.loads(r.read())["counts"]["sleep"] >= 6
        assert get("POST", "/api/sync", {"Host": f"127.0.0.1:{port}"}).status == 403  # no secret
        r = get("GET", "/", {"Host": f"localhost:{port}"})
        assert r.status == 200 and b"__SECRET__" not in r.read()
    finally:
        httpd.shutdown()
