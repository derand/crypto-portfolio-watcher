import json

import httpx
import pytest

from portfolio.chains.base import Cursor, Target
from portfolio.chains.evm import EvmAdapter
from portfolio.config import TokenCfg
from portfolio.models import Direction

ME = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
THEM = "0x1111111111111111111111111111111111111111"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SCAM = "0x9999999999999999999999999999999999999999"

TOKENS = {"ethereum": [TokenCfg(chain="ethereum", contract=USDC, symbol="USDC",
                                decimals=6, coingecko_id="usd-coin")]}


def target(chains=("ethereum",), watch=("native", "tokens")):
    return Target(address=ME, label="main", watch=frozenset(watch), chains=tuple(chains))


def adapter(answers, tokens=TOKENS):
    """answers: method -> result, or method -> list of results consumed in order."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        requests.append([c["method"] for c in batch])
        out = []
        for call in batch:
            got = answers[call["method"]]
            if isinstance(got, list):
                got = got.pop(0)
            out.append({"jsonrpc": "2.0", "id": call["id"], "result": got})
        return httpx.Response(200, json=out)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EvmAdapter("key", tokens, client=client,
                      url_template="http://{net}/{key}"), requests


def transfer_doc(uid, category, value_hex, from_=THEM, to=ME, contract=None, block="0x64"):
    return {"uniqueId": uid, "hash": uid.split(":")[0], "category": category,
            "from": from_, "to": to, "blockNum": block,
            "rawContract": {"value": value_hex, "address": contract, "decimal": "0x6"},
            "metadata": {"blockTimestamp": "2026-01-01T00:00:00.000Z"}}


BALANCES = {"eth_blockNumber": "0x100", "eth_getBalance": "0xde0b6b3a7640000",  # 1 ETH
            "alchemy_getTokenBalances": {"tokenBalances": [
                {"contractAddress": USDC, "tokenBalance": hex(1_000_000)}]}}


async def test_probe_is_a_single_round_trip():
    """Three questions batched into one HTTP request keeps quiet ticks cheap."""
    a, requests = adapter(dict(BALANCES))
    p = await a.probe(target(), "ethereum", Cursor())
    assert len(requests) == 1
    assert requests[0] == ["eth_blockNumber", "eth_getBalance", "alchemy_getTokenBalances"]
    assert p.changed is True


async def test_probe_notices_a_token_move_with_the_native_balance_unchanged():
    """Watching only ETH would miss a USDC transfer completely."""
    a, _ = adapter(dict(BALANCES))
    before = await a.probe(target(), "ethereum", Cursor())

    moved = dict(BALANCES)
    moved["alchemy_getTokenBalances"] = {"tokenBalances": [
        {"contractAddress": USDC, "tokenBalance": hex(2_000_000)}]}
    a2, _ = adapter(moved)
    after = await a2.probe(target(), "ethereum", Cursor(last_marker=before.marker))
    assert after.changed is True


async def test_probe_unchanged_when_nothing_moved():
    a, _ = adapter(dict(BALANCES))
    first = await a.probe(target(), "ethereum", Cursor())
    a2, _ = adapter(dict(BALANCES))
    second = await a2.probe(target(), "ethereum", Cursor(last_marker=first.marker))
    assert second.changed is False


async def test_first_sight_records_balances_without_replaying_history():
    a, requests = adapter(dict(BALANCES))
    probe = await a.probe(target(), "ethereum", Cursor())
    state = await a.fetch(target(), "ethereum", Cursor(), probe)
    assert state.transfers == []
    assert state.cursor.last_block == 0x100
    assert {b.symbol for b in state.balances} == {"ETH", "USDC"}
    assert len(requests) == 1, "baseline must not ask for transfers"


async def test_native_balance_is_marked_fee_bearing():
    """Gas drift on ETH must be absorbed, not reported as a missing transfer."""
    a, _ = adapter(dict(BALANCES))
    probe = await a.probe(target(), "ethereum", Cursor())
    state = await a.fetch(target(), "ethereum", Cursor(), probe)
    native = [b for b in state.balances if b.asset_key == "ethereum:native"][0]
    usdc = [b for b in state.balances if b.symbol == "USDC"][0]
    assert native.fee_bearing is True
    assert usdc.fee_bearing is False


async def test_scam_token_airdrop_is_ignored():
    """The whitelist is the anti-spam mechanism; nothing outside it is tracked."""
    answers = dict(BALANCES)
    answers["alchemy_getAssetTransfers"] = [
        {"transfers": [transfer_doc("0xa:log:1", "erc20", hex(500_000), contract=USDC),
                       transfer_doc("0xb:log:2", "erc20", hex(10**24), contract=SCAM)]},
        {"transfers": []}]
    a, _ = adapter(answers)
    probe = await a.probe(target(), "ethereum", Cursor())
    state = await a.fetch(target(), "ethereum",
                          Cursor(last_block=0x50, last_marker="old"), probe)
    assert [t.symbol for t in state.transfers] == ["USDC"]
    assert state.transfers[0].amount_raw == 500_000


async def test_amount_comes_from_the_exact_integer_not_the_float():
    """Alchemy's "value" field is a float and loses wei on large amounts."""
    big = 123456789012345678901  # > 2**53, unrepresentable as a float
    answers = dict(BALANCES)
    doc = transfer_doc("0xc:external", "external", hex(big), contract=None)
    doc["value"] = 123.45678901234568          # the lossy sibling field
    answers["alchemy_getAssetTransfers"] = [{"transfers": [doc]}, {"transfers": []}]
    a, _ = adapter(answers)
    probe = await a.probe(target(), "ethereum", Cursor())
    state = await a.fetch(target(), "ethereum",
                          Cursor(last_block=0x50, last_marker="old"), probe)
    assert state.transfers[0].amount_raw == big


