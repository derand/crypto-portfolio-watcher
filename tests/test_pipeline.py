from datetime import datetime, timezone

from portfolio import config as cfgmod
from portfolio import db as dbmod
from portfolio import pipeline
from portfolio.chains.base import AddressState, Cursor, Target
from portfolio.models import (BalanceSnapshot, Direction, Position, Probe,
                              Transfer)

A1 = "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97"
A2 = "bc1q9qkjc8x853msxzsykp5qc5hjc0uav8ak4wwj5q"
THEM = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"

CFG = """
db_path: {db}
notify:
  telegram:
    enabled: false
addresses:
  - chain: bitcoin
    address: {a1}
    label: btc-1
    watch: [native]
  - chain: bitcoin
    address: {a2}
    label: btc-2
    watch: [native]
tokens: []
"""


class FakeRouter:
    """Records what would have been sent; can be told to fail."""

    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    @property
    def channels(self):
        return ["telegram"]

    async def send(self, msg):
        self.sent.append(msg)
        return {"telegram": self.fail}


class FakeChain:
    """Scripted adapter: one (balance, transfers) step per scan, per address."""

    chain = "bitcoin"

    def __init__(self, script):
        self.script = script          # address -> list of (sats, [Transfer])
        self.step = {}
        self.fetches = 0

    def _current(self, address):
        i = min(self.step.get(address, 0), len(self.script[address]) - 1)
        return self.script[address][i]

    def scopes(self, t):
        return [""]

    async def probe(self, t, scope, cursor):
        address = t.address
        sats, transfers = self._current(address)
        # Describes the chain state only: repeating a step must look unchanged,
        # exactly as a real probe would.
        marker = f"{sats}:{sorted(f'{t.tx_hash}@{t.block_height}' for t in transfers)}"
        return Probe(changed=marker != cursor.last_marker, marker=marker)

    async def fetch(self, t, scope, cursor, probe):
        address = t.address
        self.fetches += 1
        sats, transfers = self._current(address)
        self.step[address] = self.step.get(address, 0) + 1
        state = AddressState(
            balances=[BalanceSnapshot(asset_key="bitcoin:native", amount_raw=sats,
                                      decimals=8, symbol="BTC")],
            cursor=Cursor(last_marker=probe.marker, last_item=f"tip{self.step[address]}"))
        if not cursor.is_fresh:
            state.transfers = list(transfers)
        return state


def transfer(txid, amount, direction=Direction.IN, counterparty=THEM, height=800000):
    return Transfer(tx_hash=txid, uid=txid, asset_key="bitcoin:native",
                    amount_raw=amount, direction=direction, counterparty=counterparty,
                    block_height=height, ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    symbol="BTC", decimals=8)


