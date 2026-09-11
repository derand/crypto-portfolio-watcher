import json
import math

import httpx

from portfolio import catalog
from portfolio.chains.abi import encode_address, selector
from portfolio.chains.base import Target
from portfolio.chains.evm import EvmAdapter
from portfolio.protocols import ticks
from portfolio.protocols.univ3 import UniV3Source

ME = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
NFPM = "0x46261bc7d0e9f2a35b4c6d8e0f1a2b3c4d5e6f70"
FACTORY = "0x46262ca8e1f0a3b46c5d7e9f0a1b2c3d4e5f6071"
POOL = "0x46263db9f201b4c57d6e8f0a1b2c3d4e5f607182"
WETH = "0x46264eca0312c5d68e7f9a0b1c2d3e4f50617283"
USDC = "0x46265fdb1423d6e79f8a0b1c2d3e4f5061728394"


# --------------------------------------------------------------------------
# The arithmetic, against constants the port cannot have invented
# --------------------------------------------------------------------------

def test_the_tick_scale_matches_uniswaps_own_boundary_constants():
    """These three numbers are published by the protocol: Q96 at tick zero, and
    MIN_SQRT_RATIO / MAX_SQRT_RATIO at the ends. A single wrong digit in any of
    the twenty magic factors moves at least one of them, which is what makes
    them a real check on a table nobody can read by eye."""
    assert ticks.sqrt_ratio_at_tick(0) == 1 << 96
    assert ticks.sqrt_ratio_at_tick(ticks.MIN_TICK) == 4295128739
    assert ticks.sqrt_ratio_at_tick(ticks.MAX_TICK) == \
        1461446703485210103287273052203988822378723970342


def test_the_tick_scale_agrees_with_the_arithmetic_it_stands_for():
    """The table is a fast way to compute sqrt(1.0001^tick) in integers. Float
    is the slow way, and disagreeing with it by more than float's own error
    means the table is wrong, not that the shortcut is clever."""
    for tick in (-500000, -100000, -14377, -1, 1, 4321, 100000, 500000):
        exact = ticks.sqrt_ratio_at_tick(tick)
        approx = math.sqrt(1.0001 ** tick) * (1 << 96)
        assert abs(exact - approx) / approx < 1e-9, tick


def test_the_scale_never_goes_backwards():
    """Price rises with the tick. A non-monotonic step would put a position on
    the wrong side of its own range."""
    previous = None
    for tick in range(-1000, 1000, 37):
        value = ticks.sqrt_ratio_at_tick(tick)
        assert previous is None or value > previous
        previous = value


def test_a_position_below_its_range_holds_only_token0():
    """This is what "out of range" means in money: the liquidity has been fully
    converted into one asset and earns nothing until the price comes back."""
    lower, upper, liquidity = -200, -100, 10 ** 18
    below = ticks.sqrt_ratio_at_tick(-500)
    amount0, amount1 = ticks.amounts_for_liquidity(below, lower, upper, liquidity)
    assert amount0 > 0 and amount1 == 0
    assert not ticks.in_range(-500, lower, upper)


def test_a_position_above_its_range_holds_only_token1():
    lower, upper, liquidity = -200, -100, 10 ** 18
    above = ticks.sqrt_ratio_at_tick(500)
    amount0, amount1 = ticks.amounts_for_liquidity(above, lower, upper, liquidity)
    assert amount0 == 0 and amount1 > 0


def test_inside_its_range_a_position_holds_both():
    """And both halves have to match the formulas independently: getting one of
    them right while the other is scaled by 2^96 is the easy mistake here."""
    lower, upper, liquidity = -14600, -14200, 1008220849297
    price = ticks.sqrt_ratio_at_tick(-14377)
    amount0, amount1 = ticks.amounts_for_liquidity(price, lower, upper, liquidity)
    sp, sa, sb = price / 2 ** 96, math.sqrt(1.0001 ** lower), math.sqrt(1.0001 ** upper)
    assert abs(amount0 - liquidity * (sb - sp) / (sp * sb)) / amount0 < 1e-6
    assert abs(amount1 - liquidity * (sp - sa)) / amount1 < 1e-6


