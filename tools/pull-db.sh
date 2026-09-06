#!/usr/bin/env bash
# Pull a consistent snapshot of the VPS database into ./data/vps.db.
#
#   tools/pull-db.sh
#
# Set the remote once, in your shell profile or in .env-style export:
#   export PW_REMOTE=rem0te@vps.example.net     # or an ssh config alias
#
# Why not scp portfolio.db directly: the watcher runs 24/7, so SQLite is in WAL
# mode and the main file is only as fresh as the last checkpoint - up to 4 MB of
# recent commits live in portfolio.db-wal. A plain copy opens without complaint
# and is silently an hour old. VACUUM INTO asks SQLite itself for a snapshot,
# consistent as of now, WAL folded in, in one file with nothing beside it.
#
# The snapshot is read-only as far as this project is concerned. To render it
# with the normal commands, point db_path at it without editing the config:
#   ./pw -c <(sed 's|^db_path:.*|db_path: data/vps.db|' config/portfolio.yaml) portfolio
set -euo pipefail

REMOTE="${PW_REMOTE:-}"
REMOTE_DIR="${PW_REMOTE_DIR:-docker/portfolio}"
LOCAL="${PW_LOCAL_DB:-data/vps.db}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

if [ -z "$REMOTE" ]; then
    echo "pull-db: set PW_REMOTE to the VPS, e.g." >&2
    echo "pull-db:   PW_REMOTE=rem0te@vps.example.net tools/pull-db.sh" >&2
    exit 2
fi

# The live local database is not a download target. Overwriting it while a local
# watcher holds it open corrupts the pair of files, and even stopped it throws
# away local cursors - which is a silent rebaseline, not an error.
if [ "$(basename "$LOCAL")" = "portfolio.db" ] && [ "${PW_ALLOW_OVERWRITE:-}" != "1" ]; then
    echo "pull-db: refusing to overwrite the live $LOCAL" >&2
    echo "pull-db: pull into data/vps.db instead, or pass PW_ALLOW_OVERWRITE=1" >&2
    exit 2
fi

echo "pull-db: snapshotting on $REMOTE:$REMOTE_DIR"
# bash -s so the remote side is a script rather than a quoting puzzle; the
# directory arrives as $1 instead of being interpolated into the heredoc.
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" <<'REMOTE_SCRIPT'
set -euo pipefail
cd "$1"
# VACUUM INTO refuses an existing target, and a leftover from a failed run would
# otherwise block every future pull.
rm -f data/snapshot.db
if command -v sqlite3 >/dev/null 2>&1; then
    # Faster than starting a container, when the host happens to have the CLI.
    sqlite3 data/portfolio.db "VACUUM INTO 'data/snapshot.db'"
else
    # The image is python:3.12-slim and carries no sqlite3 binary, but Python's
    # sqlite3 module speaks the same SQL. /app/data is the same bind mount.
    docker compose --profile cli run --rm --entrypoint python pw -c \
        "import sqlite3; sqlite3.connect('/app/data/portfolio.db').execute(\"VACUUM INTO '/app/data/snapshot.db'\")" \
        >/dev/null
fi
REMOTE_SCRIPT

# Download beside the target, then move: an interrupted transfer leaves a .part
# file rather than a half database that opens and reads short.
echo "pull-db: downloading"
mkdir -p "$(dirname "$LOCAL")"
scp -q "$REMOTE:$REMOTE_DIR/data/snapshot.db" "$LOCAL.part"
ssh "$REMOTE" "rm -f $REMOTE_DIR/data/snapshot.db"

../venv/bin/python - "$LOCAL.part" <<'CHECK'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
ok = conn.execute("PRAGMA quick_check").fetchone()[0]
if ok != "ok":
    sys.exit(f"pull-db: snapshot failed integrity check: {ok}")
CHECK

# A stale -wal/-shm pair left by an earlier local reader belongs to the file we
# are replacing. Kept beside the new snapshot, they are read as its journal.
rm -f "$LOCAL-wal" "$LOCAL-shm"
mv "$LOCAL.part" "$LOCAL"

../venv/bin/python - "$LOCAL" <<'REPORT'
import sqlite3, sys
from datetime import datetime
conn = sqlite3.connect(sys.argv[1])
q = lambda sql: conn.execute(sql).fetchone()[0]
stamp = q("SELECT max(updated_at) FROM balances")
if stamp:
    stamp = datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
print(f"pull-db: {sys.argv[1]}  balances as of {stamp or 'never'}")
pending = q("SELECT count(*) FROM notifications WHERE status = 'pending'")
print(f"pull-db: {q('SELECT count(*) FROM addresses WHERE enabled=1')} addresses, "
      f"{q('SELECT count(*) FROM events')} events, {pending} undelivered")
last = conn.execute("SELECT value FROM meta WHERE key='digest:last_total'").fetchone()
if last:
    print(f"pull-db: last digest total ${float(last[0]):,.2f}")
REPORT