def setup(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(CFG.format(db=tmp_path / "t.db", a1=A1, a2=A2))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    return cfg, conn


def events(conn):
    return conn.execute(
        "SELECT tx_hash, kind, direction, amount_raw, status FROM events ORDER BY id").fetchall()


async def test_first_run_records_baseline_without_notifying(tmp_path):
    cfg, conn = setup(tmp_path)
    chain = FakeChain({A1: [(100_000, [])], A2: [(0, [])]})
    router = FakeRouter()
    res = await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    assert res.new_events == 0
    assert router.sent == [], "day one must not alert"
    assert conn.execute("SELECT amount_raw FROM balances").fetchone()[0] == "100000"


async def test_unchanged_probe_skips_the_expensive_fetch(tmp_path):
    cfg, conn = setup(tmp_path)
    chain = FakeChain({A1: [(100_000, [])], A2: [(0, [])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    before = chain.fetches
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    assert chain.fetches == before, "marker unchanged: no transaction list fetched"


async def test_new_transfer_produces_one_event_and_one_message(tmp_path):
    cfg, conn = setup(tmp_path)
    chain = FakeChain({A1: [(100_000, []), (150_000, [transfer("t1", 50_000)])],
                       A2: [(0, [])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    res = await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    assert res.new_events == 1
    assert len(router.sent) == 1
    assert "0.0005" in router.sent[0].body
    assert conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE status='sent'").fetchone()[0] == 1


async def test_the_same_transfer_seen_again_is_not_re_alerted(tmp_path):
    cfg, conn = setup(tmp_path)
    t = transfer("t1", 50_000)
    chain = FakeChain({A1: [(100_000, []), (150_000, [t]), (150_000, [t])], A2: [(0, [])]})
    router = FakeRouter()
    for _ in range(3):
        await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    assert len([e for e in events(conn) if e["kind"] == "transfer"]) == 1
    assert len(router.sent) == 1


async def test_pending_then_confirmed_updates_instead_of_duplicating(tmp_path):
    cfg, conn = setup(tmp_path)
    pending = transfer("t1", 50_000, height=None)
    confirmed = transfer("t1", 50_000, height=800001)
    chain = FakeChain({A1: [(100_000, []), (150_000, [pending]), (150_000, [confirmed])],
                       A2: [(0, [])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    assert events(conn)[0]["status"] == "pending"
    res = await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    rows = events(conn)
    assert len(rows) == 1, "the confirmation updates the row, it does not add one"
    assert rows[0]["status"] == "confirmed"
    assert res.confirmed == 1
    assert len(router.sent) == 2, "arrival and confirmation are both worth saying"


async def test_transfer_between_our_own_addresses_is_internal(tmp_path):
    cfg, conn = setup(tmp_path)
    chain = FakeChain({
        A1: [(100_000, []), (50_000, [transfer("t1", 50_000, Direction.OUT, A2)])],
        A2: [(0, []), (50_000, [transfer("t1", 50_000, Direction.IN, A1)])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    assert {e["direction"] for e in events(conn)} == {"internal"}


async def test_failed_delivery_is_retried_on_the_next_tick(tmp_path):
    """The crash-safety promise: a queued notification survives a dead channel."""
    cfg, conn = setup(tmp_path)
    chain = FakeChain({A1: [(100_000, []), (150_000, [transfer("t1", 50_000)])],
                       A2: [(0, [])]})
    broken = FakeRouter(fail="telegram down")
    await pipeline.scan_once(cfg, conn, broken, {"bitcoin": chain})
    await pipeline.scan_once(cfg, conn, broken, {"bitcoin": chain})
    assert conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0] == 1

    working = FakeRouter()
    await pipeline.scan_once(cfg, conn, working, {"bitcoin": chain})
    assert len(working.sent) == 1, "the missed alert is delivered late, not lost"
    assert conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0] == 0


async def test_unexplained_balance_move_raises_an_anomaly(tmp_path):
    """Balance jumped but no transfer accounts for it: never fail silently."""
    cfg, conn = setup(tmp_path)
    chain = FakeChain({A1: [(100_000, []), (900_000, [])], A2: [(0, [])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})
    anomalies = [e for e in events(conn) if e["kind"] == "anomaly"]
    assert len(anomalies) == 1
    assert anomalies[0]["amount_raw"] == "800000"
    assert router.sent[0].severity.value == "high"


class FakeMultiChain:
    """One address living on two networks, each with its own cursor."""

    chain = "evm"

    def __init__(self, script):
        self.script = script          # (address, scope) -> list of (wei, [Transfer])
        self.step = {}

    def scopes(self, t):
        return list(t.chains)

    def _current(self, key):
        i = min(self.step.get(key, 0), len(self.script[key]) - 1)
        return self.script[key][i]

    async def probe(self, t, scope, cursor):
        wei, transfers = self._current((t.address, scope))
        marker = f"{wei}:{sorted(x.uid for x in transfers)}"
        return Probe(changed=marker != cursor.last_marker, marker=marker)

    async def fetch(self, t, scope, cursor, probe):
        key = (t.address, scope)
        wei, transfers = self._current(key)
        self.step[key] = self.step.get(key, 0) + 1
        state = AddressState(
            balances=[BalanceSnapshot(asset_key=f"{scope}:native", amount_raw=wei,
                                      decimals=18, symbol="ETH", fee_bearing=True)],
            cursor=Cursor(last_marker=probe.marker, last_item=f"tip{self.step[key]}"))
        if not cursor.is_fresh:
            state.transfers = list(transfers)
        return state


EVM = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
EVM_CFG = """
db_path: {db}
notify:
  telegram:
    enabled: false
addresses:
  - chain: evm
    address: "{a}"
    label: main
    chains: [ethereum, arbitrum]
    watch: [native]
tokens: []
"""


def setup_evm(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text(EVM_CFG.format(db=tmp_path / "e.db", a=EVM))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    return cfg, conn


def wei_transfer(uid, amount, scope="ethereum", direction=Direction.IN):
    return Transfer(tx_hash=uid.split(":")[0], uid=uid, asset_key=f"{scope}:native",
                    amount_raw=amount, direction=direction, counterparty=THEM,
                    block_height=100, ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    symbol="ETH", decimals=18)


async def test_one_address_is_tracked_per_network_independently(tmp_path):
    cfg, conn = setup_evm(tmp_path)
    chain = FakeMultiChain({
        (EVM, "ethereum"): [(10**18, []), (2 * 10**18, [wei_transfer("0xa:ext", 10**18)])],
        (EVM, "arbitrum"): [(5 * 10**17, [])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"evm": chain})
    res = await pipeline.scan_once(cfg, conn, router, {"evm": chain})

    assert res.new_events == 1
    scopes = {r["scope"] for r in conn.execute("SELECT DISTINCT scope FROM cursors")}
    assert scopes == {"ethereum", "arbitrum"}
    assert conn.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 2
    assert "main/ethereum" in router.sent[0].body


async def test_gas_drift_on_the_native_coin_is_absorbed_not_alerted(tmp_path):
    """An approve or a failed swap burns ETH with no transfer to show for it.
    Alerting on that would fire on every contract interaction."""
    cfg, conn = setup_evm(tmp_path)
    gas = 3 * 10**14
    chain = FakeMultiChain({
        (EVM, "ethereum"): [(10**18, []), (10**18 - gas, [])],
        (EVM, "arbitrum"): [(0, [])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"evm": chain})
    await pipeline.scan_once(cfg, conn, router, {"evm": chain})

    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert kinds == ["accrual"]
    assert router.sent == [], "fees belong in the digest, not in a notification"


async def test_native_balance_growing_unexplained_still_raises_an_anomaly(tmp_path):
    """Absorbing losses as fees must not also silence money appearing."""
    cfg, conn = setup_evm(tmp_path)
    chain = FakeMultiChain({
        (EVM, "ethereum"): [(10**18, []), (9 * 10**18, [])],
        (EVM, "arbitrum"): [(0, [])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"evm": chain})
    await pipeline.scan_once(cfg, conn, router, {"evm": chain})

    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert kinds == ["anomaly"]
    assert router.sent[0].severity.value == "high"


class FakeSource:
    """Scripted position source: one snapshot per scan."""

    name = "hyperliquid"

    def __init__(self, script):
        self.script = script
        self.step = 0

    async def fetch(self, t):
        snapshot = self.script[min(self.step, len(self.script) - 1)]
        self.step += 1
        marker = "|".join(f"{p.key}={p.amount_raw}"
                          for p in snapshot if p.key != "account")
        return list(snapshot), marker


HL_CFG = """
db_path: {db}
notify:
  telegram:
    enabled: false
thresholds:
  liq_distance_pct: 15.0
addresses:
  - chain: evm
    address: "{a}"
    label: hl-1
    watch: [hyperliquid]
tokens: []
"""


def setup_hl(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(HL_CFG.format(db=tmp_path / "h.db", a=EVM))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    return cfg, conn


def perp(coin="ETH", size=150000000, liq_pct=None, side="long"):
    extra = {"side": side, "entry_px": "3000.0", "liq_px": "2400.0",
             "mark_px": "3000.0", "leverage": "20"}
    if liq_pct is not None:
        extra["liq_distance_pct"] = f"{liq_pct:.2f}"
    return Position(protocol="hyperliquid", key=f"perp:{coin}", symbol=coin,
                    amount_raw=size, decimals=8, asset_key=f"hyperliquid:{coin}",
                    usd=4500.0, extra=extra)


def spot(coin="USDC", amount=4782113549):
    return Position(protocol="hyperliquid", key=f"spot:{coin}", symbol=coin,
                    amount_raw=amount, decimals=8, asset_key=f"hyperliquid:{coin}")


def account(value=100000000000):
    return Position(protocol="hyperliquid", key="account", symbol="USD",
                    amount_raw=value, decimals=8, asset_key="hyperliquid:account",
                    usd=1000.0, extra={"withdrawable": "900.0"})


async def run_hl(cfg, conn, router, src):
    return await pipeline.scan_once(cfg, conn, router, {}, {"hyperliquid": src})


async def test_first_hyperliquid_scan_is_a_silent_baseline(tmp_path):
    cfg, conn = setup_hl(tmp_path)
    src = FakeSource([[account(), spot(), perp()]])
    router = FakeRouter()
    await run_hl(cfg, conn, router, src)
    assert router.sent == []
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 3


async def test_opening_a_position_alerts_with_its_terms(tmp_path):
    cfg, conn = setup_hl(tmp_path)
    src = FakeSource([[account(), spot()], [account(), spot(), perp()]])
    router = FakeRouter()
    await run_hl(cfg, conn, router, src)
    await run_hl(cfg, conn, router, src)
    body = router.sent[0].body
    assert "opened long 1.5 ETH" in body
    assert "20x" in body and "liq 2,400.00" in body


async def test_resizing_and_closing_a_position_each_alert_once(tmp_path):
    cfg, conn = setup_hl(tmp_path)
    src = FakeSource([[account(), perp(size=150000000)],
                      [account(), perp(size=250000000)],
                      [account(), perp(size=250000000)],   # unchanged tick
                      [account()]])                        # closed
    router = FakeRouter()
    for _ in range(4):
        await run_hl(cfg, conn, router, src)
    bodies = [m.body for m in router.sent]
    assert len(bodies) == 2, "the unchanged tick must stay quiet"
    assert "1.5 → 2.5" in bodies[0]
    assert "closed perp:ETH" in bodies[1]
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE position_key LIKE 'perp:%'"
                        ).fetchone()[0] == 0


async def test_account_value_drift_never_alerts(tmp_path):
    """Unrealised PnL moves the account value constantly; it is digest material."""
    cfg, conn = setup_hl(tmp_path)
    src = FakeSource([[account(100000000000), spot()],
                      [account(123456789000), spot(amount=5000000000)]])
    router = FakeRouter()
    await run_hl(cfg, conn, router, src)
    await run_hl(cfg, conn, router, src)
    kinds = sorted(r["kind"] for r in conn.execute("SELECT kind FROM events"))
    assert kinds == ["accrual", "position_change"]
    assert "spot:USDC" in router.sent[0].body
    assert "account" not in router.sent[0].body


async def test_liquidation_warning_fires_and_then_stays_quiet(tmp_path):
    cfg, conn = setup_hl(tmp_path)
    src = FakeSource([[account(), perp(liq_pct=40.0)],
                      [account(), perp(size=150000001, liq_pct=8.3)],
                      [account(), perp(size=150000002, liq_pct=8.4)]])
    router = FakeRouter()
    for _ in range(3):
        await run_hl(cfg, conn, router, src)
    warnings = [m for m in router.sent if m.severity.value == "high"]
    assert len(warnings) == 1, "one warning per percentage point, not per tick"
    assert "within 8.3% of liquidation" in warnings[0].body


async def test_liquidation_warning_repeats_when_risk_worsens(tmp_path):
    cfg, conn = setup_hl(tmp_path)
    src = FakeSource([[account(), perp(liq_pct=40.0)],
                      [account(), perp(size=150000001, liq_pct=8.3)],
                      [account(), perp(size=150000002, liq_pct=4.1)]])
    router = FakeRouter()
    for _ in range(3):
        await run_hl(cfg, conn, router, src)
    warned = [m.body for m in router.sent if m.severity.value == "high"]
    assert len(warned) == 2
    assert "4.1%" in warned[1]


class FakePrices:
    def __init__(self, table):
        self.table = table
        self.refreshed = 0

    async def refresh(self, assets, ttl_minutes=None):
        self.refreshed += 1

    def usd(self, key):
        return self.table.get(key)

    def value(self, key, amount_raw, decimals):
        price = self.table.get(key)
        return None if price is None else amount_raw / 10 ** decimals * price


async def test_dust_transfer_is_recorded_but_not_announced(tmp_path):
    """The threshold exists so gas-sized movements do not wake anyone up."""
    cfg, conn = setup(tmp_path)
    cfg.thresholds.notify_usd = 1.0
    dust = transfer("0xdust", 1000)                     # 0.00001 BTC
    chain = FakeChain({A1: [(100_000, []), (101_000, [dust])], A2: [(0, [])]})
    router = FakeRouter()
    prices = FakePrices({"bitcoin:native": 77000.0})    # 0.00001 BTC = $0.77
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)
    res = await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)

    assert res.new_events == 1 and res.below_threshold == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert router.sent == [], "recorded for the digest, not pushed to the phone"


async def test_transfer_above_the_threshold_is_announced_with_its_value(tmp_path):
    cfg, conn = setup(tmp_path)
    cfg.thresholds.notify_usd = 1.0
    big = transfer("0xbig", 5_000_000)                  # 0.05 BTC
    chain = FakeChain({A1: [(100_000, []), (5_100_000, [big])], A2: [(0, [])]})
    router = FakeRouter()
    prices = FakePrices({"bitcoin:native": 77000.0})
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)

    assert len(router.sent) == 1
    assert "$3,850.00" in router.sent[0].body


async def test_unpriced_transfer_always_speaks_up(tmp_path):
    """Not knowing what something is worth must never silence it."""
    cfg, conn = setup(tmp_path)
    cfg.thresholds.notify_usd = 1000.0
    t = transfer("0xunknown", 1000)
    chain = FakeChain({A1: [(100_000, []), (101_000, [t])], A2: [(0, [])]})
    router = FakeRouter()
    prices = FakePrices({})                             # nothing has a price
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)
    res = await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)

    assert res.below_threshold == 0
    assert len(router.sent) == 1


async def test_a_broken_price_service_does_not_stop_the_scan(tmp_path):
    """Prices are advisory. Losing them must never stop the watching."""
    cfg, conn = setup(tmp_path)

    class Exploding(FakePrices):
        async def refresh(self, assets, ttl_minutes=None):
            raise RuntimeError("coingecko is down")

    chain = FakeChain({A1: [(100_000, []), (150_000, [transfer("0xa", 50_000)])],
                       A2: [(0, [])]})
    router = FakeRouter()
    prices = Exploding({})
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)
    res = await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain}, prices=prices)

    assert res.new_events == 1
    assert len(router.sent) == 1


class FakeVaultChain:
    """One address holding a share token whose redemption value climbs."""

    chain = "evm"

    def __init__(self, amounts):
        self.amounts = amounts
        self.i = 0

    def scopes(self, t):
        return ["ethereum"]

    def _now(self):
        return self.amounts[min(self.i, len(self.amounts) - 1)]

    async def probe(self, t, scope, cursor):
        marker = str(self._now())
        return Probe(changed=marker != cursor.last_marker, marker=marker)

    async def fetch(self, t, scope, cursor, probe):
        amount = self._now()
        self.i += 1
        return AddressState(
            balances=[BalanceSnapshot(asset_key="ethereum:0xvault", amount_raw=amount,
                                      decimals=18, symbol="SHARE",
                                      yield_bearing=True)],
            cursor=Cursor(last_marker=probe.marker))


async def test_growing_share_value_is_yield_not_an_anomaly(tmp_path):
    """A vault share and an Aave aToken both grow with no transfer behind it.
    Alerting on that fires every few hours for as long as the position exists."""
    cfg, conn = setup_evm(tmp_path)
    chain = FakeVaultChain([10**18, 10**18 + 3 * 10**14])
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"evm": chain})
    await pipeline.scan_once(cfg, conn, router, {"evm": chain})

    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert kinds == ["accrual"]
    assert router.sent == [], "yield belongs in the digest, not in a notification"


async def test_an_accrual_records_what_the_yield_was_worth(tmp_path):
    """The digest aggregates a day of accruals per asset and prints dollars
    beside the quantity. With no value on the row that column is empty forever,
    and "yield in 5 assets" stays a sentence nobody can size."""
    cfg, conn = setup_evm(tmp_path)
    chain = FakeVaultChain([10**18, 10**18 + 3 * 10**14])
    router = FakeRouter()
    prices = FakePrices({"ethereum:0xvault": 2000.0})   # 0.0003 share = $0.60
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)

    row = conn.execute("SELECT kind, usd FROM events").fetchone()
    assert row["kind"] == "accrual"
    assert abs(row["usd"] - 0.60) < 1e-9, "the residual's worth, not the holding's"


async def test_fees_are_valued_negatively(tmp_path):
    """Gas is an accrual too, and the digest tells earnings from losses by the
    sign. A fee recorded as a positive dollar figure reads as income."""
    cfg, conn = setup_evm(tmp_path)
    gas = 3 * 10**14
    chain = FakeMultiChain({
        (EVM, "ethereum"): [(10**18, []), (10**18 - gas, [])],
        (EVM, "arbitrum"): [(0, [])]})
    router = FakeRouter()
    prices = FakePrices({"ethereum:native": 2000.0})    # 0.0003 ETH = $0.60
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)

    row = conn.execute("SELECT kind, usd FROM events").fetchone()
    assert row["kind"] == "accrual"
    assert abs(row["usd"] + 0.60) < 1e-9, "a fee is negative, not income"


async def test_an_asset_registered_before_its_index_gets_it_later(tmp_path):
    """Hyperliquid spot rows already in the database were written without the
    venue's token index, and the row is only created once. Without a backfill
    such a holding stays unpriceable for as long as it is held."""
    from portfolio import pipeline
    from portfolio import db as dbmod
    conn = dbmod.connect(tmp_path / "b.db")
    dbmod.init(conn)
    first = pipeline._asset_id(conn, "hyperliquid:UBTC", "UBTC", 8)
    assert conn.execute("SELECT contract FROM assets WHERE id=?",
                        (first,)).fetchone()["contract"] is None
    again = pipeline._asset_id(conn, "hyperliquid:UBTC", "UBTC", 8, "197")
    assert again == first, "the asset is the same one, not a second row"
    assert conn.execute("SELECT contract FROM assets WHERE id=?",
                        (first,)).fetchone()["contract"] == "197"


async def test_one_broken_provider_does_not_hold_back_everyone_elses_alerts(tmp_path):
    """A tick is probe → fetch → diff → events → notify for every address, and
    the notify step runs once at the end. An exception the scope handler does
    not expect skips every remaining address *and* that delivery, so a single
    provider answering 403 - Alchemy does exactly that for a network merely
    disabled in the dashboard - silences alerts for addresses it has nothing to
    do with, on every tick, until someone reads the log."""
    from portfolio.retry import Permanent

    class Broken(FakeChain):
        async def probe(self, t, scope, cursor):
            if t.address == A1:
                raise Permanent("alchemy 403: network disabled in the dashboard")
            return await FakeChain.probe(self, t, scope, cursor)

    cfg, conn = setup(tmp_path)
    chain = Broken({A1: [(100_000, [])],
                    A2: [(0, []), (50_000, [transfer("0xb", 50_000)])]})
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})       # baseline
    res = await pipeline.scan_once(cfg, conn, router, {"bitcoin": chain})

    assert len(router.sent) == 1, "the healthy address still reaches the chat"
    assert any("btc-1" in f for f in res.failed), "and the broken one is reported"


async def test_a_position_drifting_by_cents_is_recorded_but_not_announced(tmp_path):
    """Hyperliquid settles funding into the spot balance, so spot:USDC moves a
    fraction of a cent every few minutes. The position path used to queue every
    change unconditionally - the transfer path has had the USD threshold since
    the start - which turned an open perp into a notification every tick. The
    threshold measures the change, not the position, so a real deposit is still
    worth saying out loud."""
    cfg, conn = setup_hl(tmp_path)
    cfg.thresholds.notify_usd = 1.0
    prices = FakePrices({"hyperliquid:USDC": 1.0, "hyperliquid:ETH": 3000.0})
    src = FakeSource([[account(), spot(amount=10_000_000_000)],      # 100.00 USDC
                      [account(), spot(amount=9_998_770_000)],       # funding: -1.2¢
                      [account(), spot(amount=59_998_770_000)]])     # a $500 deposit
    router = FakeRouter()

    await pipeline.scan_once(cfg, conn, router, {}, {"hyperliquid": src}, prices)
    quiet = await pipeline.scan_once(cfg, conn, router, {}, {"hyperliquid": src}, prices)
    assert quiet.new_events == 1, "the drift is still recorded for the digest"
    assert quiet.below_threshold == 1
    assert router.sent == [], "a 1.2-cent funding payment must not ring"

    loud = await pipeline.scan_once(cfg, conn, router, {}, {"hyperliquid": src}, prices)
    assert loud.new_events == 1 and len(router.sent) == 1, "$500 still speaks"


async def test_a_closed_position_is_printed_in_its_own_decimals(tmp_path):
    """The closing line formatted every amount at 8 decimals. That is right for
    Bitcoin and for Hyperliquid's scale and wrong by ten orders of magnitude
    for anything holding wei - an exited validator read as 320000000000 ETH."""
    cfg, conn = setup_hl(tmp_path)
    staked = Position(protocol="hyperliquid", key="validator:1", symbol="ETH",
                      amount_raw=32 * 10 ** 18, decimals=18,
                      asset_key="hyperliquid:ETH")
    src = FakeSource([[account(), staked], [account()]])
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {}, {"hyperliquid": src})
    await pipeline.scan_once(cfg, conn, router, {}, {"hyperliquid": src})

    detail = conn.execute("SELECT detail FROM events WHERE detail LIKE 'closed%'").fetchone()[0]
    assert "32" in detail and "320000000000" not in detail, detail


class FakeDebtChain:
    """One address owing a variable debt token. `owed` is what the token says;
    the snapshot carries it negated, the way the EVM adapter builds it."""

    chain = "evm"

    def __init__(self, owed):
        self.owed = owed
        self.i = 0

    def scopes(self, t):
        return ["ethereum"]

    def _now(self):
        return self.owed[min(self.i, len(self.owed) - 1)]

    async def probe(self, t, scope, cursor):
        marker = str(self._now())
        return Probe(changed=marker != cursor.last_marker, marker=marker)

    async def fetch(self, t, scope, cursor, probe):
        owed = self._now()
        self.i += 1
        return AddressState(
            balances=[BalanceSnapshot(asset_key="ethereum:0xdebt", amount_raw=-owed,
                                      decimals=6, symbol="variableDebtUSDC",
                                      debt=True)],
            cursor=Cursor(last_marker=probe.marker))


DEBT_PRICE = {"ethereum:0xdebt": 1.0}


async def test_a_debt_is_recorded_as_a_negative_balance(tmp_path):
    """The portfolio total is a sum. A debt stored positive is added to net
    worth instead of taken off it, and the number is wrong by twice the loan."""
    cfg, conn = setup_evm(tmp_path)
    router = FakeRouter()
    await pipeline.scan_once(cfg, conn, router, {"evm": FakeDebtChain([1_000_000_000])},
                             prices=FakePrices(DEBT_PRICE))
    assert conn.execute(
        "SELECT amount_raw FROM balances").fetchone()["amount_raw"] == "-1000000000"
    # The valued snapshot is what the digest's total is built from.
    assert conn.execute(
        "SELECT usd FROM balance_snapshots").fetchone()["usd"] == -1000.0


async def test_borrow_interest_does_not_ring(tmp_path):
    """A debt grows every block. Alerting on that fires every tick for as long
    as the loan exists - the exact failure the accrual channel exists for."""
    cfg, conn = setup_evm(tmp_path)
    router = FakeRouter()
    chain = FakeDebtChain([1_000_000_000, 1_000_100_000])   # +0.10 USDC of interest
    prices = FakePrices(DEBT_PRICE)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)

    row = conn.execute("SELECT kind, detail, usd FROM events").fetchone()
    assert row["kind"] == "accrual"
    assert row["detail"] == "borrow interest"
    assert abs(row["usd"] + 0.10) < 1e-9, "interest costs money; the sign says so"
    assert router.sent == []