def test_the_upper_bound_is_exclusive():
    """Uniswap's own convention. A position whose range ends exactly at the
    current tick is already out, and saying otherwise reports it as earning."""
    assert ticks.in_range(-100, -200, -100) is False
    assert ticks.in_range(-101, -200, -100) is True
    assert ticks.in_range(-200, -200, -100) is True


# --------------------------------------------------------------------------
# The source: wiring, not arithmetic
# --------------------------------------------------------------------------

def word(n):
    return f"{n & ((1 << 256) - 1):064x}"


def enc(*values):
    return "0x" + "".join(word(v) for v in values)


def enc_string(text):
    raw = text.encode()
    return "0x" + word(32) + word(len(raw)) + raw.hex() + "00" * ((-len(raw)) % 32)


def position_answer(token0, token1, key4, lower, upper, liquidity):
    return enc(0, 0, int(token0, 16), int(token1, 16), key4, lower, upper,
               liquidity, 0, 0, 0, 0)


def source(answers, entries, seen=None, owned=None, index_status=200):
    """`owned`: what the NFT index lists, defaulting to the one position."""
    calls = seen if seen is not None else []

    def handler(request):
        if request.method == "GET":
            calls.append(("nft-index", str(request.url), None))
            if index_status != 200:
                return httpx.Response(index_status, json={})
            ids = [TOKEN_ID] if owned is None else owned
            return httpx.Response(200, json={
                "ownedNfts": [{"tokenId": str(i)} for i in ids]})
        out = []
        for call in json.loads(request.content):
            params = call["params"][0]
            to, data = params["to"].lower(), params["data"]
            calls.append((to, data, params.get("from")))
            got = answers.get((to, data))
            if got is None:
                out.append({"jsonrpc": "2.0", "id": call["id"],
                            "error": {"code": 3, "message": "execution reverted"}})
            else:
                out.append({"jsonrpc": "2.0", "id": call["id"], "result": got})
        return httpx.Response(200, json=out)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = EvmAdapter("key", {}, client=client, url_template="http://{net}/{key}",
                         nft_url_template="http://nft/{net}/{key}")
    return UniV3Source(entries=entries, adapter=adapter), calls


UNI = catalog.Entry(protocol="uniswap-v3", chain="ethereum", kind="univ3",
                    address=NFPM)
SLIP = catalog.Entry(protocol="aerodrome-slipstream", chain="ethereum",
                     kind="slipstream", address=NFPM)

LOWER, UPPER, LIQUIDITY, TOKEN_ID = -14600, -14200, 1008220849297, 4242


def market(tick, fees=(0, 0), key4=3000):
    """One owner, one position, one pool, priced at `tick`."""
    return {
        (NFPM, encode_address("balanceOf(address)", ME)): enc(1),
        (NFPM, selector("tokenOfOwnerByIndex(address,uint256)")
         + word(int(ME, 16)) + word(0)): enc(TOKEN_ID),
        (NFPM, selector("positions(uint256)") + word(TOKEN_ID)):
            position_answer(WETH, USDC, key4, LOWER, UPPER, LIQUIDITY),
        (NFPM, selector("collect((uint256,address,uint128,uint128))")
         + word(TOKEN_ID) + word(int(ME, 16))
         + word((1 << 128) - 1) + word((1 << 128) - 1)): enc(*fees),
        (NFPM, selector("factory()")): enc(int(FACTORY, 16)),
        (FACTORY, selector("getPool(address,address,uint24)")
         + word(int(WETH, 16)) + word(int(USDC, 16)) + word(key4)): enc(int(POOL, 16)),
        (FACTORY, selector("getPool(address,address,int24)")
         + word(int(WETH, 16)) + word(int(USDC, 16)) + word(key4)): enc(int(POOL, 16)),
        (POOL, selector("slot0()")): enc(ticks.sqrt_ratio_at_tick(tick), tick, 0, 0, 0, 0, 1),
        (WETH, selector("symbol()")): enc_string("WETH"),
        (WETH, selector("decimals()")): enc(18),
        (USDC, selector("symbol()")): enc_string("USDC"),
        (USDC, selector("decimals()")): enc(6),
    }


