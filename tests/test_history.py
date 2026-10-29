import json
from datetime import datetime, timedelta, timezone

import pytest

from portfolio import config as cfgmod
from portfolio import db as dbmod
from portfolio import digest
from portfolio.history import History, NET_SINCE

CFG = """
db_path: {db}
notify:
  telegram:
    enabled: false
addresses:
  - chain: bitcoin
    address: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa
    label: btc
    watch: [native]
tokens: []
"""

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
MAX_AGE = timedelta(minutes=180)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


class Db:
    def __init__(self, conn):
        self.conn = conn
        self.btc_address = conn.execute("SELECT id FROM addresses").fetchone()[0]
        cur = conn.execute(
            """INSERT INTO addresses(chain, address, label, watch, added_at)
               VALUES ('hyperliquid', '0x00000000000000000000000000000000000000aa',
                       'hl', '["hyperliquid"]', '2026-01-01')""")
        self.hl_address = cur.lastrowid
        self.btc = self.asset("bitcoin:native", "bitcoin", "BTC", 8)

    def asset(self, key, chain, symbol, decimals, whitelisted=1):
        cur = self.conn.execute(
            """INSERT INTO assets(asset_key, chain, symbol, decimals, whitelisted)
               VALUES (?,?,?,?,?)""", (key, chain, symbol, decimals, whitelisted))
        return cur.lastrowid

    def balance(self, ts, amount, asset=None, address=None):
        self.conn.execute(
            """INSERT INTO balance_snapshots(address_id, asset_id, amount_raw, ts)
               VALUES (?,?,?,?)""",
            (address or self.btc_address, asset or self.btc, str(amount), ts.isoformat()))

    def position(self, ts, key, amount, asset=None, extra=None, protocol="hyperliquid"):
        self.conn.execute(
            """INSERT INTO position_snapshots(address_id, protocol, position_key,
                                              asset_id, amount_raw, extra, ts)
               VALUES (?,?,?,?,?,?,?)""",
            (self.hl_address, protocol, key, asset, str(amount),
             json.dumps(extra or {}), ts.isoformat()))

    def price(self, ts, usd, asset=None):
        self.conn.execute("INSERT INTO prices(asset_id, usd, source, ts) VALUES (?,?,?,?)",
                          (asset or self.btc, usd, "market", ts.isoformat()))

    def history(self, since=T0 - timedelta(days=60)):
        return History(self.conn, MAX_AGE, since=since)


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(CFG.format(db=tmp_path / "h.db"))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    return Db(conn)


def test_the_past_is_valued_at_the_price_it_had_then(db):
    """Valued at today's price, every point of the chart moves with today's
    market, and a month in which bitcoin fell reads as a month of gains."""
    db.balance(at(0), 50_000_000)
    db.price(at(0), 60_000.0)
    db.price(at(2), 80_000.0)
    h = db.history()
    assert h.total_at(at(1)) == (pytest.approx(30_000.0), 0)
    assert h.total_at(at(2)) == (pytest.approx(40_000.0), 0)


def test_a_quantity_holds_until_the_next_snapshot(db):
    """Snapshots are written when something is fetched, not every hour. Reading
    the gap between two rows as zero draws the portfolio dropping to nothing
    between every pair of scans."""
    db.balance(at(0), 100_000_000)
    db.balance(at(3), 200_000_000)
    for h in range(4):
        db.price(at(h), 50_000.0)
    h = db.history()
    assert h.total_at(at(2))[0] == pytest.approx(50_000.0)
    assert h.total_at(at(3))[0] == pytest.approx(100_000.0)


def test_a_price_nobody_refreshed_leaves_the_holding_unpriced_not_stale(db):
    """The live book stops quoting a price older than max_age; a history that
    kept quoting it would draw a dead price source as a market standing still."""
    db.balance(at(0), 100_000_000)
    db.price(at(0), 50_000.0)
    db.balance(at(10), 100_000_000)
    assert db.history().total_at(at(10)) == (0.0, 1)


