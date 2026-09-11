from datetime import datetime, timezone

import pytest

from portfolio import config as cfgmod
from portfolio import db as dbmod
from portfolio import digest

CFG = """
db_path: {db}
digest:
  enabled: true
  hour: 9
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


class Prices:
    def __init__(self, table):
        self.table = table

    def usd(self, key):
        return self.table.get(key)

    def value(self, key, amount_raw, decimals):
        price = self.table.get(key)
        return None if price is None else amount_raw / 10 ** decimals * price


@pytest.fixture()
def setup(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(CFG.format(db=tmp_path / "d.db"))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    aid = conn.execute("SELECT id FROM addresses").fetchone()[0]
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('bitcoin:native','bitcoin',NULL,'BTC',8,'native',1)""")
    sid = conn.execute("SELECT id FROM assets").fetchone()[0]
    conn.execute("""INSERT INTO balances(address_id, asset_id, amount_raw, updated_at)
                    VALUES (?,?,?,?)""", (aid, sid, str(50_000_000), "2026-01-01"))
    return cfg, conn, aid, sid


def test_total_is_the_sum_of_priced_holdings(setup):
    cfg, conn, _, _ = setup
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0}))
    assert data["total"] == pytest.approx(38500.0)      # 0.5 BTC
    assert data["unpriced"] == 0


def test_unpriced_holdings_are_counted_out_and_declared(setup):
    """Never fold an unknown price into the total as zero and stay quiet."""
    cfg, conn, _, _ = setup
    data = digest.collect(conn, Prices({}))
    assert data["total"] == 0.0
    assert data["unpriced"] == 1
    body = digest.render(conn, data).body
    assert "no price" in body


def test_second_digest_reports_the_change_since_the_first(setup):
    cfg, conn, _, _ = setup
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0}))
    first = digest.render(conn, data).body
    assert "since last digest" not in first
    dbmod.set_meta(conn, digest.LAST_TOTAL, "38500.00")

    data = digest.collect(conn, Prices({"bitcoin:native": 80000.0}))
    second = digest.render(conn, data).body
    assert "+1,500.00" in second and "+3.90%" in second


def test_perp_notional_is_not_counted_as_money_held(setup):
    """A 25 BTC long is exposure. Adding its notional to the portfolio total
    would report a fortune that cannot be withdrawn - only the unrealised PnL
    on top of the margin is money."""
    cfg, conn, aid, _ = setup
    conn.execute("""INSERT INTO positions(address_id, protocol, position_key,
                                          amount_raw, usd, extra, updated_at)
                    VALUES (?,'hyperliquid','perp:BTC',?,?,?,?)""",
                 (aid, str(2_500_000_000), 1_927_310.0,
                  '{"side": "long", "liq_distance_pct": "7.37",'
                  ' "unrealized_pnl": "412.50"}', "2026-01-01"))
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0}))
    assert data["total"] == pytest.approx(38500.0 + 412.50)
    # The full perp table is in the portfolio view; the digest keeps one line,
    # because a position drifting toward liquidation must not be invisible in
    # the message that is actually read every morning.
    assert "liq 7.37% away" in digest.render_state(conn, data).body
    assert "closest liq 7.37% away" in digest.render(conn, data).body


