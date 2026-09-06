import json

import httpx
import pytest

from portfolio import config as cfgmod
from portfolio import discover
from portfolio.chains.evm import EvmAdapter

ME = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
GOOD = "0x1111111111111111111111111111111111111111"
DUST = "0x2222222222222222222222222222222222222222"
SPAM = "0x3333333333333333333333333333333333333333"
KNOWN = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

CFG = """
db_path: {db}
notify:
  telegram:
    enabled: false
addresses:
  - chain: evm
    address: "{me}"
    label: main
    chains: [ethereum]
    watch: [native, tokens]
tokens:
  - chain: ethereum
    contract: "{known}"
    symbol: USDC
    decimals: 6
"""


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(CFG.format(db=tmp_path / "c.db", me=ME, known=KNOWN))
    return cfgmod.load(p, tmp_path / "missing.env")


class Sources:
    """Stands in for PriceSources and counts what discovery is allowed to ask."""

    def __init__(self, prices):
        self.prices = prices
        self.dex_calls = 0

    async def coingecko_tokens(self, chain, contracts):
        return {c: p for c, p in self.prices.get(chain, {}).items() if c in contracts}

    async def dexscreener(self, chain, contract):
        self.dex_calls += 1
        return 1_000_000.0


def adapter(pages, metadata=None, seen=None):
    """pages: list of {tokenBalances, pageKey} answered in order."""
    queue = list(pages)
    meta = metadata or {}

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        out = []
        for call in batch:
            if seen is not None:
                seen.append((call["method"], call["params"]))
            if call["method"] == "alchemy_getTokenBalances":
                result = queue.pop(0)
            else:                                   # alchemy_getTokenMetadata
                result = meta.get(call["params"][0], {"symbol": "?", "decimals": 18})
            out.append({"jsonrpc": "2.0", "id": call["id"], "result": result})
        return httpx.Response(200, json=out)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EvmAdapter("key", {}, client=client, url_template="http://{net}/{key}")


def balances(*pairs, page_key=None):
    return {"address": ME, "pageKey": page_key,
            "tokenBalances": [{"contractAddress": c, "tokenBalance": hex(v)}
                              for c, v in pairs]}


async def test_paginates_and_drops_zero_balances(cfg):
    a = adapter([balances((GOOD, 2 * 10**18), (DUST, 0), page_key="next"),
                 balances((SPAM, 5 * 10**18))],
                metadata={GOOD: {"symbol": "GOOD", "decimals": 18}})
    src = Sources({"ethereum": {GOOD: 100.0}})
    rows, _ = await discover.collect(cfg, a, src, min_usd=10.0)
    assert [(r["symbol"], r["usd"]) for r in rows] == [("GOOD", 200.0)]


async def test_dexscreener_is_never_consulted(cfg):
    """It prices a token from its own pool, which is exactly how a scam token
    would talk its way onto the whitelist."""
    a = adapter([balances((SPAM, 10**24))])
    src = Sources({})                                # CoinGecko knows nothing
    rows, _ = await discover.collect(cfg, a, src, min_usd=1.0)
    assert rows == []
    assert src.dex_calls == 0


async def test_metadata_is_only_fetched_for_priced_tokens(cfg):
    """One metadata call per token, so asking about 400 airdrops to discard
    them all would be 400 wasted calls."""
    seen = []
    a = adapter([balances((GOOD, 10**18), (SPAM, 10**24))],
                metadata={GOOD: {"symbol": "GOOD", "decimals": 18}}, seen=seen)
    src = Sources({"ethereum": {GOOD: 50.0}})
    await discover.collect(cfg, a, src, min_usd=1.0)
    asked = [params[0] for method, params in seen
             if method == "alchemy_getTokenMetadata"]
    assert asked == [GOOD]


@pytest.mark.parametrize("min_usd,expected", [(10.0, []), (1.0, ["DUST"])])
async def test_threshold_decides_what_is_offered(cfg, min_usd, expected):
    """$2 of a real token is still $2: worth seeing at a low bar, noise at $10."""
    a = adapter([balances((DUST, 10**18))],
                metadata={DUST: {"symbol": "DUST", "decimals": 18}})
    src = Sources({"ethereum": {DUST: 2.0}})
    rows, _ = await discover.collect(cfg, a, src, min_usd=min_usd)
    assert [r["symbol"] for r in rows] == expected


async def test_already_whitelisted_tokens_are_flagged_not_hidden(cfg):
    """Seeing them confirms the config matches reality; the YAML block below
    still only offers the new ones."""
    a = adapter([balances((KNOWN, 500 * 10**6), (GOOD, 10**18))],
                metadata={KNOWN: {"symbol": "USDC", "decimals": 6},
                          GOOD: {"symbol": "GOOD", "decimals": 18}})
    src = Sources({"ethereum": {KNOWN: 1.0, GOOD: 100.0}})
    rows, _ = await discover.collect(cfg, a, src, min_usd=10.0)
    assert {r["symbol"]: r["known"] for r in rows} == {"USDC": True, "GOOD": False}
    text = discover.render(rows, 10.0)
    assert "already in config" in text
    assert GOOD in text and KNOWN not in text.split("Paste into")[1]


async def test_unpriced_holdings_are_hidden_by_default_and_shown_on_request(cfg):
    """Vault shares are listed nowhere by their own address. Without this
    flag a real four-figure position vanishes from the report in silence."""
    def build():
        return adapter([balances((GOOD, 10**18), (SPAM, 3 * 10**18))],
                       metadata={GOOD: {"symbol": "GOOD", "decimals": 18},
                                 SPAM: {"symbol": "VLTX", "decimals": 18}})
    src = Sources({"ethereum": {GOOD: 50.0}})

    rows, unpriced = await discover.collect(cfg, build(), src, min_usd=10.0)
    assert [r["symbol"] for r in rows] == ["GOOD"]
    assert unpriced == []

    rows, unpriced = await discover.collect(cfg, build(), src, min_usd=10.0,
                                            include_unpriced=True)
    assert [r["symbol"] for r in rows] == ["GOOD"]
    assert [r["symbol"] for r in unpriced] == ["VLTX"]
    assert src.dex_calls == 0, "still no guessing prices from pools"
    text = discover.render(rows, 10.0, unpriced)
    assert "Held but unpriced" in text and "VLTX" in text


async def test_metadata_for_unpriced_tokens_is_only_paid_for_on_request(cfg):
    seen = []
    a = adapter([balances((GOOD, 10**18), (SPAM, 3 * 10**18))],
                metadata={GOOD: {"symbol": "GOOD", "decimals": 18}}, seen=seen)
    src = Sources({"ethereum": {GOOD: 50.0}})
    await discover.collect(cfg, a, src, min_usd=1.0, include_unpriced=True)
    asked = {params[0] for method, params in seen
             if method == "alchemy_getTokenMetadata"}
    assert asked == {GOOD, SPAM}
