import json

import httpx
from eth_utils import keccak

import multicall
from portfolio import catalog
from portfolio.chains.abi import selector
from portfolio.chains.base import Target
from portfolio.chains.evm import MULTICALL3, EvmAdapter
from portfolio.protocols import ticks
from portfolio.protocols.univ4 import (POOLS_SLOT, UniV4Source, pool_id,
                                       state_slot, unpack_info, unpack_slot0)

ME = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
POSM = "0x46261bc7d0e9f2a35b4c6d8e0f1a2b3c4d5e6f70"
MANAGER = "0x46262ca8e1f0a3b46c5d7e9f0a1b2c3d4e5f6071"
USDC = "0x46263db9f201b4c57d6e8f0a1b2c3d4e5f607182"
NATIVE = "0x" + "0" * 40

ENTRY = catalog.Entry(protocol="uniswap-v4", chain="ethereum", kind="univ4",
                      address=POSM)
LOWER, UPPER, LIQUIDITY, TOKEN_ID = -14600, -14200, 1008220849297, 77


def word(n):
    return f"{n & ((1 << 256) - 1):064x}"


def enc(*values):
    return "0x" + "".join(word(v) for v in values)


def enc_string(text):
    raw = text.encode()
    return "0x" + word(32) + word(len(raw)) + raw.hex() + "00" * ((-len(raw)) % 32)


# --------------------------------------------------------------------------
# The pieces v4 replaced
# --------------------------------------------------------------------------

def test_a_pool_is_identified_by_five_fields_including_its_hook():
    """v3 asked a factory to turn a pair and a fee into a pool address. Here the
    identity is a hash of the whole key, so it is computed rather than fetched -
    and the hook contract is part of it. Two pools with identical tokens, fee
    and spacing but different hooks are different pools, and treating them as
    one would read the wrong storage."""
    plain = pool_id(1, 2, 3000, 60, 0)
    hooked = pool_id(1, 2, 3000, 60, 0xABCD)
    assert plain != hooked
    assert pool_id(1, 2, 3000, 60, 0) == plain, "the same key hashes the same way"
    # fee and tick spacing are independent in v4; v3 tied them together
    assert pool_id(1, 2, 3000, 60, 0) != pool_id(1, 2, 3000, 10, 0)


def test_the_pool_key_is_five_words_in_order():
    """A field out of order or a short word gives a plausible-looking hash that
    points at nothing, and every number read afterwards comes from whatever
    happens to live at that slot."""
    expected = keccak(b"".join(x.to_bytes(32, "big") for x in
                               (11, 22, 500, 10, 33)))
    assert pool_id(11, 22, 500, 10, 33) == expected


def test_a_negative_tick_spacing_still_hashes_as_a_full_word():
    """tickSpacing is int24. Encoded as anything but the two's complement of a
    256-bit word it hashes differently from what the chain hashed."""
    assert pool_id(1, 2, 3000, -60, 0) == keccak(b"".join(
        x.to_bytes(32, "big") for x in (1, 2, 3000, (1 << 256) - 60, 0)))


def test_the_state_slot_is_the_pools_mapping_entry():
    """PoolManager has no getters at all, so the price is read out of storage.
    This is the one number in the module that could go wrong without any call
    failing - the read would just return some other slot's contents."""
    pid = pool_id(1, 2, 3000, 60, 0)
    assert state_slot(pid) == "0x" + keccak(
        pid + POOLS_SLOT.to_bytes(32, "big")).hex()


def test_slot0_is_unpacked_from_one_word():
    """sqrtPriceX96 in the low 160 bits, the tick in the next 24 - and the tick
    is signed, so a pool below parity decodes as a vast positive number if the
    sign is dropped."""
    price = ticks.sqrt_ratio_at_tick(-14377)
    packed = price | ((-14377 & 0xffffff) << 160)
    assert unpack_slot0(packed) == (price, -14377)
    assert unpack_slot0(0) is None, "an unset pool is not a pool at price zero"


def test_position_info_unpacks_to_the_ticks_and_the_pool_it_names():
    """PositionInfo packs poolId(200) | tickUpper(24) | tickLower(24) |
    subscriber(8) into one word."""
    pid = pool_id(1, 2, 3000, 60, 0)
    truncated = int.from_bytes(pid, "big") >> 56
    packed = (truncated << 56) | ((-100 & 0xffffff) << 32) | ((-200 & 0xffffff) << 8)
    assert unpack_info(packed) == (-200, -100, truncated)


# --------------------------------------------------------------------------
# The source
# --------------------------------------------------------------------------

def source(answers, owned, entries, seen=None):
    """owned: the NFT index's answer; answers: (to, calldata) -> hex."""
    calls = seen if seen is not None else []

    def handler(request):
        if request.method == "GET":
            calls.append(("nft-index", str(request.url)))
            return httpx.Response(200, json={
                "ownedNfts": [{"tokenId": str(i)} for i in owned]})
        out = []
        for call in json.loads(request.content):
            params = call["params"][0]
            to, data = params["to"].lower(), params["data"]
            if to == MULTICALL3:
                out.append({"jsonrpc": "2.0", "id": call["id"],
                            "result": multicall.answer(answers, data)})
                for sub in multicall.subcalls(data):
                    calls.append(sub)
                continue
            calls.append((to, data))
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
    return UniV4Source(entries=entries, adapter=adapter), calls