def test_exchange_account_value_is_not_added_to_the_spot_balance(setup):
    """Hyperliquid reserves perp margin inside the spot USDC balance: the spot
    row's `hold` equals marginUsed exactly, and an account holding USDC with no
    position reports accountValue 0. Adding accountValue on top of spot counts
    the same dollars twice - on one bot it turned $104 into $207."""
    cfg, conn, aid, _ = setup
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('hyperliquid:USDC','hyperliquid',NULL,'USDC',8,'perp',1)""")
    usdc = conn.execute("SELECT id FROM assets WHERE asset_key='hyperliquid:USDC'").fetchone()[0]
    conn.execute("""INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                          amount_raw, usd, extra, updated_at)
                    VALUES (?,'hyperliquid','spot:USDC',?,?,NULL,'{}',?)""",
                 (aid, usdc, str(103_92_000_000), "2026-01-01"))
    conn.execute("""INSERT INTO positions(address_id, protocol, position_key,
                                          amount_raw, usd, extra, updated_at)
                    VALUES (?,'hyperliquid','account',?,?,'{}',?)""",
                 (aid, str(103_43_000_000), 103.43, "2026-01-01"))
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0,
                                        "hyperliquid:USDC": 1.0}))
    assert data["total"] == pytest.approx(38500.0 + 103.92)
    assert "account" not in digest.render_state(conn, data, cfg).body

def test_staked_holding_is_priced_by_its_asset_not_its_key(setup):
    """"staking:pending" ends in a bucket name, not a coin. Deriving the asset
    from the key leaves a four-figure holding silently unpriced."""
    cfg, conn, aid, _ = setup
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('hyperliquid:HYPE','hyperliquid',NULL,'HYPE',8,'perp',1)""")
    hype = conn.execute("SELECT id FROM assets WHERE symbol='HYPE'").fetchone()[0]
    conn.execute("""INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                          amount_raw, usd, extra, updated_at)
                    VALUES (?,'hyperliquid','staking:pending',?,?,NULL,
                            '{"withdrawals": "1"}',?)""",
                 (aid, hype, str(1_275_774_913), "2026-01-01"))
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0,
                                        "hyperliquid:HYPE": 81.8005}))
    assert data["unpriced"] == 0
    assert data["total"] == pytest.approx(38500.0 + 12.75774913 * 81.8005)
    body = digest.render_state(conn, data).body
    # The bucket has to survive into the text: an address can hold delegated,
    # undelegated and pending HYPE at once, and three rows reading "btc HYPE"
    # would be unreadable. The digest drops the "staking:" prefix, which is the
    # same for all three, and keeps the bucket, which is not.
    assert "pending" in body and "HYPE" in body


def test_due_waits_for_the_configured_hour(setup):
    cfg, conn, _, _ = setup
    assert digest.due(cfg, conn, datetime(2026, 1, 1, 8, 59)) is False
    assert digest.due(cfg, conn, datetime(2026, 1, 1, 9, 0)) is True


def test_due_fires_once_a_day(setup):
    cfg, conn, _, _ = setup
    dbmod.set_meta(conn, digest.LAST_DATE, "2026-01-01")
    assert digest.due(cfg, conn, datetime(2026, 1, 1, 20, 0)) is False
    assert digest.due(cfg, conn, datetime(2026, 1, 2, 9, 0)) is True


def test_digest_shows_claim_worthy_lp_fees(setup):
    cfg, conn, _, _ = setup
    claim = {"label": "main", "chain": "ethereum", "venue": "uniswap-v3",
             "token_id": 42, "usd": 63.5}
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0}), [claim])

    body = digest.render(conn, data, cfg).body
    assert "LP fees ready to claim" in body
    assert "main uniswap-v3 #42  $63.50" in body


def test_disabled_digest_never_fires(setup):
    cfg, conn, _, _ = setup
    cfg.digest.enabled = False
    assert digest.due(cfg, conn, datetime(2026, 1, 1, 23, 0)) is False


def test_position_amount_is_scaled_by_its_asset_not_by_hyperliquid(setup):
    """Positions used to be Hyperliquid only, where everything is 8 decimals.
    A validator balance is ETH at 18: read as 8 it reports 32 ETH as three
    hundred billion and puts the portfolio total in the trillions."""
    cfg, conn, aid, _ = setup
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('ethereum:native','ethereum',NULL,'ETH',18,'native',1)""")
    eth = conn.execute("SELECT id FROM assets WHERE asset_key='ethereum:native'").fetchone()[0]
    conn.execute("""INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                          amount_raw, usd, extra, updated_at)
                    VALUES (?,'beacon','validator:999999',?,?,NULL,'{}',?)""",
                 (aid, eth, str(32008617079 * 10**9), "2026-01-01"))
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0,
                                        "ethereum:native": 2505.0}))
    assert data["total"] == pytest.approx(38500.0 + 32.008617079 * 2505.0)
    body = digest.render_state(conn, data).body
    # Four decimals is what the table shows; a scale error would print
    # 320,086,170.79 here, so the assertion still catches it.
    assert "32.0086" in body


GROUPED = """
db_path: {db}
digest:
  enabled: true
  hour: 9
notify:
  telegram:
    enabled: false