async def test_borrowing_alerts_and_names_itself(tmp_path):
    """Interest and a new loan move the same number, so nothing but its size
    tells them apart. Silencing the balance outright - the way a vault share is
    silenced - would let a six-figure borrow pass without a word."""
    cfg, conn = setup_evm(tmp_path)
    router = FakeRouter()
    chain = FakeDebtChain([1_000_000_000, 1_500_000_000])   # +500 USDC borrowed
    prices = FakePrices(DEBT_PRICE)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)

    row = conn.execute("SELECT kind, detail, amount_raw FROM events").fetchone()
    assert row["kind"] == "position_change"
    assert row["detail"] == "borrowed 500 variableDebtUSDC"
    assert row["amount_raw"] == "-500000000", "the debt grew; the sign is the point"
    assert len(router.sent) == 1


async def test_repaying_is_told_from_borrowing_by_the_sign(tmp_path):
    """Both cross the threshold and both are position changes; a message that
    called a repayment a borrow would be read as money going the wrong way."""
    cfg, conn = setup_evm(tmp_path)
    router = FakeRouter()
    chain = FakeDebtChain([1_000_000_000, 400_000_000])     # 600 USDC repaid
    prices = FakePrices(DEBT_PRICE)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)
    await pipeline.scan_once(cfg, conn, router, {"evm": chain}, prices=prices)

    row = conn.execute("SELECT detail FROM events").fetchone()
    assert row["detail"] == "repaid 600 variableDebtUSDC"


