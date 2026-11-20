# ZeppLocal

A private, local copy of your own Zepp / Amazfit health data: heart rate, sleep and
stages, steps, workouts with GPS, HRV, SpO₂, stress, PAI, readiness. Stored in one
SQLite file on your Mac, viewed in a dashboard served from `127.0.0.1`.

A clean-room re-implementation of the idea behind
[ZeppBridge](https://github.com/lingcang728/ZeppBridge), written so that the whole
thing is small enough to read in an evening and has **no outbound traffic except to Zepp
itself**.

> Unofficial. Uses the same private API the Zepp phone app uses. Use it only with
> your own account. Zepp can change or block this API at any time.

## What leaves your machine

| Destination | When | What |
| --- | --- | --- |
| Zepp sign-in page (`watchface.zepp.com`) | `zepplocal login`, in a private window you drive | whatever you type into Zepp's own page |
| `https://api-mifit*.zepp.com` / `*.huami.com` | `login` (region check), `sync`, `backfill` | your token, as the `apptoken` header, to your own region |
| anything else | never | — |

How that is enforced, so you can check it rather than trust it:

- `zepplocal/net.py` is the **only** module that can make network requests. A test
  (`test_only_net_py_imports_network_libraries`) fails if any other file imports a
  networking library.
- `net.py` only accepts `https://api-mifit[-xx].zepp.com|huami.com` origins (no port, path,
  userinfo), refuses redirects (so the token can't be forwarded), and ignores
  `HTTP_PROXY`/system proxies.
- The dashboard binds to `127.0.0.1`, rejects requests with a foreign `Host` header
  (DNS rebinding), and sends a Content-Security-Policy that stops the browser from
  loading or sending anything to another origin. No CDN, fonts or map tiles.
  Workout routes are drawn as plain lines: tile servers would otherwise learn where you
  run.
- No telemetry, crash reporting, update checks or "feedback" endpoint.
- The token goes into the macOS Keychain (`keyring`), never into a file unless you
  pass `--token-file`. `auth.json` holds only your user id and region.
- Dependencies: `keyring`, plus `pywebview` for the sign-in window. Everything else
  is the Python standard library.

## Setup

```bash
cd ~/Research/zepp-local
uv sync --extra login          # creates .venv with keyring + pywebview
uv run zepplocal login         # official Zepp page opens in a private window
uv run zepplocal sync          # last 30 days
uv run zepplocal serve         # dashboard on http://127.0.0.1:8765
```

Older history, month by month (stop with Ctrl-C, re-run to continue; finished months
are skipped):

```bash
uv run zepplocal backfill --since 2023-01
```

If the sign-in window can't read the credential (e.g. you use "Sign in with
Google/Apple" and it lands somewhere unexpected):

- **HAR**: sign in at watchface.zepp.com in your browser → DevTools → Network →
  reload → "Save all as HAR" → `uv run zepplocal login --har file.har` → delete the HAR
  file (it contains your token).
- **Manual**: `uv run zepplocal login --manual` and paste the `apptoken` and user
  id (input is hidden).

## Commands

| | |
| --- | --- |
| `login` / `logout` | connect, or delete the token (keeps your data) |
| `status`, `privacy` | what's connected, row counts, where things live |
| `sync [--days N]` | incremental fetch (re-fetches the last 3 days to pick up late uploads) |
| `backfill --since YYYY-MM [--force]` | history, month by month, with a coverage ledger |
| `reparse` | rebuild every table from the stored raw responses, offline |
| `serve [--port] [--no-browser]` | dashboard |
| `export --format json\|csv\|workouts-csv\|gpx\|ai` | files in `exports/` (or `--out`, `--copy`) |
| `backup create\|list\|restore FILE` | SQLite snapshots with SHA-256 manifests and integrity check |
| `demo` | synthetic data, for trying the dashboard (`ZEPPLOCAL_HOME=/tmp/zl-demo`) |

`export --format ai` builds a prompt plus JSON with account/device/workout ids and all
GPS coordinates removed. It only writes a file or copies to your clipboard; it never
sends anything.

## Data

- macOS: `~/Library/Application Support/ZeppLocal/` (folder `0700`, files `0600`);
  Linux: `~/.local/share/zepplocal/`; override with `ZEPPLOCAL_HOME`.
- `zepp.db` is plaintext SQLite. Open it with any SQLite tool. Tables: `daily`,
  `samples` (minute HR, SpO₂, stress, HRV), `sleep`, `sleep_stages`, `workouts`,
  `workout_samples`, `route_points`, `coverage`, plus `raw_records` (every API response,
  gzipped, so parser fixes can be re-applied with `reparse`).
- The database isn't encrypted. FileVault covers it at rest. Backups sit on the same
  disk, so copy one elsewhere if you care about drive failure.

## Honesty rules (borrowed from ZeppBridge, because they're right)

- Missing is missing: no zeros for unmeasured values, no interpolated curves (charts
  break across gaps), no estimated sleep stages.
- Sentinels are dropped (0/255 bpm, readiness 255, SpO₂ < 50).
- If a day's summary has no timezone, minute heart rate and stage times are **not**
  placed, rather than guessed as UTC.

## Status

The payload formats come from reading ZeppBridge's open-source decoder (MIT), not
from a live account. Parsing is covered by unit tests on synthetic payloads, and the
dashboard was checked with `demo` data. **The first real sync is the real test**: if a
stream comes back empty or odd, the raw response is in `raw_records`, and a fixed parser
can be applied with `reparse` without fetching again.

Not implemented (yet): MCP server, FIT export, weight / blood pressure, per-second
heart rate files, auto-sync schedule (use `cron`/`launchd` with `zepplocal sync`).

## Running on the Raspberry Pi

`deploy/pi-deploy.sh` copies the code to the Pi, sets up a venv and installs two systemd units:

- `zepplocal-web.service`: the dashboard, listening **only on the Pi's Tailscale address**
  (`http://pihole1.tail448243.ts.net:8810/`). The LAN and the internet can't reach it.
- `zepplocal-sync.timer`: runs `zepplocal sync` at 08:00 and 20:00 (catches up after downtime).

```bash
deploy/pi-deploy.sh --with-data   # first time: also copies the Mac database + token
deploy/pi-deploy.sh               # later: update code/services only
deploy/pi-deploy.sh --token       # after the token expires: `uv run zepplocal login` on the Mac, then this
```

The Pi has no keyring, so the token sits in `~/.local/share/zepplocal/credentials.json` (mode 0600).
It is sent over ssh stdin, never on a command line. Sync logs: `journalctl -u zepplocal-sync`.
