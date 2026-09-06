import sqlite3

import pytest

from portfolio import config as cfgmod
from portfolio import db as dbmod

CFG = """
db_path: {db}
notify:
  telegram:
    enabled: false
addresses:
  - chain: evm
    address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    label: main
    chains: [ethereum]
    watch: [native, tokens]
  - chain: bitcoin
    address: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa
    label: btc
    watch: [native]
tokens:
  - chain: ethereum
    contract: "0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
    symbol: USDC
    decimals: 6
    coingecko_id: usd-coin
"""


def make(tmp_path, body=CFG):
    p = tmp_path / "c.yaml"
    p.write_text(body.format(db=tmp_path / "t.db"))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    return cfg, conn


def test_init_is_idempotent(tmp_path):
    cfg, conn = make(tmp_path)
    dbmod.init(conn)  # must not raise on a second run
    assert conn.execute(
        "SELECT version FROM schema_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_sync_config_is_idempotent(tmp_path):
    cfg, conn = make(tmp_path)
    dbmod.sync_config(conn, cfg)
    dbmod.sync_config(conn, cfg)
    assert conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_dropped_address_is_disabled_not_deleted(tmp_path):
    """History must survive removing an address from the YAML."""
    cfg, conn = make(tmp_path)
    dbmod.sync_config(conn, cfg)
    cfg.addresses = [a for a in cfg.addresses if a.label == "btc"]
    dbmod.sync_config(conn, cfg)
    rows = {r["label"]: r["enabled"] for r in conn.execute("SELECT label, enabled FROM addresses")}
    assert rows == {"main": 0, "btc": 1}


def test_event_uniqueness_blocks_duplicate_alerts(tmp_path):
    """The same transfer seen twice by overlapping fetches must not insert twice."""
    cfg, conn = make(tmp_path)
    dbmod.sync_config(conn, cfg)
    aid = conn.execute("SELECT id FROM addresses WHERE label='main'").fetchone()[0]
    row = ("ethereum", aid, "0xdead", "0xdead:log:3", "transfer", "in", "1000",
           "2026-01-01T00:00:00Z")
    sql = """INSERT INTO events(chain, address_id, tx_hash, uid, kind,
                                direction, amount_raw, ts) VALUES (?,?,?,?,?,?,?,?)"""
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)


def test_accrual_and_transfer_coexist_on_one_tx(tmp_path):
    """kind is part of the key: a rebase and a transfer in the same tx are
    different facts and both must be recordable."""
    cfg, conn = make(tmp_path)
    dbmod.sync_config(conn, cfg)
    aid = conn.execute("SELECT id FROM addresses WHERE label='main'").fetchone()[0]
    sql = """INSERT INTO events(chain, address_id, tx_hash, uid, kind, ts)
             VALUES (?,?,?,?,?,?)"""
    conn.execute(sql, ("ethereum", aid, "0xd", "0xd:ext", "transfer", "2026-01-01T00:00:00Z"))
    conn.execute(sql, ("ethereum", aid, "0xd", "0xd:ext", "accrual", "2026-01-01T00:00:00Z"))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_unknown_future_schema_refuses_to_run(tmp_path):
    """A db written by newer code must not be silently used by older code."""
    cfg, conn = make(tmp_path)
    conn.execute("UPDATE schema_version SET version=99")
    with pytest.raises(RuntimeError, match="expects v"):
        dbmod.init(conn)


def test_v1_database_migrates_to_uid_keyed_events(tmp_path):
    """An existing v1 db keeps its events instead of starting over."""
    cfg, conn = make(tmp_path)
    dbmod.sync_config(conn, cfg)
    conn.executescript("""
        DROP TABLE events;
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, chain TEXT NOT NULL,
            address_id INTEGER NOT NULL REFERENCES addresses(id),
            asset_id INTEGER REFERENCES assets(id), tx_hash TEXT NOT NULL DEFAULT '',
            log_index INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL, direction TEXT,
            amount_raw TEXT, usd REAL, counterparty TEXT, block_height INTEGER,
            ts TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', detail TEXT,
            UNIQUE(chain, tx_hash, log_index, address_id, kind));
        UPDATE schema_version SET version=1;
    """)
    aid = conn.execute("SELECT id FROM addresses LIMIT 1").fetchone()[0]
    conn.execute("""INSERT INTO events(chain, address_id, tx_hash, log_index, kind, ts)
                    VALUES ('bitcoin', ?, '0xold', 2, 'transfer', '2026-01-01T00:00:00Z')""",
                 (aid,))

    dbmod.init(conn)

    assert conn.execute(
        "SELECT version FROM schema_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    row = conn.execute("SELECT tx_hash, uid FROM events").fetchone()
    assert (row["tx_hash"], row["uid"]) == ("0xold", "0xold:2")


def test_amounts_survive_beyond_int64(tmp_path):
    """1000 ETH in wei overflows SQLite INTEGER; amounts are stored as text."""
    cfg, conn = make(tmp_path)
    dbmod.sync_config(conn, cfg)
    aid = conn.execute("SELECT id FROM addresses WHERE label='main'").fetchone()[0]
    asset = conn.execute("SELECT id FROM assets").fetchone()[0]
    big = 10**21
    assert big > 2**63 - 1
    conn.execute("""INSERT INTO balances(address_id, asset_id, amount_raw, updated_at)
                    VALUES (?,?,?,?)""", (aid, asset, str(big), "2026-01-01T00:00:00Z"))
    got = conn.execute("SELECT amount_raw FROM balances").fetchone()[0]
    assert int(got) == big


def test_notifications_still_work_after_migrating(tmp_path):
    """The bug this guards: renaming `events` during a migration silently
    repointed notifications.event_id at the temporary table, and every queued
    notification afterwards died on "no such table"."""
    cfg, conn = make(tmp_path)
    dbmod.sync_config(conn, cfg)
    conn.executescript("""
        DROP TABLE events;
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, chain TEXT NOT NULL,
            address_id INTEGER NOT NULL REFERENCES addresses(id),
            asset_id INTEGER REFERENCES assets(id), tx_hash TEXT NOT NULL DEFAULT '',
            log_index INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL, direction TEXT,
            amount_raw TEXT, usd REAL, counterparty TEXT, block_height INTEGER,
            ts TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', detail TEXT,
            UNIQUE(chain, tx_hash, log_index, address_id, kind));
        UPDATE schema_version SET version=1;
    """)
    aid = conn.execute("SELECT id FROM addresses LIMIT 1").fetchone()[0]

    dbmod.init(conn)

    conn.execute("""INSERT INTO events(chain, address_id, tx_hash, uid, kind, ts)
                    VALUES ('bitcoin', ?, '0xnew', '0xnew', 'transfer', '2026-01-01')""",
                 (aid,))
    eid = conn.execute("SELECT id FROM events").fetchone()[0]
    conn.execute("INSERT INTO notifications(event_id, channel, status) VALUES (?,?,?)",
                 (eid, "telegram", "pending"))
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='notifications'").fetchone()[0]
    assert "events_v1" not in sql