class FakeRangeSource:
    """A concentrated-liquidity position that moves in and out of its range."""

    name = "univ3"

    def __init__(self, states):
        self.states = states          # list of "true"/"false"
        self.i = 0

    async def fetch(self, t):
        state = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        p = Position(
            protocol="univ3", key="ethereum:4242:0", symbol="WETH",
            amount_raw=10 ** 18, decimals=18, asset_key="ethereum:native",
            accrues=True,
            extra={"venue": "uniswap-v3", "token_id": "4242", "tick": "-14377",
                   "in_range": state})
        return [p], f"{state}:{self.i}"


UNIV3_CFG = EVM_CFG.replace("watch: [native]", "watch: [native, univ3]")


def setup_univ3(tmp_path):
    p = tmp_path / "u.yaml"
    p.write_text(UNIV3_CFG.format(db=tmp_path / "u.db", a=EVM))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    conn = dbmod.connect(cfg.db_path)
    dbmod.init(conn)
    dbmod.sync_config(conn, cfg)
    return cfg, conn


async def test_falling_out_of_range_says_so_once(tmp_path):
    """The one discrete thing that happens to a range position: the liquidity
    stops earning. Everything else about it drifts, so this is the only part
    worth a message - and repeating it every tick would bury it."""
    cfg, conn = setup_univ3(tmp_path)
    router = FakeRouter()
    src = FakeRangeSource(["true", "false", "false"])
    for _ in range(3):
        await pipeline.scan_once(cfg, conn, router, {}, {"univ3": src})

    said = [m.body for m in router.sent]
    assert len(said) == 1, said
    assert "left its range" in said[0] and "4242" in said[0]


