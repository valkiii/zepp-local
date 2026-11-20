#!/usr/bin/env bash
# Deploy ZeppLocal to the Raspberry Pi and run it as a Tailscale-only dashboard
# with an automatic daily sync.
#
#   deploy/pi-deploy.sh              update code + services (keeps the Pi's data)
#   deploy/pi-deploy.sh --with-data  first move: also copy this Mac's database and token
#   deploy/pi-deploy.sh --token      only refresh the token (after `zepplocal login` on the Mac)
#
# Nothing on the Pi is ever deleted. An existing database on the Pi is never
# overwritten. The token travels over ssh stdin (not on a command line) into a
# 0600 file, because a headless Pi has no keyring.
set -euo pipefail

PI="${PI:-vc@192.168.2.36}"
PORT="${PORT:-8810}"
TS_IP="${TS_IP:-100.106.44.120}"              # the Pi's Tailscale address: the dashboard listens only here
TS_NAME="${TS_NAME:-pihole1.tail448243.ts.net}"
SYNC_TIMES="${SYNC_TIMES:-*-*-* 08,20:00:00}"
APP=/home/vc/zepp-local
DATA=/home/vc/.local/share/zepplocal
HERE="$(cd "$(dirname "$0")/.." && pwd)"
MAC_DATA="$HOME/Library/Application Support/ZeppLocal"

mode="${1:-}"

push_token() {
  local uid
  uid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["user_id"])' "$MAC_DATA/auth.json")
  # Read from the Mac Keychain and write straight into the Pi's 0600 file via stdin.
  (cd "$HERE" && uv run python -c '
import json, sys
from zepplocal import credentials
t = credentials.load(sys.argv[1], "keyring")
if not t: sys.exit("No token in the Keychain: run `uv run zepplocal login` first")
sys.stdout.write(json.dumps({sys.argv[1]: t}))' "$uid") |
    ssh "$PI" "mkdir -p $DATA && chmod 700 $DATA && umask 077 && cat > $DATA/credentials.json && chmod 600 $DATA/credentials.json"
  # auth.json without the token, pointing at the file store
  python3 -c 'import json,sys; a=json.load(open(sys.argv[1])); a["token_store"]="file"; print(json.dumps(a, indent=2))' "$MAC_DATA/auth.json" |
    ssh "$PI" "umask 077 && cat > $DATA/auth.json"
  echo "token refreshed on the Pi"
}

if [[ "$mode" == "--token" ]]; then push_token; exit 0; fi

echo "== code"
rsync -a --exclude .venv --exclude .git --exclude __pycache__ --exclude .pytest_cache --exclude '*.har' \
  "$HERE/" "$PI:$APP/"
ssh "$PI" "cd $APP && { [ -x .venv/bin/python ] || python3 -m venv .venv; } && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q ."

if [[ "$mode" == "--with-data" ]]; then
  echo "== data"
  if ssh "$PI" "test -e $DATA/zepp.db"; then
    echo "The Pi already has a database ($DATA/zepp.db): not overwriting it."
  else
    snap="$(mktemp -d)/zepp.db"
    sqlite3 "$MAC_DATA/zepp.db" ".backup '$snap'"   # consistent copy even while the Mac app runs
    ssh "$PI" "mkdir -p $DATA && chmod 700 $DATA"
    scp -q "$snap" "$PI:$DATA/zepp.db"
    ssh "$PI" "chmod 600 $DATA/zepp.db"
    rm -f "$snap"
    echo "database copied"
  fi
  push_token
fi

echo "== services"
ssh "$PI" "sudo tee /etc/systemd/system/zepplocal-web.service >/dev/null" <<EOF
[Unit]
Description=ZeppLocal dashboard (Tailscale only)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=vc
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/zepplocal serve --no-browser --host $TS_IP --port $PORT --allow-host $TS_NAME --allow-host pihole1
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
ssh "$PI" "sudo tee /etc/systemd/system/zepplocal-sync.service >/dev/null" <<EOF
[Unit]
Description=ZeppLocal sync from Zepp
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=vc
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/zepplocal sync
EOF
ssh "$PI" "sudo tee /etc/systemd/system/zepplocal-sync.timer >/dev/null" <<EOF
[Unit]
Description=ZeppLocal automatic sync

[Timer]
OnCalendar=$SYNC_TIMES
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
EOF
ssh "$PI" "sudo systemctl daemon-reload && sudo systemctl enable --now zepplocal-sync.timer zepplocal-web.service && sudo systemctl restart zepplocal-web.service"
sleep 2
ssh "$PI" "systemctl is-active zepplocal-web.service; systemctl list-timers zepplocal-sync.timer --no-pager | head -2"
echo "Dashboard: http://$TS_NAME:$PORT/  (Tailscale devices only)"