addresses:
  - chain: evm
    address: "0x0000000000000000000000000000000000000001"
    label: evm-2
    chains: [ethereum]
    watch: [native, tokens]
tokens:
  - chain: ethereum
    contract: "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
    symbol: WSTETH
    decimals: 18
    group: ETH
  - chain: ethereum
    contract: "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    symbol: USDC
    decimals: 6
    group: USD
"""

WSTETH = "ethereum:0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
USDC = "ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
PRICES = Prices({"ethereum:native": 2500.0, WSTETH: 3000.0, USDC: 1.0})


@pytest.fixture()
def evm(tmp_path):
    """One EVM address holding ETH, wstETH and USDC, grouped by the whitelist."""
    p = tmp_path / "g.yaml"
    p.write_text(GROUPED.format(db=tmp_path / "g.db"))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    aid = conn.execute("SELECT id FROM addresses").fetchone()[0]
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('ethereum:native','ethereum',NULL,'ETH',18,'native',1)""")

    def hold(asset_key, raw):
        sid = conn.execute("SELECT id FROM assets WHERE asset_key=?",
                           (asset_key,)).fetchone()[0]
        conn.execute("""INSERT INTO balances(address_id, asset_id, amount_raw, updated_at)
                        VALUES (?,?,?,?)""", (aid, sid, str(raw), "2026-01-01"))

    return cfg, conn, aid, hold


def test_holdings_of_the_same_asset_are_summarised_as_one(evm):
    """Two flavours of ETH in two rows answer "what is this line", not "how
    much ETH do I have". The group line is the only place the second question
    is asked, and it must add both up."""
    cfg, conn, _, hold = evm
    hold("ethereum:native", 2 * 10**18)          # $5,000
    hold(WSTETH, 10**18)                         # $3,000
    hold(USDC, 1_000 * 10**6)                    # $1,000
    body = digest.render_state(conn, digest.collect(conn, PRICES), cfg).body
    assert "ETH" in body and "$8,000" in body
    # And the detail still names both, or the group total cannot be checked.
    assert "WSTETH" in body


def test_a_token_without_a_group_stands_alone(evm):
    """Silently folding an unlabelled token into a neighbouring group would
    misstate how much of that asset the portfolio holds."""
    cfg, conn, aid, hold = evm
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('ethereum:0xdead','ethereum','0xdead','ETHFI',18,'erc20',1)""")
    hold("ethereum:native", 10**18)
    hold("ethereum:0xdead", 10**18)
    data = digest.collect(conn, Prices({"ethereum:native": 2500.0,
                                        "ethereum:0xdead": 2000.0}))
    body = digest.render_state(conn, data, cfg).body
    assert "ETHFI" in body
    groups = [line.split()[0] for line in body.splitlines() if line.startswith("ETH")]
    assert "ETHFI" in groups, "a token named like ETH is not ETH"


def test_dust_is_counted_but_not_listed(evm):
    """Four addresses hold ETH worth fractions of a cent. A line each pushes
    the real money off the first screen, and dropping them silently would make
    the group totals disagree with the sum of the rows."""
    cfg, conn, _, hold = evm
    hold("ethereum:native", 10**18)
    hold(WSTETH, 10**11)                          # $0.0003
    body = digest.render_state(conn, digest.collect(conn, PRICES), cfg).body
    assert "under $1" in body
    assert "WSTETH" not in body, "dust earns a count, not a row"


def test_slivers_are_merged_into_one_other_group(evm):
    """A group worth a tenth of a percent next to the total is noise. Merging
    needs two of them: renaming a single group to "other" hides its name and
    saves nothing."""
    cfg, conn, aid, hold = evm
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('ethereum:0xaaa','ethereum','0xaaa','AAA',18,'erc20',1)""")
    hold("ethereum:native", 100 * 10**18)         # $250,000
    hold(USDC, 10 * 10**6)                        # $10
    hold("ethereum:0xaaa", 10**18)                # $20
    data = digest.collect(conn, Prices({"ethereum:native": 2500.0, USDC: 1.0,
                                        "ethereum:0xaaa": 20.0}))
    body = digest.render_state(conn, data, cfg).body
    assert "other" in body
    assert "AAA" in body and "USDC" in body, "merged, not dropped"


