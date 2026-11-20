"""Getting a user id + app token, three ways.

1. `webview_login`: opens the *official* Zepp sign-in page in a private,
   throw-away browser window. You type your password into Zepp's page, not
   into this program. Once signed in, we read the session cookie the page
   set, and close the window.
2. `har_login`: you sign in with your own browser, save a HAR from devtools,
   and we pull the token out of requests to the Zepp API.
3. Manual entry (in cli.py): you paste the token yourself.

Nothing here talks to the network except the sign-in window itself (which
only you drive) - the token check happens in net.probe_region.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass

from .net import validate_region_host

LOGIN_URL = "https://watchface.zepp.com/"
FALLBACK_URL = "https://user.huami.com/privacy2/index.html"

# Pages the sign-in window may show: Zepp/Huami plus the third-party sign-in
# providers Zepp offers. We only *read* credentials while on Zepp/Huami pages.
_ZEPP_DOMAINS = ("zepp.com", "huami.com", "amazfit.com")
_STORAGE_SCRIPT = r"""(function(){
  try {
    var wanted = ['hm-user-login-info','hm_user_login_info','userid','user_id','apptoken',
                  'app_token','app-token','token_info','logininfo','cname','domains','wf_baseurl'];
    var out = {};
    [window.localStorage, window.sessionStorage].forEach(function(s){
      if (!s) return;
      for (var i = 0; i < s.length; i++) {
        var k = s.key(i); if (!k) continue;
        var l = k.toLowerCase();
        if (wanted.some(function(w){ return l.indexOf(w) !== -1; }) && !(k in out)) out[k] = s.getItem(k) || '';
      }
    });
    out['__cookie__'] = document.cookie || '';
    return JSON.stringify(out);
  } catch (e) { return '{}'; }
})()"""


@dataclass
class Extracted:
    user_id: str
    app_token: str
    region_hint: str | None = None

    def __repr__(self) -> str:
        return f"Extracted(user_id={self.user_id!r}, app_token=<hidden>, region_hint={self.region_hint!r})"


def _decode(value: str) -> str:
    text = value.strip().strip('"')
    for _ in range(2):
        if "%" in text:
            text = urllib.parse.unquote(text)
    return text


def _sanitize_user_id(value) -> str | None:
    text = _decode(str(value))
    return text if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text) else None


def _sanitize_token(value) -> str | None:
    text = _decode(str(value))
    if 16 <= len(text) <= 4096 and all(33 <= ord(c) <= 126 for c in text):
        return text
    return None


def parse_cookie_header(header: str) -> list[tuple[str, str]]:
    pairs = []
    for part in header.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            if name.strip():
                pairs.append((name.strip(), value.strip()))
    return pairs


def _lookup(pairs: list[tuple[str, str]], names: tuple[str, ...]) -> str | None:
    wanted = {n.lower() for n in names}
    for name, value in pairs:
        if name.lower() in wanted and value:
            return value
    return None


def _find_host(value) -> str | None:
    if isinstance(value, str):
        try:
            return validate_region_host(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        for item in value.values():
            if found := _find_host(item):
                return found
    if isinstance(value, list):
        for item in value:
            if found := _find_host(item):
                return found
    return None


def _region_hint(pairs: list[tuple[str, str]]) -> str | None:
    for key in ("wf_baseUrl", "cname", "domains", "region_host", "api_host"):
        raw = _lookup(pairs, (key,))
        if not raw:
            continue
        decoded = _decode(raw)
        if host := _find_host(decoded):
            return host
        try:
            if host := _find_host(json.loads(decoded)):
                return host
        except json.JSONDecodeError:
            pass
    return _lookup(pairs, ("region", "country_code"))


def parse_login_pairs(pairs: list[tuple[str, str]]) -> Extracted | None:
    """Find the credential in cookie / web-storage name-value pairs."""
    user_id = _lookup(pairs, ("userid", "user_id", "userId"))
    token = _lookup(pairs, ("apptoken", "app_token", "app-token", "appToken"))
    if user_id and token:
        uid, tok = _sanitize_user_id(user_id), _sanitize_token(token)
        if uid and tok:
            return Extracted(uid, tok, _region_hint(pairs))

    info = _lookup(pairs, ("hm-user-login-info", "hm_user_login_info"))
    if info:
        try:
            root = json.loads(_decode(info))
        except json.JSONDecodeError:
            return None
        token_info = root.get("token_info", root) if isinstance(root, dict) else None
        if isinstance(token_info, str):
            try:
                token_info = json.loads(_decode(token_info))
            except json.JSONDecodeError:
                return None
        if not isinstance(token_info, dict):
            return None
        uid = next((_sanitize_user_id(token_info[k]) for k in ("user_id", "userid", "userId") if k in token_info), None)
        tok = next((_sanitize_token(token_info[k]) for k in ("app_token", "apptoken", "appToken") if k in token_info), None)
        if uid and tok:
            hint = _find_host(root) or next(
                (str(token_info[k]) for k in ("region", "domain") if token_info.get(k)), None
            )
            return Extracted(uid, tok, hint)
    return None


def har_login(path: str) -> Extracted | None:
    """Read a HAR file and return the first Zepp API credential in it.

    Only requests to allow-listed api-mifit hosts are looked at.
    """
    with open(path, encoding="utf-8") as fh:
        har = json.load(fh)
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        url = request.get("url", "")
        parts = urllib.parse.urlsplit(url)
        try:
            origin = validate_region_host(f"https://{parts.hostname}")
        except (ValueError, TypeError):
            continue
        headers = {h.get("name", "").lower(): h.get("value", "") for h in request.get("headers", [])}
        token = _sanitize_token(headers.get("apptoken", ""))
        if not token:
            continue
        user_id = None
        match = re.search(r"/users/([A-Za-z0-9_-]+)/", parts.path)
        if match and match.group(1) != "me":
            user_id = match.group(1)
        if not user_id:
            query = dict(urllib.parse.parse_qsl(parts.query))
            user_id = query.get("userid") or query.get("userId")
        if user_id and (uid := _sanitize_user_id(user_id)):
            return Extracted(uid, token, origin)
    return None


def webview_login(timeout_s: int = 900) -> Extracted | None:
    """Open the official Zepp sign-in page in an isolated window and wait."""
    try:
        import webview  # pywebview
    except ImportError as exc:
        raise SystemExit(
            "The sign-in window needs pywebview: `uv sync --extra login`.\n"
            "Alternatives: `zepplocal login --har FILE` or `zepplocal login --manual`."
        ) from exc

    result: dict = {}
    window = webview.create_window("Sign in to Zepp — ZeppLocal", LOGIN_URL, width=520, height=780)

    def on_zepp_page(url: str | None) -> bool:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
        return url is not None and url.startswith("https://") and any(
            host == d or host.endswith("." + d) for d in _ZEPP_DOMAINS
        )

    def poll(win):
        started = time.monotonic()
        switched = False
        while time.monotonic() - started < timeout_s:
            time.sleep(1.5)
            try:
                url = win.get_current_url()
            except Exception:  # noqa: BLE001 - window closed
                return
            if url is None and not switched and time.monotonic() - started > 60:
                win.load_url(FALLBACK_URL)
                switched = True
                continue
            if not on_zepp_page(url):
                continue  # on Google/Apple/Xiaomi sign-in: wait, don't read anything
            pairs: list[tuple[str, str]] = []
            try:
                raw = win.evaluate_js(_STORAGE_SCRIPT) or "{}"
                storage = json.loads(raw) if isinstance(raw, str) else dict(raw)
                pairs += parse_cookie_header(storage.pop("__cookie__", ""))
                pairs += [(k, v if isinstance(v, str) else json.dumps(v)) for k, v in storage.items()]
            except Exception:  # noqa: BLE001 - page still loading
                pass
            try:
                for jar in win.get_cookies() or []:
                    for name, morsel in jar.items():
                        domain = (morsel["domain"] or "").lstrip(".").lower()
                        if any(domain == d or domain.endswith("." + d) for d in _ZEPP_DOMAINS):
                            pairs.append((name, morsel.value))
            except Exception:  # noqa: BLE001
                pass
            found = parse_login_pairs(pairs)
            if found:
                result["value"] = found
                win.destroy()
                return
        win.destroy()

    # private_mode: a fresh, non-persistent browser profile for this sign-in only.
    webview.start(poll, window, private_mode=True)
    return result.get("value")