def test_a_closed_position_stops_counting_at_its_terminal_zero(db):
    """A closed position is deleted from the state table, and its history ends
    in a zero row. Without honouring that row, a validator exited in October is
    still 32 ETH in every point after it."""
    eth = db.asset("ethereum:native", "ethereum", "ETH", 18)
    db.position(at(0), "validator:1", 32 * 10 ** 18, asset=eth, protocol="beacon")
    db.position(at(2), "validator:1", 0, asset=eth, protocol="beacon")
    for h in range(4):
        db.price(at(h), 2_500.0, asset=eth)
    h = db.history()
    assert h.total_at(at(1))[0] == pytest.approx(80_000.0)
    assert h.total_at(at(3))[0] == 0.0


def test_a_perps_pnl_follows_the_price_between_recorded_rows(db):
    """The recorded PnL is written only on ticks where something else moved, so
    it can sit for days. Size × (price − entry) is what the venue computes, and
    the entry price only changes with the size."""
    eth = db.asset("hyperliquid:ETH", "hyperliquid", "ETH", 8)
    db.position(at(0), "perp:ETH", -4_000_000, asset=eth,
                extra={"side": "short", "entry_px": "2300", "unrealized_pnl": "0"})
    db.price(at(0), 2_300.0, asset=eth)
    db.price(at(1), 2_400.0, asset=eth)
    assert db.history().total_at(at(1))[0] == pytest.approx(-0.04 * 100)


def test_collateral_recorded_with_the_pnl_inside_is_counted_once(db):
    """Before the source netted it, the venue's spot USDC included every open
    perp's PnL. Adding the perp's PnL on top of those rows counts it twice for
    as long as a position was open."""
    usdc = db.asset("hyperliquid:USDC", "hyperliquid", "USDC", 8)
    eth = db.asset("hyperliquid:ETH", "hyperliquid", "ETH", 8)
    t = NET_SINCE - timedelta(days=1)
    db.position(t, "perp:ETH", 100_000_000, asset=eth,
                extra={"side": "long", "entry_px": "2000", "unrealized_pnl": "10"})
    db.position(t + timedelta(milliseconds=1), "spot:USDC", 510 * 10 ** 8, asset=usdc,
                extra={"hold": "100"})
    db.price(t, 1.0, asset=usdc)
    db.price(t, 2_010.0, asset=eth)
    total, _ = db.history(since=t - HOUR).total_at(t + HOUR)
    assert total == pytest.approx(500.0 + 10.0)


def test_collateral_reported_net_is_left_alone(db):
    """A row carrying the venue's `total` was already netted by the source;
    taking the PnL out again undercounts by it."""
    usdc = db.asset("hyperliquid:USDC", "hyperliquid", "USDC", 8)
    eth = db.asset("hyperliquid:ETH", "hyperliquid", "ETH", 8)
    t = NET_SINCE + timedelta(days=1)
    db.position(t, "perp:ETH", 100_000_000, asset=eth,
                extra={"side": "long", "entry_px": "2000", "unrealized_pnl": "10"})
    db.position(t, "spot:USDC", 500 * 10 ** 8, asset=usdc,
                extra={"hold": "100", "total": "510"})
    db.price(t, 1.0, asset=usdc)
    db.price(t, 2_010.0, asset=eth)
    assert db.history(since=t - HOUR).total_at(t + HOUR)[0] == pytest.approx(510.0)


def test_a_perp_closed_on_the_collaterals_tick_is_not_taken_out_of_it(db):
    """On the closing tick the venue has already realised the PnL into the
    collateral, but the perp's terminal zero is written after the collateral's
    row. Read at the row's own time, the perp looks open, its PnL is taken out
    of money that is now really there - and with nothing open the collateral
    may not be written again for weeks."""
    usdc = db.asset("hyperliquid:USDC", "hyperliquid", "USDC", 8)
    eth = db.asset("hyperliquid:ETH", "hyperliquid", "ETH", 8)
    t = NET_SINCE - timedelta(days=2)
    db.position(t - timedelta(minutes=15), "perp:ETH", 100_000_000, asset=eth,
                extra={"side": "long", "entry_px": "2000", "unrealized_pnl": "10"})
    db.position(t, "spot:USDC", 510 * 10 ** 8, asset=usdc, extra={"hold": "0"})
    db.position(t + timedelta(milliseconds=2), "perp:ETH", 0, asset=eth,
                extra={"side": "long", "entry_px": "2000", "unrealized_pnl": "10"})
    db.price(t, 1.0, asset=usdc)
    assert db.history(since=t - HOUR).total_at(t + HOUR)[0] == pytest.approx(510.0)


