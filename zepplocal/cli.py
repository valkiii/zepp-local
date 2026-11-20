"""zepplocal command line."""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from . import __version__, backup, config, credentials, db, export, login, net, normalize

FIRST_SYNC_DAYS = 30
OVERLAP_DAYS = 3


def make_client() -> net.ZeppClient:
    auth = config.load_auth()
    if not auth:
        raise SystemExit("Not connected. Run `zepplocal login` first.")
    token = credentials.load(auth["user_id"], auth.get("token_store", "keyring"))
    if not token:
        raise SystemExit("Token missing from the credential store. Run `zepplocal login` again.")
    return net.ZeppClient(auth["user_id"], token, auth["region_host"])


def incremental_range(conn, days: int | None) -> tuple[date, date]:
    today = date.today()
    if days:
        return today - timedelta(days=days - 1), today
    last = db.get_meta(conn, "last_sync_day")
    if last:
        return min(date.fromisoformat(last), today) - timedelta(days=OVERLAP_DAYS), today
    return today - timedelta(days=FIRST_SYNC_DAYS - 1), today


# ---------------------------------------------------------------- commands


def cmd_login(args) -> None:
    if args.har:
        found = login.har_login(args.har)
        if not found:
            raise SystemExit("No Zepp API credential found in that HAR file.")
        print("Credential found in HAR. Delete the HAR file now; it is equivalent to your password.")
    elif args.manual:
        user_id = input("Zepp user id (numeric): ").strip()
        token = getpass.getpass("App token (input hidden): ").strip()
        found = login.Extracted(user_id, token, args.region)
    else:
        print("Opening the official Zepp sign-in page in a private window…")
        found = login.webview_login()
        if not found:
            raise SystemExit("Sign-in window closed before a credential was found. "
                             "Try `zepplocal login --har FILE` or `--manual`.")
    print(f"Signed in as user {found.user_id}. Finding your region…")
    host = net.probe_region(found.user_id, found.app_token, args.region or found.region_hint)
    store = credentials.save(found.user_id, found.app_token, use_file=args.token_file)
    config.save_auth(found.user_id, host, store)
    print(f"Connected: {host}. Token saved to {'a 0600 file' if store == 'file' else 'the system keychain'}.")
    print("Next: `zepplocal sync` (last 30 days) or `zepplocal backfill --since 2024-01`.")


def cmd_logout(args) -> None:
    auth = config.load_auth()
    if auth:
        credentials.delete(auth["user_id"], auth.get("token_store", "keyring"))
    config.clear_auth()
    print("Token removed from the credential store. Your health database was kept.")


def cmd_status(args) -> None:
    auth = config.load_auth()
    conn = db.connect()
    print(f"ZeppLocal {__version__}")
    print(f"data folder : {config.data_dir()}")
    if auth:
        token = credentials.load(auth["user_id"], auth.get("token_store", "keyring"))
        print(f"account     : user {auth['user_id']} @ {auth['region_host']}")
        print(f"token       : {credentials.mask(token) if token else 'MISSING - run login'} ({auth.get('token_store')})")
    else:
        print("account     : not connected")
    for table in ("samples", "daily", "sleep", "workouts", "route_points", "raw_records"):
        print(f"{table:12}: {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:,} rows")
    span = conn.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
    print(f"days held   : {span[0]} … {span[1]}")
    for r in conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 3"):
        print(f"sync        : {r['started_at']}  {r['outcome']}  {r['detail']}")


def _run_sync(first: date, last: date, skip_done: bool = False) -> None:
    from .sync import Syncer

    conn = db.connect()
    try:
        result = Syncer(conn, make_client(), config.local_timezone()).run(first, last, skip_done=skip_done)
    except net.NeedsReauth:
        raise SystemExit("Zepp rejected the token (expired or revoked). Run `zepplocal login` again.")
    print(f"\n{result['outcome']}: {result['rows']:,} rows written, {result['requests']} requests.")
    for err in result["errors"][:10]:
        print(f"  error: {err}")


def cmd_sync(args) -> None:
    first, last = incremental_range(db.connect(), args.days)
    print(f"Syncing {first} … {last}")
    _run_sync(first, last)


def cmd_backfill(args) -> None:
    try:
        y, m = (int(x) for x in args.since.split("-")[:2])
        first = date(y, m, 1)
    except ValueError:
        raise SystemExit("--since must look like 2024-01")
    print(f"Backfilling {first} … today, month by month. Ctrl-C to stop; re-run to continue.")
    _run_sync(first, date.today(), skip_done=not args.force)


def cmd_reparse(args) -> None:
    counts = normalize.reparse_all(db.connect())
    for stream, n in sorted(counts.items()):
        print(f"{stream:32} {n:,} rows")


def cmd_serve(args) -> None:
    from .server import serve

    serve(args.port, open_browser=not args.no_browser, host=args.host, allow_hosts=args.allow_host)