def target(watch=("univ3",)):
    return Target(address=ME, label="main", watch=frozenset(watch),
                  chains=("ethereum",))


async def test_one_nft_becomes_two_legs_that_can_be_priced():
    """A range position holds two assets at once, so one row cannot describe it.
    Each leg carries the asset key of the token it actually is, which is what
    lets the existing price machinery value it without knowing about Uniswap."""
    src, _ = source(market(-14377), [UNI])
    positions, _ = await src.fetch(target())
    assert [p.key for p in positions] == [f"ethereum:{TOKEN_ID}:0",
                                          f"ethereum:{TOKEN_ID}:1"]
    assert [p.symbol for p in positions] == ["WETH", "USDC"]
    assert [p.decimals for p in positions] == [18, 6]
    assert positions[0].asset_key == f"ethereum:{WETH}"
    assert all(p.amount_raw > 0 for p in positions), "in range: both legs held"
    assert all(p.accrues for p in positions), \
        "composition moves with every trade; alerting on it never stops"


async def test_the_held_ids_come_from_the_index_not_one_call_per_position():
    """tokenOfOwnerByIndex is one eth_call per position - 35 of them on the
    measured portfolio, for a list the NFT index hands over in one request.
    At 26 compute units each that is most of what a tick spends here."""
    src, calls = source(market(-14377), [UNI])
    positions, _ = await src.fetch(target())
    assert len(positions) == 2
    assert any(c[0] == "nft-index" for c in calls)
    assert not [c for c in calls if c[1].startswith(
        selector("tokenOfOwnerByIndex(address,uint256)"))]


async def test_an_index_short_of_the_balance_is_not_a_closed_position():
    """The index is a second source of truth and may lag. Read at face value a
    short list reports the missing NFTs as closed - which alerts, deletes the
    stored row and writes a terminal zero into the history no later tick can
    take back. balanceOf is the count; a disagreement asks the contract."""
    src, calls = source(market(-14377), [UNI], owned=[])
    positions, _ = await src.fetch(target())
    assert [p.key for p in positions] == [f"ethereum:{TOKEN_ID}:0",
                                          f"ethereum:{TOKEN_ID}:1"]
    assert [c for c in calls if c[1].startswith(
        selector("tokenOfOwnerByIndex(address,uint256)"))], "the contract decides"


async def test_an_unreachable_index_costs_calls_and_not_the_positions():
    """The index is an optimisation, and v3 has the enumerable contract the
    optimisation replaces. A 403 from the NFT API - a key without that product
    is one - must cost the tick 35 calls, never the position."""
    src, calls = source(market(-14377), [UNI], index_status=403)
    positions, _ = await src.fetch(target())
    assert len(positions) == 2
    assert [c for c in calls if c[1].startswith(
        selector("tokenOfOwnerByIndex(address,uint256)"))]


async def test_the_factory_is_asked_once_and_only_when_a_pool_is_unknown():
    """factory() is immutable on the position manager and is needed only to
    resolve a pool this process has not seen. Asked on every tick it was a
    serialized round trip whose answer was thrown away - the getPool batch
    cannot be built until it returns - for a pool address already in memory."""
    src, calls = source(market(-14377), [UNI])
    await src.fetch(target())
    await src.fetch(target())

    asked = [c for c in calls if c[1] == selector("factory()")]
    assert len(asked) == 1, "the second tick knew the pool already"
    assert len([c for c in calls if c[1].startswith(
        selector("getPool(address,address,uint24)"))]) == 1


async def test_a_factory_that_could_not_be_read_is_not_remembered_as_none():
    """One reverted read must not become the permanent answer: every position
    this market gains afterwards would be unresolvable, and a position with no
    pool is a position with no amount."""
    answers = market(-14377)
    hidden = answers.pop((NFPM, selector("factory()")))
    src, _ = source(answers, [UNI])
    assert await src.fetch(target()) == ([], "")

    answers[(NFPM, selector("factory()"))] = hidden
    positions, _ = await src.fetch(target())
    assert len(positions) == 2