async def test_two_movements_in_one_transaction_both_survive():
    """A swap emits several transfers under one hash; uid keeps them distinct."""
    answers = dict(BALANCES)
    answers["alchemy_getAssetTransfers"] = [
        {"transfers": [transfer_doc("0xsame:log:1", "erc20", hex(1000), contract=USDC),
                       transfer_doc("0xsame:log:2", "erc20", hex(2000), contract=USDC)]},
        {"transfers": []}]
    a, _ = adapter(answers)
    probe = await a.probe(target(), "ethereum", Cursor())
    state = await a.fetch(target(), "ethereum",
                          Cursor(last_block=0x50, last_marker="old"), probe)
    assert len(state.transfers) == 2
    assert len({t.uid for t in state.transfers}) == 2
    assert {t.tx_hash for t in state.transfers} == {"0xsame"}


async def test_outgoing_direction_and_counterparty():
    answers = dict(BALANCES)
    answers["alchemy_getAssetTransfers"] = [
        {"transfers": []},
        {"transfers": [transfer_doc("0xd:external", "external", hex(10**18),
                                    from_=ME, to=THEM)]}]
    a, _ = adapter(answers)
    probe = await a.probe(target(), "ethereum", Cursor())
    state = await a.fetch(target(), "ethereum",
                          Cursor(last_block=0x50, last_marker="old"), probe)
    t = state.transfers[0]
    assert (t.direction, t.counterparty, t.symbol) == (Direction.OUT, THEM, "ETH")
    assert t.effect == -(10**18), "an outgoing transfer lowers the balance"


@pytest.mark.parametrize("network", ["arbitrum", "bsc"])
async def test_networks_without_traces_do_not_request_internal_transfers(network):
    """Alchemy has no internal-transfer data there; asking errors with -32602."""
    captured = {}
    answers = dict(BALANCES)
    answers["alchemy_getTokenBalances"] = {"tokenBalances": []}
    answers["alchemy_getAssetTransfers"] = {"transfers": []}

    def handler(request):
        batch = json.loads(request.content)
        for call in batch:
            if call["method"] == "alchemy_getAssetTransfers":
                captured["categories"] = call["params"][0]["category"]
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": c["id"], "result": answers[c["method"]]} for c in batch])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a = EvmAdapter("key", {}, client=client, url_template="http://{net}/{key}")
    t = target(chains=(network,))
    probe = await a.probe(t, network, Cursor())
    await a.fetch(t, network, Cursor(last_block=0x50, last_marker="old"), probe)
    assert captured["categories"] == ["external", "erc20"]


async def test_bsc_native_balance_is_bnb_not_eth():
    """The native coin differs from the other three networks; ETH here would
    price the whole position off the wrong coin."""
    a, _ = adapter(dict(BALANCES), tokens={})
    t = target(chains=("bsc",))
    probe = await a.probe(t, "bsc", Cursor())
    state = await a.fetch(t, "bsc", Cursor(), probe)
    native = [b for b in state.balances if b.asset_key == "bsc:native"]
    assert [b.symbol for b in native] == ["BNB"]