async def test_coming_back_into_range_is_worth_saying_too(tmp_path):
    cfg, conn = setup_univ3(tmp_path)
    router = FakeRouter()
    src = FakeRangeSource(["true", "false", "true"])
    for _ in range(3):
        await pipeline.scan_once(cfg, conn, router, {}, {"univ3": src})

    said = [m.body for m in router.sent]
    assert len(said) == 2
    assert "left its range" in said[0]
    assert "back in range" in said[1]


async def test_a_position_first_seen_out_of_range_does_not_alert(tmp_path):
    """Day one records what is there; it does not announce a state that has
    been true for months. The alert is about the transition."""
    cfg, conn = setup_univ3(tmp_path)
    router = FakeRouter()
    src = FakeRangeSource(["false", "false"])
    for _ in range(2):
        await pipeline.scan_once(cfg, conn, router, {}, {"univ3": src})
    assert router.sent == []


async def test_the_drifting_amount_of_a_range_position_stays_silent(tmp_path):
    """Its composition changes with every trade in the pool, with no transfer
    behind it. That is digest material; alerting on it fires for as long as the
    position exists."""
    cfg, conn = setup_univ3(tmp_path)
    router = FakeRouter()

    class Drifting(FakeRangeSource):
        async def fetch(self, t):
            positions, _ = await super().fetch(t)
            positions[0].amount_raw = 10 ** 18 + self.i * 10 ** 15
            return positions, f"drift:{self.i}"

    src = Drifting(["true", "true", "true"])
    for _ in range(3):
        await pipeline.scan_once(cfg, conn, router, {}, {"univ3": src})

    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert kinds and set(kinds) == {"accrual"}
    assert router.sent == []