async def test_uncollected_fees_are_read_only_by_the_daily_claim_path():
    """tokensOwed in the NFT only updates when the position is poked, so on one
    left alone it reads zero while real fees sit there. Simulating collect() is
    the reading that is actually true - and it is only given to the owner, so
    the call has to carry a from address."""
    src, calls = source(market(-14377, fees=(84250230863135893, 661007116889360)),
                        [UNI])
    positions, _ = await src.fetch(target())
    assert "fees_raw" not in positions[0].extra
    assert not [c for c in calls if c[1].startswith(
        selector("collect((uint256,address,uint128,uint128))"))]
    fees = await src.claimable_fees(
        target(), {("ethereum", "uniswap-v3"): [TOKEN_ID]})
    assert fees[("ethereum", "uniswap-v3", TOKEN_ID)] == \
        (84250230863135893, 661007116889360)
    collects = [c for c in calls if c[1].startswith(
        selector("collect((uint256,address,uint128,uint128))"))]
    assert collects and all(c[2] == ME for c in collects), \
        "collect simulated as somebody else pays out nothing"


async def test_a_price_nudge_does_not_make_the_marker_change():
    """The marker decides whether the tick bothers diffing. Built from the
    amounts it would differ on every block, because a range position's split
    moves with each trade - and the cheap check would stop being a check."""
    a, _ = source(market(-14377), [UNI])
    b, _ = source(market(-14377 + 5), [UNI])
    _, first = await a.fetch(target())
    _, nudged = await b.fetch(target())
    assert first == nudged

    c, _ = source(market(-14377 + 300), [UNI])
    _, moved = await c.fetch(target())
    assert moved != first, "a real move still has to be noticed"


async def test_leaving_the_range_changes_the_marker_whatever_the_price_did():
    """Rounding the price is safe; rounding this away is not. Falling out of
    range is the one transition worth a message, and a marker that missed it
    would hide it until something else happened to move."""
    inside, _ = source(market(LOWER + 10), [UNI])
    outside, _ = source(market(UPPER + 1), [UNI])
    _, marker_in = await inside.fetch(target())
    _, marker_out = await outside.fetch(target())
    assert marker_in != marker_out

    positions, _ = await outside.fetch(target())
    assert positions[0].extra["in_range"] == "false"
    assert positions[0].amount_raw == 0, "above the range it is all token1"
    assert positions[1].amount_raw > 0


async def test_a_closed_position_is_not_reported():
    """Burning the liquidity leaves the NFT in the wallet with zero in it.
    Reporting those would fill the digest with empty rows for every position
    ever closed."""
    answers = market(-14377)
    answers[(NFPM, selector("positions(uint256)") + word(TOKEN_ID))] = \
        position_answer(WETH, USDC, 3000, LOWER, UPPER, 0)
    src, _ = source(answers, [UNI])
    positions, marker = await src.fetch(target())
    assert positions == [] and marker == ""


async def test_slipstream_is_asked_for_its_pool_the_way_it_expects():
    """Its positions() looks identical but the fourth field is a tick spacing,
    and its factory takes int24 where Uniswap takes uint24. Calling the Uniswap
    form reverts - so a fork treated as Uniswap finds no pool, and every
    position under it silently disappears."""
    src, calls = source(market(-14377, key4=100), [SLIP])
    positions, _ = await src.fetch(target())
    assert len(positions) == 2, "the pool was found"
    asked = [c[1][:10] for c in calls if c[0] == FACTORY]
    assert selector("getPool(address,address,int24)") in asked
    assert selector("getPool(address,address,uint24)") not in asked


async def test_the_pool_is_looked_up_once_and_remembered():
    """A pool address for a pair and a fee never changes, and a watcher runs for
    weeks. Asking the factory again every fifteen minutes buys nothing."""
    src, calls = source(market(-14377), [UNI])
    await src.fetch(target())
    await src.fetch(target())
    lookups = [c for c in calls if c[0] == FACTORY]
    assert len(lookups) == 1, f"asked {len(lookups)} times"