async def test_hyperliquid_only_address_is_not_polled_on_any_network():
    a, requests = adapter(dict(BALANCES))
    assert a.scopes(target(chains=("ethereum", "base"), watch=("hyperliquid",))) == []
    assert requests == []


async def test_unsupported_network_is_skipped_loudly(caplog):
    """Polygon passes config validation but has no entry in NETWORKS yet:
    say so instead of silently watching nothing."""
    a, _ = adapter(dict(BALANCES))
    with caplog.at_level("WARNING"):
        scopes = a.scopes(target(chains=("ethereum", "polygon")))
    assert scopes == ["ethereum"]
    assert "polygon" in caplog.text


async def test_bad_api_key_is_permanent_not_retried():
    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a = EvmAdapter("nope", {}, client=client, url_template="http://{net}/{key}")
    from portfolio.retry import Permanent
    with pytest.raises(Permanent, match="ALCHEMY_API_KEY"):
        await a.probe(target(), "ethereum", Cursor())


VAULT = "0x46261bc7d0e9f2a35b4c6d8e0f1a2b3c4d5e6f70"
RATE_TOKENS = {"bsc": [TokenCfg(chain="bsc", contract=VAULT, symbol="vBTC",
                                decimals=18, coingecko_id="wrapped-bitcoin",
                                rate_call="convertToAssets(uint256)")]}


def vault_answers(shares, rate):
    return {"eth_blockNumber": "0x100", "eth_getBalance": "0x0",
            "alchemy_getTokenBalances": {"tokenBalances": [
                {"contractAddress": VAULT, "tokenBalance": hex(shares)}]},
            "eth_call": hex(rate)}


async def test_share_balance_is_reported_as_what_it_redeems_for():
    """The yield lives in the rate, so the share count alone looks frozen while
    the position grows."""
    a, requests = adapter(vault_answers(98625346708661549, 1034152000000000000),
                          tokens=RATE_TOKENS)
    t = target(chains=("bsc",))
    probe = await a.probe(t, "bsc", Cursor())
    state = await a.fetch(t, "bsc", Cursor(), probe)
    held = [b for b in state.balances if b.asset_key == f"bsc:{VAULT}"]
    assert held[0].amount_raw == 101993599549455758      # 0.098625 x 1.034152
    assert held[0].yield_bearing is True
    # One HTTP request still: the rate call rides in the same JSON-RPC batch.
    assert len(requests) == 1
    assert requests[0] == ["eth_blockNumber", "eth_getBalance",
                           "alchemy_getTokenBalances", "eth_call"]


async def test_rate_drift_below_four_decimals_is_not_a_change():
    """The rate moves every block. Treating that as news would turn every tick
    into a transfer fetch and never stop."""
    a, _ = adapter(vault_answers(10**18, 1034152000000000000), tokens=RATE_TOKENS)
    t = target(chains=("bsc",))
    first = await a.probe(t, "bsc", Cursor())

    b, _ = adapter(vault_answers(10**18, 1034152999999999999), tokens=RATE_TOKENS)
    nudged = await b.probe(t, "bsc", Cursor(last_marker=first.marker))
    assert nudged.changed is False

    c, _ = adapter(vault_answers(10**18, 1034200000000000000), tokens=RATE_TOKENS)
    real = await c.probe(t, "bsc", Cursor(last_marker=first.marker))
    assert real.changed is True


async def test_rebasing_token_without_a_rate_call_is_still_yield_bearing():
    """An Aave aToken grows with no rate to read; the flag is what stops it
    reading as money from nowhere."""
    atoken = "0x513c7e3a9c69ca3e22550ef58ac1c0088e918fff"
    tokens = {"arbitrum": [TokenCfg(chain="arbitrum", contract=atoken,
                                    symbol="aArbwstETH", decimals=18,
                                    yield_bearing=True)]}
    answers = {"eth_blockNumber": "0x100", "eth_getBalance": "0x0",
               "alchemy_getTokenBalances": {"tokenBalances": [
                   {"contractAddress": atoken, "tokenBalance": hex(5 * 10**18)}]}}
    a, requests = adapter(answers, tokens=tokens)
    t = target(chains=("arbitrum",))
    probe = await a.probe(t, "arbitrum", Cursor())
    state = await a.fetch(t, "arbitrum", Cursor(), probe)
    held = [b for b in state.balances if b.asset_key == f"arbitrum:{atoken}"]
    assert held[0].amount_raw == 5 * 10**18        # reported as-is, not converted
    assert held[0].yield_bearing is True
    assert "eth_call" not in requests[0]


