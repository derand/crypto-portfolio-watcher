"""SQLite schema and helpers.

Two invariants the schema enforces rather than trusting the code to:
  - events are unique per (chain, tx_hash, log_index, address_id), so a repeated
    or overlapping fetch cannot produce a duplicate alert;
  - notifications are a separate table, so a crash between "event recorded" and
    "message sent" loses nothing: the next tick picks up the unsent rows.
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS addresses (
    id          INTEGER PRIMARY KEY,
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    label       TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    watch       TEXT NOT NULL DEFAULT '[]',
    chains      TEXT NOT NULL DEFAULT '[]',
    added_at    TEXT NOT NULL,
    UNIQUE(chain, address)
);

CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY,
    asset_key     TEXT NOT NULL UNIQUE,   -- "ethereum:native" | "ethereum:0x..."
    chain         TEXT NOT NULL,
    contract      TEXT,
    symbol        TEXT NOT NULL,
    decimals      INTEGER NOT NULL,
    coingecko_id  TEXT,
    kind          TEXT NOT NULL DEFAULT 'erc20',  -- native|erc20|receipt|debt|perp|validator
    whitelisted   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS balances (
    address_id    INTEGER NOT NULL REFERENCES addresses(id),
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    amount_raw    TEXT NOT NULL,          -- decimal string: ints here exceed 2^63
    block_height  INTEGER,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (address_id, asset_id)
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id          INTEGER PRIMARY KEY,
    address_id  INTEGER NOT NULL REFERENCES addresses(id),
    asset_id    INTEGER NOT NULL REFERENCES assets(id),
    amount_raw  TEXT NOT NULL,
    usd         REAL,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON balance_snapshots(ts);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    chain         TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT '',
    address_id    INTEGER NOT NULL REFERENCES addresses(id),
    asset_id      INTEGER REFERENCES assets(id),
    tx_hash       TEXT NOT NULL DEFAULT '',
    uid           TEXT NOT NULL,          -- provider's stable id for this movement
    kind          TEXT NOT NULL,          -- transfer|accrual|position_change|anomaly|service
    direction     TEXT,                   -- in|out|internal
    amount_raw    TEXT,
    usd           REAL,
    counterparty  TEXT,
    block_height  INTEGER,
    ts            TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'confirmed',
    detail        TEXT,
    pnl_usd       REAL,                   -- realised, and only where the venue reports it
    UNIQUE(chain, address_id, kind, uid)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER REFERENCES events(id),
    channel    TEXT NOT NULL,
    status     TEXT NOT NULL,             -- pending|sent; see pipeline.queue_state
    sent_at    TEXT,
    error      TEXT,
    UNIQUE(event_id, channel)
);

CREATE TABLE IF NOT EXISTS cursors (
    chain          TEXT NOT NULL,
    address_id     INTEGER NOT NULL REFERENCES addresses(id),
    scope          TEXT NOT NULL DEFAULT '',  -- EVM network, or '' where not applicable
    last_block     INTEGER,
    last_signature TEXT,
    last_marker    TEXT,
    last_ok_at     TEXT,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chain, address_id, scope)
);

CREATE TABLE IF NOT EXISTS prices (
    asset_id  INTEGER NOT NULL REFERENCES assets(id),
    usd       REAL NOT NULL,
    source    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    PRIMARY KEY (asset_id, ts)
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    address_id    INTEGER NOT NULL REFERENCES addresses(id),
    protocol      TEXT NOT NULL,
    position_key  TEXT NOT NULL,
    asset_id      INTEGER REFERENCES assets(id),
    amount_raw    TEXT,
    usd           REAL,
    extra         TEXT,                   -- JSON: leverage, liq price, validator status
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (address_id, protocol, position_key)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _v1_to_v2(conn: sqlite3.Connection) -> None:
    """events keyed by a provider uid instead of (tx_hash, log_index).

    One EVM transaction can move several assets to the same address - a swap
    emits two ERC-20 logs plus an internal ETH transfer - and Alchemy already
    hands us a stable id per movement. (tx_hash, log_index) collided between an
    external transfer and an internal one in the same transaction.
    """
    conn.executescript("""
        PRAGMA foreign_keys=OFF;
        -- Without legacy_alter_table, SQLite helpfully rewrites every foreign
        -- key that points at `events` to point at `events_v1` instead, and the
        -- next INSERT into notifications dies on a table that no longer exists.
        PRAGMA legacy_alter_table=ON;
        ALTER TABLE events RENAME TO events_v1;
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, chain TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
            address_id INTEGER NOT NULL REFERENCES addresses(id),
            asset_id INTEGER REFERENCES assets(id), tx_hash TEXT NOT NULL DEFAULT '',
            uid TEXT NOT NULL, kind TEXT NOT NULL, direction TEXT, amount_raw TEXT,
            usd REAL, counterparty TEXT, block_height INTEGER, ts TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed', detail TEXT,
            UNIQUE(chain, address_id, kind, uid)
        );
        INSERT INTO events(id, chain, address_id, asset_id, tx_hash, uid, kind, direction,
                           amount_raw, usd, counterparty, block_height, ts, status, detail)
            SELECT id, chain, address_id, asset_id, tx_hash,
                   tx_hash || ':' || log_index, kind, direction, amount_raw, usd,
                   counterparty, block_height, ts, status, detail FROM events_v1;
        DROP TABLE events_v1;
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        PRAGMA legacy_alter_table=OFF;
        PRAGMA foreign_keys=ON;
    """)


def _v2_to_v3(conn: sqlite3.Connection) -> None:
    """Repair notifications.event_id, which v1->v2 may have pointed at events_v1.

    Rebuilding is unconditional: it is cheap, and telling a repaired database
    from a healthy one by parsing stored SQL is more fragile than just doing it.
    """
    conn.executescript("""
        PRAGMA foreign_keys=OFF;
        PRAGMA legacy_alter_table=ON;
        ALTER TABLE notifications RENAME TO notifications_old;
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY,
            event_id INTEGER REFERENCES events(id),
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            sent_at TEXT,
            error TEXT,
            UNIQUE(event_id, channel)
        );
        INSERT INTO notifications(id, event_id, channel, status, sent_at, error)
            SELECT id, event_id, channel, status, sent_at, error FROM notifications_old;
        DROP TABLE notifications_old;
        PRAGMA legacy_alter_table=OFF;
        PRAGMA foreign_keys=ON;
    """)


def _v3_to_v4(conn: sqlite3.Connection) -> None:
    """A place to remember process-level facts, such as when the digest last ran."""
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")


def _v4_to_v5(conn: sqlite3.Connection) -> None:
    """`events.pnl_usd`: what a closed trade actually made.

    Separate from `usd`, which everywhere else means "what the thing that moved
    was worth". A closed perp moves a notional and realises a profit, and the
    two are different numbers - summing one column that sometimes holds each
    would produce a figure that means nothing. NULL is the normal state: it says
    the venue did not tell us, and nothing downstream may invent a number for it.
    """
    conn.execute("ALTER TABLE events ADD COLUMN pnl_usd REAL")


MIGRATIONS = {1: _v1_to_v2, 2: _v2_to_v3, 3: _v3_to_v4, 4: _v4_to_v5}


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        return

    version = row["version"]
    while version < SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise RuntimeError(f"no migration from schema v{version}")
        log.info("migrating schema v%d -> v%d", version, version + 1)
        step(conn)
        version += 1
        conn.execute("UPDATE schema_version SET version=?", (version,))
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"db schema v{version}, code expects v{SCHEMA_VERSION}; migration needed")


def sync_config(conn: sqlite3.Connection, cfg) -> tuple[int, int]:
    """Push addresses and the token whitelist from YAML into the db.

    The YAML file is the source of truth. Addresses dropped from it are disabled
    rather than deleted, so their history and events survive.
    """
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN")
    try:
        keep = []
        for a in cfg.addresses:
            conn.execute(
                """INSERT INTO addresses(chain, address, label, enabled, watch, chains, added_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(chain, address) DO UPDATE SET
                     label=excluded.label, enabled=excluded.enabled,
                     watch=excluded.watch, chains=excluded.chains""",
                (a.chain, a.address, a.label, int(a.enabled),
                 json.dumps(a.watch), json.dumps(a.chains), now))
            keep.append((a.chain, a.address))

        if keep:
            flat = [x for pair in keep for x in pair]
            cond = " OR ".join("(chain=? AND address=?)" for _ in keep)
            conn.execute(f"UPDATE addresses SET enabled=0 WHERE NOT ({cond})", flat)
        else:
            conn.execute("UPDATE addresses SET enabled=0")

        for t in cfg.tokens:
            conn.execute(
                """INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                      coingecko_id, kind, whitelisted)
                   VALUES (?,?,?,?,?,?,'erc20',1)
                   ON CONFLICT(asset_key) DO UPDATE SET
                     symbol=excluded.symbol, decimals=excluded.decimals,
                     coingecko_id=excluded.coingecko_id, whitelisted=1""",
                (t.asset_key, t.chain, t.contract, t.symbol, t.decimals, t.coingecko_id))

        # The mirror of the addresses rule above: a token dropped from the YAML
        # has to lose its whitelist flag, or its last balance stays frozen in
        # the portfolio total forever, priced at today's rate, removable only
        # by hand in SQLite. Scoped to erc20 - natives, Hyperliquid coins and
        # position assets are not whitelist material and were never listed.
        keys = [t.asset_key for t in cfg.tokens]
        if keys:
            holes = ",".join("?" for _ in keys)
            conn.execute(f"UPDATE assets SET whitelisted=0 WHERE kind='erc20' "
                         f"AND asset_key NOT IN ({holes})", keys)
        else:
            conn.execute("UPDATE assets SET whitelisted=0 WHERE kind='erc20'")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(cfg.addresses), len(cfg.tokens)