def market(tick, token0=NATIVE, token1=USDC, liquidity=LIQUIDITY,
           lower=LOWER, upper=UPPER, break_key=False):
    fee, spacing, hooks = 3000, 60, 0
    pid = pool_id(int(token0, 16), int(token1, 16), fee, spacing, hooks)
    stored = int.from_bytes(pid, "big") >> 56
    if break_key:
        stored ^= 1
    info = (stored << 56) | ((upper & 0xffffff) << 32) | ((lower & 0xffffff) << 8)
    return {
        (POSM, selector("poolManager()")): enc(int(MANAGER, 16)),
        (POSM, selector("getPositionLiquidity(uint256)") + word(TOKEN_ID)):
            enc(liquidity),
        (POSM, selector("getPoolAndPositionInfo(uint256)") + word(TOKEN_ID)):
            enc(int(token0, 16), int(token1, 16), fee, spacing, hooks, info),
        (MANAGER, selector("extsload(bytes32)") + state_slot(pid)[2:]):
            enc(ticks.sqrt_ratio_at_tick(tick) | ((tick & 0xffffff) << 160)),
        (USDC, selector("symbol()")): enc_string("USDC"),
        (USDC, selector("decimals()")): enc(6),
    }


def target():
    return Target(address=ME, label="main", watch=frozenset(["univ4"]),
                  chains=("ethereum",))


async def test_positions_are_found_through_the_nft_index_not_the_contract():
    """v4's position manager is deliberately not ERC721Enumerable, so there is
    no tokenOfOwnerByIndex to walk. The fallback everyone reaches for is
    eth_getLogs, which the free tier serves in ten-block windows - hundreds of
    thousands of requests to cover a year."""
    src, calls = source(market(-14377), [TOKEN_ID], [ENTRY])
    positions, _ = await src.fetch(target())
    assert len(positions) == 2
    assert any(c[0] == "nft-index" for c in calls)
    assert not any(isinstance(c[1], str) and c[1].startswith(
        selector("tokenOfOwnerByIndex(address,uint256)")) for c in calls)


async def test_native_eth_is_a_currency_and_is_not_asked_for_a_symbol():
    """currency0 may be the zero address and mean real ETH rather than WETH.
    Asking it for symbol() or decimals() reverts, and taking the revert as
    "eighteen decimals, no name" would be luck rather than correctness."""
    src, calls = source(market(-14377), [TOKEN_ID], [ENTRY])
    positions, _ = await src.fetch(target())
    eth = positions[0]
    assert eth.symbol == "ETH" and eth.decimals == 18
    assert eth.asset_key == "ethereum:native", "priced as the coin, not as a token"
    assert not any(c[0] == NATIVE for c in calls)


async def test_a_position_whose_key_does_not_match_its_pool_is_skipped():
    """The NFT stores the top 200 bits of the pool id it belongs to. If the key
    we hashed disagrees, every figure that follows would be read out of another
    pool's storage - which is worse than reporting nothing."""
    src, _ = source(market(-14377, break_key=True), [TOKEN_ID], [ENTRY])
    positions, marker = await src.fetch(target())
    assert positions == [] and marker == ""


async def test_a_closed_position_is_not_reported():
    src, _ = source(market(-14377, liquidity=0), [TOKEN_ID], [ENTRY])
    positions, marker = await src.fetch(target())
    assert positions == [] and marker == ""


async def test_the_range_alert_works_the_same_as_it_does_for_v3():
    """The plumbing is different; the thing worth saying is not. Both legs carry
    in_range, so the pipeline's state event does not need to know which version
    of Uniswap produced them."""
    inside, _ = source(market(LOWER + 10), [TOKEN_ID], [ENTRY])
    outside, _ = source(market(UPPER + 1), [TOKEN_ID], [ENTRY])
    got_in, marker_in = await inside.fetch(target())
    got_out, marker_out = await outside.fetch(target())
    assert got_in[0].extra["in_range"] == "true"
    assert got_out[0].extra["in_range"] == "false"
    assert marker_in != marker_out
    assert all(p.accrues for p in got_in), "composition drift stays silent"


async def test_a_price_nudge_does_not_move_the_marker():
    a, _ = source(market(-14377), [TOKEN_ID], [ENTRY])
    b, _ = source(market(-14377 + 5), [TOKEN_ID], [ENTRY])
    c, _ = source(market(-14377 + 300), [TOKEN_ID], [ENTRY])
    _, first = await a.fetch(target())
    _, nudged = await b.fetch(target())
    _, moved = await c.fetch(target())
    assert first == nudged and moved != first


async def test_the_pool_manager_is_asked_once():
    """It is a property of the deployment, not of the tick."""
    src, calls = source(market(-14377), [TOKEN_ID], [ENTRY])
    await src.fetch(target())
    await src.fetch(target())
    asked = [c for c in calls
             if c[0] == POSM and c[1] == selector("poolManager()")]
    assert len(asked) == 1