def test_a_days_yield_is_one_line_per_asset(setup):
    """The vaults accrue every tick: ninety-six events a day for one holding.
    Ninety-six lines saying "vault yield" are unreadable, and most of them
    round to zero - the day's total for that asset is the only useful number."""
    cfg, conn, aid, sid = setup
    for i in range(30):
        conn.execute("""INSERT INTO events(chain, address_id, asset_id, uid, kind,
                                           amount_raw, detail, ts)
                        VALUES ('bitcoin',?,?,?,'accrual','1000','vault yield',?)""",
                     (aid, sid, f"u{i}", datetime.now(timezone.utc).isoformat()))
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0}))
    body = digest.render(conn, data, cfg).body
    assert body.count("vault") == 1
    assert "0.0003" in body, "thirty accruals of 1000 sats are 0.0003 BTC"


def test_a_tiny_accrual_never_prints_as_zero(setup):
    """A wei of an 18-decimal token trimmed to six places reads "0", and a
    digest line saying an asset earned zero looks like a broken watcher rather
    than a tiny yield."""
    cfg, conn, aid, _ = setup
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('ethereum:0xa','ethereum','0xa','aWETH',18,'erc20',1)""")
    wid = conn.execute("SELECT id FROM assets WHERE symbol='aWETH'").fetchone()[0]
    conn.execute("""INSERT INTO events(chain, address_id, asset_id, uid, kind,
                                       amount_raw, detail, ts)
                    VALUES ('ethereum',?,?,'u1','accrual','1400000000','vault yield',?)""",
                 (aid, wid, datetime.now(timezone.utc).isoformat()))
    body = digest.render(conn, digest.collect(conn, Prices({})), cfg).body
    line = [x for x in body.splitlines() if "aWETH" in x][0]
    assert "e-" in line, f"1.4e-9 rendered as {line!r}"


def test_an_exchange_balance_is_never_listed_among_the_wallets(evm):
    """A Hyperliquid balance is a claim on an exchange; ETH in an address is a
    coin. Listing them in one table invites reading the exchange's promise as
    money already held - and it is the first thing that stops being true when
    an exchange stops paying out."""
    cfg, conn, aid, hold = evm
    hold("ethereum:native", 10**18)
    usdc = conn.execute("SELECT id FROM assets WHERE asset_key=?", (USDC,)).fetchone()[0]
    conn.execute("""INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                          amount_raw, usd, extra, updated_at)
                    VALUES (?,'hyperliquid','spot:USDC',?,?,?,'{}',?)""",
                 (aid, usdc, str(500 * 10**8), 500.0, "2026-01-01"))
    body = digest.render_state(conn, digest.collect(conn, PRICES), cfg).body
    onchain, _, exchange = body.partition("HYPERLIQUID")
    assert "spot" in exchange and "spot" not in onchain
    assert exchange.count("ETH") == 0, "wallet holdings stay out of the exchange table"


def test_a_validator_is_on_chain_not_on_an_exchange(setup):
    """Staked ETH sits on the beacon chain. Filing it under the exchange that
    happens to be the other source of positions would move eighty thousand
    dollars to the wrong side of the report."""
    cfg, conn, aid, _ = setup
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('ethereum:native','ethereum',NULL,'ETH',18,'native',1)""")
    eth = conn.execute("SELECT id FROM assets WHERE asset_key='ethereum:native'").fetchone()[0]
    conn.execute("""INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                          amount_raw, usd, extra, updated_at)
                    VALUES (?,'beacon','validator:999999',?,?,NULL,'{}',?)""",
                 (aid, eth, str(32 * 10**18), "2026-01-01"))
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0,
                                        "ethereum:native": 2500.0}))
    body = digest.render_state(conn, data, cfg).body
    assert "val·999999" in body.partition("HYPERLIQUID")[0]


def test_the_daily_digest_does_not_repeat_the_whole_portfolio(evm):
    """The digest is read every morning; the breakdown can be asked for at any
    time with `pw portfolio`. A message that must be scrolled past thirty rows
    to reach the part about the day defeats its own purpose."""
    cfg, conn, _, hold = evm
    hold("ethereum:native", 10**18)
    hold(WSTETH, 10**18)
    data = digest.collect(conn, PRICES)
    digest_body = digest.render(conn, data, cfg).body
    assert "24h" in digest_body
    assert "WSTETH" not in digest_body, "per-holding rows belong to the state view"
    assert "ETH" in digest_body, "the instrument summary stays"