def cmd_export(args) -> None:
    conn = db.connect(readonly=True)
    last = date.fromisoformat(args.to) if args.to else date.today()
    first = date.fromisoformat(args.from_) if args.from_ else last - timedelta(days=29)
    if args.format == "gpx":
        if not args.workout:
            raise SystemExit("--workout ID is required for gpx")
        text = export.gpx(conn, args.workout)
        if not text:
            raise SystemExit("That workout has no GPS route.")
        suffix = "gpx"
    else:
        data = export.collect(conn, first, last, full=args.full)
        if args.format == "json":
            text, suffix = json.dumps(data, indent=1), "json"
        elif args.format == "csv":
            text, suffix = export.to_csv(data), "csv"
        elif args.format == "workouts-csv":
            text, suffix = export.workouts_csv(data), "csv"
        else:
            text, suffix = export.ai_package(data, args.template), "md"
    if args.copy:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        print(f"Copied {len(text):,} characters to the clipboard.")
        return
    out = Path(args.out) if args.out else config.exports_dir() / f"zepp-{args.format}-{first}-{last}.{suffix}"
    out.write_text(text)
    print(f"Wrote {out}")


def cmd_backup(args) -> None:
    if args.action == "create":
        print(f"Snapshot: {backup.create()}")
    elif args.action == "list":
        for b in backup.list_backups():
            print(f"{b['file']}  {b['bytes']:>12,} bytes  {'ok' if b['valid'] else 'CHECKSUM MISMATCH'}")
    elif args.action == "restore":
        path = Path(args.file)
        for table, (cur, snap) in backup.diff(path).items():
            print(f"{table:16} now {cur:>10,}  snapshot {snap:>10,}")
        if input("Replace the current database with this snapshot? [y/N] ").lower() != "y":
            return
        print(f"Restored. Previous database saved as {backup.restore(path)}")


def cmd_demo(args) -> None:
    from . import demo

    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] and db.get_meta(conn, "demo") != "1":
        raise SystemExit("This data folder holds real data. Use a separate one: ZEPPLOCAL_HOME=/tmp/zl-demo zepplocal demo")
    demo.populate(conn, days=args.days)
    print(f"Synthetic data written to {config.db_path()}. Run `zepplocal serve` with the same ZEPPLOCAL_HOME.")


def cmd_privacy(args) -> None:
    auth = config.load_auth() or {}
    print("Where your data is and where it can go:\n")
    print(f"  database     {config.db_path()}  (plaintext SQLite, folder mode 0700)")
    print(f"  token        {'system keychain' if auth.get('token_store') != 'file' else 'credentials.json (0600)'}")
    print("  network      only https://api-mifit*.zepp.com / *.huami.com, from zepplocal/net.py")
    print(f"  your region  {auth.get('region_host', '(not connected)')}")
    print("  dashboard    127.0.0.1 only; CSP blocks every other origin")
    print("  telemetry    none. No update checks, no crash reports, no analytics.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="zepplocal", description="Your Zepp/Amazfit data, on your machine.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="connect your Zepp account")
    s.add_argument("--har", metavar="FILE", help="import the token from a browser HAR export")
    s.add_argument("--manual", action="store_true", help="type the user id and token yourself")
    s.add_argument("--region", help="region host, e.g. https://api-mifit-de2.huami.com")
    s.add_argument("--token-file", action="store_true", help="store the token in a 0600 file (no keyring)")
    s.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="delete the stored token").set_defaults(func=cmd_logout)
    sub.add_parser("status", help="connection and data summary").set_defaults(func=cmd_status)
    sub.add_parser("privacy", help="where data lives and what the network can reach").set_defaults(func=cmd_privacy)

    s = sub.add_parser("sync", help="fetch recent data (incremental)")
    s.add_argument("--days", type=int, help="fetch exactly the last N days")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("backfill", help="fetch older history month by month")
    s.add_argument("--since", required=True, help="first month, YYYY-MM")
    s.add_argument("--force", action="store_true", help="re-fetch months already marked complete")
    s.set_defaults(func=cmd_backfill)

    sub.add_parser("reparse", help="rebuild tables from stored raw responses (offline)").set_defaults(func=cmd_reparse)

    s = sub.add_parser("serve", help="open the local dashboard")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--host", default="127.0.0.1",
                   help="address to listen on; keep 127.0.0.1 unless it's a private (e.g. Tailscale) address")
    s.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="extra hostname browsers may use, e.g. pihole1.tailnet.ts.net (repeatable)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("export", help="write JSON / CSV / GPX / AI package")
    s.add_argument("--format", choices=["json", "csv", "workouts-csv", "gpx", "ai"], default="json")
    s.add_argument("--from", dest="from_", help="YYYY-MM-DD (default: 30 days ago)")
    s.add_argument("--to", help="YYYY-MM-DD (default: today)")
    s.add_argument("--full", action="store_true", help="json: include per-minute/second series and routes")
    s.add_argument("--workout", help="gpx: workout id")
    s.add_argument("--template", choices=sorted(export.PROMPTS), default="summary", help="ai: prompt")
    s.add_argument("--out", help="output file")
    s.add_argument("--copy", action="store_true", help="copy to clipboard instead of writing a file")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("demo", help="fill a separate data folder with synthetic data")
    s.add_argument("--days", type=int, default=120)
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("backup", help="database snapshots")
    s.add_argument("action", choices=["create", "list", "restore"])
    s.add_argument("file", nargs="?")
    s.set_defaults(func=cmd_backup)

    args = p.parse_args(argv)
    if args.cmd == "backup" and args.action == "restore" and not args.file:
        p.error("backup restore needs a snapshot file")
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to continue.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