ACCOUNTANT = "0xacc07a1b2c3d4e5f60718293a4b5c6d7e8f90a1b"
ON_ACCOUNTANT = {"base": [TokenCfg(
    chain="base", contract="0x46262ca8e1f0a3b46c5d7e9f0a1b2c3d4e5f6071",
    symbol="vBTCb", decimals=8, coingecko_id="wrapped-bitcoin",
    rate_call="getRate()", rate_from=ACCOUNTANT)]}


async def test_rate_can_live_on_another_contract_and_scale_by_token_decimals():
    """A share whose rate lives on a separate accountant speaks in the token's
    8 decimals, not 18; dividing by 1e18 here would report it as zero."""
    contract = "0x46262ca8e1f0a3b46c5d7e9f0a1b2c3d4e5f6071"
    answers = {"eth_blockNumber": "0x100", "eth_getBalance": "0x0",
               "alchemy_getTokenBalances": {"tokenBalances": [
                   {"contractAddress": contract, "tokenBalance": hex(4890426)}]},
               "eth_call": hex(102471438)}
    sent = []

    def handler(request):
        batch = json.loads(request.content)
        sent.extend(batch)
        out = []
        for call in batch:
            got = answers[call["method"]]
            out.append({"jsonrpc": "2.0", "id": call["id"], "result": got})
        return httpx.Response(200, json=out)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a = EvmAdapter("key", ON_ACCOUNTANT, client=client,
                   url_template="http://{net}/{key}")
    t = target(chains=("base",))
    probe = await a.probe(t, "base", Cursor())
    state = await a.fetch(t, "base", Cursor(), probe)
    held = [b for b in state.balances if b.asset_key == f"base:{contract}"]
    assert held[0].amount_raw == 5011289          # 0.04890426 x 1.02471438
    assert held[0].yield_bearing is True

    ask = [c for c in sent if c["method"] == "eth_call"][0]["params"][0]
    assert ask["to"] == ACCOUNTANT, "the rate is asked of the accountant"
    assert ask["data"] == "0x679aefce", "getRate() takes no argument"


async def test_share_and_asset_decimals_can_differ():
    """A USDC vault holds 18-decimal shares and answers convertToAssets in
    USDC's 6. Scaling the call argument by the asset would ask for a millionth
    of a share and report the position twelve orders of magnitude short."""
    rewarder = "0x46263db9f201b4c57d6e8f0a1b2c3d4e5f607182"
    pool = "0x46264eca0312c5d68e7f9a0b1c2d3e4f50617283"
    tokens = {"ethereum": [TokenCfg(
        chain="ethereum", contract=rewarder, symbol="vUSDC", decimals=6,
        share_decimals=18, coingecko_id="usd-coin",
        rate_call="convertToAssets(uint256)", rate_from=pool)]}
    answers = {"eth_blockNumber": "0x100", "eth_getBalance": "0x0",
               "alchemy_getTokenBalances": {"tokenBalances": [
                   {"contractAddress": rewarder,
                    "tokenBalance": hex(4390077883900320389986)}]},
               "eth_call": hex(1100871)}          # USDC per share, 6 decimals
    sent = []

    def handler(request):
        batch = json.loads(request.content)
        sent.extend(batch)
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": c["id"], "result": answers[c["method"]]}
            for c in batch])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a = EvmAdapter("key", tokens, client=client, url_template="http://{net}/{key}")
    t = target(chains=("ethereum",))
    probe = await a.probe(t, "ethereum", Cursor())
    state = await a.fetch(t, "ethereum", Cursor(), probe)
    held = [b for b in state.balances if b.asset_key == f"ethereum:{rewarder}"]
    # convertToAssets(balance) on chain answers 4832911956; asking per whole
    # share instead truncates the rate to USDC's 6 decimals and loses 0.0025
    # USDC across 4390 shares. That is the price of one round trip, and it is
    # three orders of magnitude below the $1 alert threshold.
    assert held[0].amount_raw == 4832909430           # 4,832.909430 USDC
    assert held[0].decimals == 6

    ask = [c for c in sent if c["method"] == "eth_call"][0]["params"][0]
    assert ask["to"] == pool, "the rate comes from the pool, not the share"
    assert ask["data"].endswith(f"{10**18:064x}"), "asked in whole shares"


class FakeWithdrawals:
    """Records what it was asked for and answers with one withdrawal."""

    def __init__(self, transfers=()):
        self.transfers = list(transfers)
        self.asked = []

    async def since(self, address, start, tip):
        self.asked.append((address, start, tip))
        return list(self.transfers)

    async def aclose(self):
        pass