def test_the_portfolio_view_says_how_stale_the_quantities_are(evm):
    """Prices are refreshed on the spot, balances are whatever the last scan
    left. Without the timestamp the reader cannot tell a quiet portfolio from
    a watcher that stopped scanning three days ago."""
    cfg, conn, _, hold = evm
    hold("ethereum:native", 10**18)
    body = digest.render_state(conn, digest.collect(conn, PRICES), cfg).body
    assert "balances as of" in body and "prices now" in body


def test_a_move_between_our_own_wallets_is_not_counted_as_money_leaving(setup):
    """An internal transfer is recorded on both sides, and the 24h net scored
    anything that was not "in" as negative - so shuffling $10,000 between two
    of our own addresses printed net -$20,000.00. Marking a transfer internal
    exists precisely so it does not read as money moving."""
    cfg, conn, aid, sid = setup
    now = datetime.now(timezone.utc).isoformat()
    for i, direction in enumerate(("internal", "internal")):
        conn.execute("""INSERT INTO events(chain, scope, address_id, asset_id, tx_hash,
                                           uid, kind, direction, amount_raw, usd,
                                           counterparty, ts, status)
                        VALUES ('bitcoin','',?,?,?,?,'transfer',?,?,?,'them',?,'confirmed')""",
                     (aid, sid, f"0x{i}", f"u{i}", direction, str(10_000_000), 10_000.0, now))
    data = digest.collect(conn, Prices({"bitcoin:native": 77000.0}))
    body = digest.render(conn, data, cfg).body
    assert "net $+0.00" in body, body
    assert "2 internal" in body, "the moves still happened; they just net to nothing"