def test_only_what_the_digest_counts_today_is_in_the_series(db):
    """A token taken off the whitelist leaves the digest total; were it still in
    the history, the chart would end above the number the digest reports."""
    junk = db.asset("ethereum:0xjunk", "ethereum", "JUNK", 18, whitelisted=0)
    db.balance(at(0), 100_000_000)
    db.balance(at(0), 10 ** 18, asset=junk)
    db.price(at(0), 50_000.0)
    db.price(at(0), 1_000.0, asset=junk)
    assert db.history().total_at(at(1))[0] == pytest.approx(50_000.0)


def test_the_last_point_agrees_with_the_digest_for_the_same_state(db):
    """The chart and the morning total are two readings of one valuation. If
    they disagree on the same rows and prices, one of them has grown a formula
    of its own."""
    usdc = db.asset("hyperliquid:USDC", "hyperliquid", "USDC", 8)
    eth = db.asset("hyperliquid:ETH", "hyperliquid", "ETH", 8)
    t = at(5)
    db.balance(t, 50_000_000)
    db.position(t, "spot:USDC", 500 * 10 ** 8, asset=usdc, extra={"total": "498"})
    db.position(t, "perp:ETH", -4_000_000, asset=eth,
                extra={"side": "short", "entry_px": "2300", "unrealized_pnl": "-2"})
    db.position(t, "account", 94 * 10 ** 8, extra={})
    db.price(t, 77_000.0)
    db.price(t, 1.0, asset=usdc)
    db.price(t, 2_350.0, asset=eth)
    conn = db.conn
    conn.execute("""INSERT INTO balances(address_id, asset_id, amount_raw, updated_at)
                    VALUES (?,?,?,?)""", (db.btc_address, db.btc, "50000000", t.isoformat()))
    for key, asset, amount, usd, extra in (
            ("spot:USDC", usdc, 500 * 10 ** 8, None, {"total": "498"}),
            ("perp:ETH", eth, -4_000_000, 94.0,
             {"side": "short", "entry_px": "2300", "unrealized_pnl": "-2"}),
            ("account", None, 94 * 10 ** 8, 94.0, {})):
        conn.execute("""INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                              amount_raw, usd, extra, updated_at)
                        VALUES (?,'hyperliquid',?,?,?,?,?,?)""",
                     (db.hl_address, key, asset, str(amount), usd, json.dumps(extra),
                      t.isoformat()))

    class Prices:
        def value(self, key, amount_raw, decimals):
            table = {"bitcoin:native": 77_000.0, "hyperliquid:USDC": 1.0}
            return None if key not in table else amount_raw / 10 ** decimals * table[key]

    expected = digest.collect(conn, Prices())["total"]
    assert db.history().total_at(t)[0] == pytest.approx(expected)


def test_the_series_steps_on_the_hour_and_ends_at_the_last_scan(db):
    """A chart ending up to an hour before the last scan disagrees with the
    /portfolio answer sent a second earlier."""
    db.balance(at(0.25), 100_000_000)
    db.balance(at(3.5), 100_000_000)
    db.price(at(0), 50_000.0)
    points = db.history().series()
    assert [p.ts for p in points] == [at(1), at(2), at(3), at(3.5)]


def test_nothing_is_drawn_from_before_position_history_began(db):
    """Before position_snapshots existed, a position held all along has only the
    migration's seed row, stamped with when it last changed. Drawn from there,
    the first day of the chart is missing the validator: 43% of the total."""
    db.balance(at(0), 100_000_000)
    db.balance(at(48), 100_000_000)
    db.price(at(0), 50_000.0)
    points = db.history(since=at(24)).series()
    assert points[0].ts == at(24)
