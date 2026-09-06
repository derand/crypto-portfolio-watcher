import json

import httpx

from portfolio.chains.base import Target
from portfolio.protocols.hyperliquid import HyperliquidSource

ME = "0x8e11d5c0a1b2c3d4e5f60718293a4b5c6d7e8f90"
TARGET = Target(address=ME, label="hl-1", watch=frozenset({"hyperliquid"}))


def source(perp, spot, stake=None):
    calls = []
    payloads = {"clearinghouseState": perp, "spotClearinghouseState": spot,
                "delegatorSummary": stake if stake is not None else stake_summary()}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["type"])
        return httpx.Response(200, json=payloads[body["type"]])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HyperliquidSource(url="http://hl/info", client=client), calls


def perp_state(positions=(), account="1000.0", withdrawable="900.0"):
    return {"marginSummary": {"accountValue": account, "totalNtlPos": "0.0",
                              "totalRawUsd": account, "totalMarginUsed": "0.0"},
            "withdrawable": withdrawable, "assetPositions": list(positions),
            "time": 1788394273681}


def position(coin="ETH", szi="1.5", value="4500.0", entry="3000.0",
             liq="2400.0", pnl="12.5", leverage=20):
    return {"type": "oneWay", "position": {
        "coin": coin, "szi": szi, "positionValue": value, "entryPx": entry,
        "liquidationPx": liq, "unrealizedPnl": pnl, "marginUsed": "225.0",
        "leverage": {"type": "cross", "value": leverage},
        "cumFunding": {"allTime": "1.0", "sinceOpen": "0.5", "sinceChange": "0.1"}}}


def stake_summary(delegated="0.0", undelegated="0.0", pending="0.0", n=0):
    return {"delegated": delegated, "undelegated": undelegated,
            "totalPendingWithdrawal": pending, "nPendingWithdrawals": n}


def spot_state(*coins):
    return {"balances": [{"coin": c, "token": i, "total": t, "hold": "0.0",
                          "entryNtl": "0.0"} for i, (c, t) in enumerate(coins)]}


async def test_spot_balances_parsed_and_empty_rows_dropped():
    """Hyperliquid lists every token you ever touched, most of them at zero."""
    s, calls = source(perp_state(), spot_state(("USDC", "47.82113549"),
                                               ("USDE", "0.0"), ("USDT0", "0.0")))
    positions, _ = await s.fetch(TARGET)
    spot = {p.key: p for p in positions if p.key.startswith("spot:")}
    assert list(spot) == ["spot:USDC"]
    assert spot["spot:USDC"].amount_raw == 4782113549
    assert calls == ["clearinghouseState", "spotClearinghouseState",
                     "delegatorSummary"]


async def test_decimal_string_amounts_keep_every_digit():
    s, _ = source(perp_state(), spot_state(("USDC", "0.00000001")))
    positions, _ = await s.fetch(TARGET)
    assert [p.amount_raw for p in positions if p.key == "spot:USDC"] == [1]


async def test_perp_position_carries_side_leverage_and_liquidation():
    s, _ = source(perp_state([position()]), spot_state())
    positions, _ = await s.fetch(TARGET)
    eth = [p for p in positions if p.key == "perp:ETH"][0]
    assert eth.amount_raw == 150000000              # 1.5 scaled by 1e8
    assert eth.extra["side"] == "long"
    assert eth.extra["liq_px"] == "2400.0"
    assert eth.extra["leverage"] == "20"
    # mark = positionValue / |size| = 4500 / 1.5 = 3000; liq is 20% below it
    assert float(eth.extra["liq_distance_pct"]) == 20.0


async def test_short_position_has_negative_size():
    s, _ = source(perp_state([position(szi="-2.0", value="6000.0", liq="3600.0")]),
                  spot_state())
    positions, _ = await s.fetch(TARGET)
    eth = [p for p in positions if p.key == "perp:ETH"][0]
    assert eth.amount_raw == -200000000
    assert eth.extra["side"] == "short"


async def test_marker_ignores_account_value():
    """accountValue moves with unrealised PnL every second. If it were part of
    the marker, every tick would look like something happened."""
    s, _ = source(perp_state([position()], account="1000.0"), spot_state())
    _, first = await s.fetch(TARGET)
    s2, _ = source(perp_state([position()], account="1234.56"), spot_state())
    _, second = await s2.fetch(TARGET)
    assert first == second


async def test_marker_changes_when_a_position_size_changes():
    s, _ = source(perp_state([position(szi="1.5")]), spot_state())
    _, first = await s.fetch(TARGET)
    s2, _ = source(perp_state([position(szi="2.5")]), spot_state())
    _, second = await s2.fetch(TARGET)
    assert first != second


async def test_zero_size_positions_are_dropped():
    s, _ = source(perp_state([position(szi="0.0")]), spot_state())
    positions, _ = await s.fetch(TARGET)
    assert [p for p in positions if p.key.startswith("perp:")] == []


async def test_account_summary_is_always_present():
    s, _ = source(perp_state(account="49849359.7565409988"), spot_state())
    positions, _ = await s.fetch(TARGET)
    acct = [p for p in positions if p.key == "account"][0]
    assert acct.extra["withdrawable"] == "900.0"
    assert acct.amount_raw == 4984935975654099


async def test_staking_buckets_are_separate_positions():
    """Delegated, idle and queued-for-withdrawal are three different states of
    the same coin, and only the non-zero ones are reported."""
    s, _ = source(perp_state(), spot_state(),
                  stake_summary(delegated="9.996", pending="12.75774913", n=1))
    positions, _ = await s.fetch(TARGET)
    stake = {p.key: p for p in positions if p.key.startswith("staking:")}
    assert sorted(stake) == ["staking:delegated", "staking:pending"]
    assert stake["staking:delegated"].amount_raw == 999600000
    assert stake["staking:pending"].amount_raw == 1275774913
    assert stake["staking:pending"].extra["withdrawals"] == "1"
    assert {p.symbol for p in stake.values()} == {"HYPE"}
    assert {p.asset_key for p in stake.values()} == {"hyperliquid:HYPE"}


async def test_undelegating_moves_the_marker():
    """The whole stake going from delegated to pending must read as a change;
    an unchanged marker would end the tick before anything is recorded."""
    before, _ = source(perp_state(), spot_state(),
                       stake_summary(delegated="12.75774913"))
    after, _ = source(perp_state(), spot_state(),
                      stake_summary(pending="12.75774913", n=1))
    _, marker_before = await before.fetch(TARGET)
    _, marker_after = await after.fetch(TARGET)
    assert marker_before != marker_after


async def test_address_that_never_staked_reports_nothing():
    s, _ = source(perp_state(), spot_state())
    positions, _ = await s.fetch(TARGET)
    assert [p for p in positions if p.key.startswith("staking:")] == []


async def test_a_spot_balance_carries_the_venues_token_index():
    """The coin name is chosen by whoever deployed the token; the index is the
    asset itself. Without it the price has to be looked up by name, and a
    lookalike named UBTC would be valued as bitcoin."""
    s, _ = source(perp_state(), spot_state(("USDC", "1.0"), ("UBTC", "0.5")))
    positions, _ = await s.fetch(TARGET)
    spot = {p.key: p for p in positions if p.key.startswith("spot:")}
    assert spot["spot:UBTC"].contract == "1"
    assert spot["spot:USDC"].contract == "0"