def _withdrawal(amount):
    from datetime import datetime, timezone
    from portfolio.models import Transfer
    return Transfer(tx_hash="beacon:1", uid="beacon:1", asset_key="ethereum:native",
                    amount_raw=amount, direction=Direction.IN,
                    counterparty="validator:999999", block_height=0x60,
                    ts=datetime(2026, 9, 6, tzinfo=timezone.utc),
                    symbol="ETH", decimals=18)


async def test_beacon_withdrawals_are_merged_into_ethereum_transfers():
    """They are not transactions, so nothing else reports them; unexplained,
    each arrival reads as money from nowhere and alerts."""
    answers = dict(BALANCES)
    answers["alchemy_getAssetTransfers"] = {"transfers": []}
    wd = FakeWithdrawals([_withdrawal(14423633 * 10**9)])
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": c["id"], "result": answers[c["method"]]}
            for c in json.loads(r.content)])))
    a = EvmAdapter("key", {}, client=client, url_template="http://{net}/{key}",
                   withdrawals=wd)
    t = target(chains=("ethereum",), watch=("native", "tokens", "beacon"))
    probe = await a.probe(t, "ethereum", Cursor())
    state = await a.fetch(t, "ethereum",
                          Cursor(last_block=0x50, last_marker="old"), probe)
    assert [x.uid for x in state.transfers] == ["beacon:1"]
    assert wd.asked == [(ME, 0x51, 0x100)], "asked from the cursor, not genesis"


async def test_withdrawals_are_not_fetched_where_they_cannot_happen():
    """Only Ethereum has beacon withdrawals, and only if the address says so."""
    answers = dict(BALANCES)
    answers["alchemy_getAssetTransfers"] = {"transfers": []}

    async def run(chain, watch):
        wd = FakeWithdrawals()
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[
                {"jsonrpc": "2.0", "id": c["id"], "result": answers[c["method"]]}
                for c in json.loads(r.content)])))
        a = EvmAdapter("key", {}, client=client, url_template="http://{net}/{key}",
                       withdrawals=wd)
        t = target(chains=(chain,), watch=watch)
        probe = await a.probe(t, chain, Cursor())
        await a.fetch(t, chain, Cursor(last_block=0x50, last_marker="old"), probe)
        return wd.asked

    assert await run("arbitrum", ("native", "beacon")) == []
    assert await run("ethereum", ("native", "tokens")) == []
    assert await run("ethereum", ("native", "beacon")) != []


async def test_a_beacon_only_address_is_still_polled_on_ethereum():
    """Config demands `chains` only when native or tokens are watched, so an
    address watching beacon alone passes validation with none. scopes() then
    built its list from that empty tuple and returned nothing, so the address
    was never polled and its withdrawals were never read - while the validator
    balances kept arriving from the beacon source, which made the hole silent:
    numbers present, withdrawals missing. Withdrawals land on L1 by protocol,
    not by preference, so ethereum is the answer rather than an error."""
    a, _ = adapter(dict(BALANCES))
    assert a.scopes(target(chains=(), watch=("beacon",))) == ["ethereum"]


async def test_an_explicit_chain_list_still_wins_for_a_beacon_address():
    a, _ = adapter(dict(BALANCES))
    assert a.scopes(target(chains=("base",), watch=("beacon", "native"))) == ["base"]


async def test_a_provider_error_never_carries_the_api_key():
    """An adapter failure becomes text: pipeline puts it in ScanResult.failed and
    the bot prints those lines into a Telegram message. httpx's own message for a
    bad status contains the whole URL, and the Alchemy key is a path segment of
    it - so a provider answering 429 was enough to publish the key to the chat."""
    from portfolio.retry import Unavailable

    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a = EvmAdapter("s3cret-alchemy-key", {}, client=client,
                   url_template="http://{net}/{key}")
    with pytest.raises(Unavailable) as caught:
        await a.probe(target(), "ethereum", Cursor())
    assert "s3cret-alchemy-key" not in str(caught.value)
    assert "429" in str(caught.value), "the status is what the reader needs"


def test_redact_leaves_short_strings_alone():
    """A one-character or empty key would turn every message into asterisks;
    an unset key is empty, and adapters are constructed with it routinely."""
    from portfolio.retry import redact

    assert redact("no key here", "") == "no key here"
    assert redact("a fine message", "a") == "a fine message"
    assert redact("url/v2/abcdefgh12", "abcdefgh12") == "url/v2/***"