def test_a_token_dropped_from_the_whitelist_leaves_the_total(setup):
    """sync_config used to only ever set whitelisted=1, and collect never asked
    about the flag: a token removed from the YAML kept its last balance in the
    total forever, priced at today's rate, removable only by hand in SQLite."""
    cfg, conn, aid, _ = setup
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       kind, whitelisted)
                    VALUES ('ethereum:0xdead','ethereum','0xdead','GONE',18,'erc20',1)""")
    gone = conn.execute("SELECT id FROM assets WHERE symbol='GONE'").fetchone()[0]
    conn.execute("""INSERT INTO balances(address_id, asset_id, amount_raw, updated_at)
                    VALUES (?,?,?,?)""", (aid, gone, str(5 * 10 ** 18), "2026-01-01"))
    priced = Prices({"bitcoin:native": 77000.0, "ethereum:0xdead": 100.0})
    assert digest.collect(conn, priced)["total"] == pytest.approx(38500.0 + 500.0)

    dbmod.sync_config(conn, cfg)          # the YAML lists no tokens at all
    assert digest.collect(conn, priced)["total"] == pytest.approx(38500.0)


async def test_a_digest_nobody_received_is_not_recorded_as_the_new_baseline(setup):
    """router.send reports failures by returning them, never by raising, so the
    baseline was written whether or not anything was delivered. A Telegram
    outage at 9am then cost the day's digest *and* moved the number tomorrow's
    "change since yesterday" is measured against."""
    cfg, conn, _, _ = setup

    class Dead:
        channels = ["telegram"]

        async def send(self, msg):
            return {"telegram": "timeout"}

    _, results = await digest.send(cfg, conn, Dead(), Prices({"bitcoin:native": 77000.0}))
    assert results == {"telegram": "timeout"}
    assert dbmod.get_meta(conn, digest.LAST_TOTAL) is None
    assert dbmod.get_meta(conn, digest.LAST_DATE) is None, "due() must stay true"
    assert conn.execute("SELECT COUNT(*) FROM total_snapshots").fetchone()[0] == 0, \
        "the series records days that happened, and this one did not"


async def test_a_delivered_digest_does_record_the_baseline(setup):
    cfg, conn, _, _ = setup

    class Live:
        channels = ["telegram"]

        async def send(self, msg):
            return {"telegram": None}

    _, results = await digest.send(cfg, conn, Live(), Prices({"bitcoin:native": 77000.0}))
    assert results == {"telegram": None}
    assert dbmod.get_meta(conn, digest.LAST_TOTAL) == "38500.00"


async def test_each_delivered_digest_adds_a_point_to_the_total_series(setup):
    """`meta` holds one number and forgets yesterday's. A day of the portfolio's
    value that was never written down cannot be recovered from anywhere later,
    which is why the row is appended rather than overwritten - and why it counts
    the unpriced holdings beside it: a total that dipped because CoinGecko was
    silent must not read as money lost."""
    cfg, conn, _, _ = setup

    class Live:
        channels = ["telegram"]

        async def send(self, msg):
            return {"telegram": None}

    await digest.send(cfg, conn, Live(), Prices({"bitcoin:native": 77000.0}))
    await digest.send(cfg, conn, Live(), Prices({}))       # nobody quoted BTC

    rows = conn.execute("SELECT usd, unpriced FROM total_snapshots ORDER BY ts").fetchall()
    assert [(r["usd"], r["unpriced"]) for r in rows] == [(38500.0, 0), (0.0, 1)]


def _row(qty_raw, decimals, symbol, where, usd):
    return {"raw": qty_raw, "decimals": decimals, "symbol": symbol,
            "where": where, "usd": usd}


WIDEST = [
    _row(32_009_710_000_000_000_000, 18, "ETH", "val·999999", 78_700.0),
    _row(2_240_000_000_000_000_000, 18, "aArbwstETH", "sav-evm·arb", 6_846.0),
    _row(9_897_000_000_000_000, 18, "ETH", "sav-evm·eth", 24.0),
    _row(1_020_000_000_000_000_000, 18, "aEthLidoWETH", "sav-evm·eth", 8_135.0),
]


def test_a_table_is_cut_to_fit_the_screen():
    """Telegram wraps a <pre> it cannot fit rather than scrolling it sideways,
    and a wrapped row is worse than no table at all: the dollars end up on
    their own line under the wrong holding. Thirteen addresses and a
    twelve-character symbol built a 39-character line that folded on the
    phone."""
    lines = digest._table(WIDEST, width=34)
    assert lines and all(len(line) <= 34 for line in lines), lines
    assert len({len(line) for line in lines}) == 1, "a table has one width"


def test_the_chain_is_never_cut_off_a_place():
    """base and bsc differ in their last two characters. Trimming the tail of
    a "where" cell to save room would print the same string for two different
    chains, which is a wrong answer rather than a narrow one."""
    rows = [_row(10**18, 18, "ETH", "sav-evm·base", 84.0),
            _row(10**18, 18, "ETH", "sav-evm·bsc", 42.0)]
    lines = digest._table(rows, width=20)
    assert "·base" in lines[0] and "·bsc" in lines[1]


def test_a_table_that_cannot_fit_stays_readable():
    """The columns stop shrinking at a floor. Past it a symbol says nothing
    about what is held, so the wrap the trimming exists to avoid is the lesser
    loss - the table is sent as it is."""
    lines = digest._table(WIDEST, width=10)
    assert any(len(line) > 10 for line in lines)
    assert "aEthL…" in "\n".join(lines), "a symbol is still recognisable"


def _event(kind, symbol, raw, label, usd=None, detail="vault yield", decimals=18,
           asset_kind="erc20"):
    return {"kind": kind, "direction": "in", "amount_raw": str(raw), "usd": usd,
            "detail": detail, "ts": "2026-09-05T09:00:00+00:00",
            "label": label, "symbol": symbol, "decimals": decimals,
            "asset_kind": asset_kind}


def _borrow(usd=5000.0):
    """The two transfers one borrow produces: the cash, and the debt token
    minted alongside it. Both arrive, both are whitelisted, both are priced."""
    return [_event("transfer", "USDC", 5 * 10**9, "main", usd, decimals=6),
            _event("transfer", "variableDebtUSDC", 5 * 10**9, "main", usd,
                   decimals=6, asset_kind="debt")]


def test_a_borrow_is_not_a_day_of_earnings():
    """A debt token is minted to the borrower, so it arrives as an incoming
    ERC-20 transfer beside the cash it paid for. Counted at face value, a
    $5,000 loan read as "net +$10,000" - twice the cash, for a day on which
    net worth did not move at all."""
    summary, _ = digest._last24h(_borrow())
    assert "net $+0.00" in summary, summary


def test_a_repayment_is_not_a_day_of_losses():
    """The mirror: the debt token is burned, so it leaves as an outgoing
    transfer beside the cash. Signed naively that is -$10,000."""
    summary, _ = digest._last24h([dict(e, direction="out") for e in _borrow()])
    assert "net $+0.00" in summary, summary


def test_an_ordinary_transfer_keeps_its_plain_sign():
    """The debt rule must not leak into the common case: money in is income."""
    summary, _ = digest._last24h(
        [_event("transfer", "USDC", 5 * 10**9, "main", 5000.0, decimals=6)])
    assert "net $+5,000.00" in summary, summary


def test_the_24h_detail_fits_the_same_screen_as_the_tables():
    """It is the same kind of block - a <pre> behind a tap - so it folds in the
    same place. An anomaly is the row that finds that edge: it carries an
    address label where a yield row carries only dollars, and it appears on
    exactly the morning the digest has to be read carefully."""
    recent = [_event("accrual", "aArbwstETH", 3 * 10**8, "savings-evm", 0.36),
              _event("anomaly", "aEthLidoWETH", 1_020_000_000_000_000_000,
                     "electrum-cold", detail="", decimals=18)]
    _, detail = digest._last24h(recent, width=37)
    assert detail and all(len(line) <= 37 for line in detail), detail


def test_a_dollar_figure_is_never_shortened_to_fit():
    """The tail of a yield row is money, not a name: cutting it leaves the one
    number the line exists for unreadable, while a name survives being cut."""
    recent = [_event("accrual", "aArbwstETH", 5 * 10**17, "savings-evm", 1234.56)]
    _, detail = digest._last24h(recent, width=20)
    assert "$1,234.56" in detail[0], detail


def test_a_debt_is_never_folded_away_as_small_change():
    """The dust line hides holdings too small to earn a row. A debt is worth a
    negative number, which is smaller than any threshold - so a naive test folds
    a five-thousand-dollar loan into "+1 under $1" and subtracts it from the
    dust total, where nobody is looking."""
    rows = [_row(10**18, 18, "ETH", "sav-evm·eth", 4_000.0),
            _row(-5_000_000_000, 6, "variableDebtUSDC", "main·eth", -5_000.0),
            _row(10**15, 18, "ETH", "dust·eth", 0.40)]
    lines = digest._table(rows, dust_usd=1.0, width=60)
    body = "\n".join(lines)
    assert "variableDebt" in body or "-5" in body, "the loan earns its own row"
    assert "+1 under $1" in body, "only the forty cents is dust"


def _trade(pnl, detail="closed long 1.5 ETH @ 3,100   pnl +12.50"):
    row = _event("position_change", "ETH", 150000000, "hl-1", detail=detail, decimals=8)
    row["pnl_usd"] = pnl
    return row


def test_the_day_says_what_the_closed_trades_made():
    """The per-trade alerts went out hours ago and are scrolled past by morning.
    The one line that is read at 9am is where the day's result belongs."""
    summary, _ = digest._last24h([_trade(12.5), _trade(-4.4)])
    assert "2 closed, pnl $+8.10" in summary


def test_a_closed_trade_is_not_also_counted_as_a_position_change():
    """Both are the same row. Counting it twice would say "1 position change,
    1 closed" about one event and make the day look busier than it was."""
    summary, _ = digest._last24h([_trade(12.5)])
    assert "position change" not in summary

    both = digest._last24h([_trade(12.5), _event("position_change", "ETH", 1, "hl-1")])[0]
    assert "1 closed" in both and "1 position change" in both


def test_a_close_with_no_realised_figure_stays_a_position_change():
    """NULL means the venue was not asked or could not answer. Reading it as a
    zero-profit trade would put a made-up number into the day's total."""
    row = _event("position_change", "ETH", 150000000, "hl-1", detail="closed perp:ETH")
    row["pnl_usd"] = None
    summary, _ = digest._last24h([row])
    assert "closed, pnl" not in summary
    assert "1 position change" in summary
