import httpx
import pytest

from portfolio.prices import PriceBook
from portfolio.prices.sources import PriceSources

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ODD = "0x1234567890123456789012345678901234567890"


def client(routes):
    """routes: (method, path-substring) -> payload."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        for (method, needle), payload in routes.items():
            if request.method == method and needle in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def sources(routes):
    http, seen = client(routes)
    return PriceSources(client=http), seen


async def test_dexscreener_ignores_pools_on_other_chains():
    """The trap: querying by contract address returns pairs from every network
    the address exists on. For Ethereum USDC the deepest pool was an unrelated
    PulseChain token at $0.0009 - a thousandfold error if taken at face value."""
    s, _ = sources({("GET", "dexscreener"): {"pairs": [
        {"chainId": "pulsechain", "priceUsd": "0.0009121",
         "baseToken": {"address": USDC}, "liquidity": {"usd": 7442345.18}},
        {"chainId": "ethereum", "priceUsd": "0.999838",
         "baseToken": {"address": USDC}, "liquidity": {"usd": 120000.0}},
    ]}})
    price = await s.dexscreener("ethereum", USDC)
    assert price == pytest.approx(0.999838)


async def test_dexscreener_ignores_pairs_where_the_token_is_the_quote():
    s, _ = sources({("GET", "dexscreener"): {"pairs": [
        {"chainId": "ethereum", "priceUsd": "3400.0",
         "baseToken": {"address": ODD}, "liquidity": {"usd": 900000.0}},
        {"chainId": "ethereum", "priceUsd": "1.0001",
         "baseToken": {"address": USDC}, "liquidity": {"usd": 5000.0}},
    ]}})
    assert await s.dexscreener("ethereum", USDC) == pytest.approx(1.0001)


async def test_dexscreener_returns_none_when_the_chain_has_no_pool():
    s, _ = sources({("GET", "dexscreener"): {"pairs": [
        {"chainId": "solana", "priceUsd": "1.0",
         "baseToken": {"address": USDC}, "liquidity": {"usd": 10.0}}]}})
    assert await s.dexscreener("ethereum", USDC) is None


async def test_hyperliquid_mids_drop_numeric_pair_indices():
    s, _ = sources({("POST", "hyperliquid"): {
        "BTC": "77055.5", "ETH": "2382.55", "#12090": "0.28981"}})
    mids = await s.hyperliquid_mids()
    assert mids == {"BTC": 77055.5, "ETH": 2382.55}


def make_book(conn, routes, ttl=15):
    http, seen = client(routes)
    return PriceBook(conn, PriceSources(client=http), ttl_minutes=ttl), seen


def register(conn, asset_key, chain, contract, symbol, coingecko_id=None):
    conn.execute("""INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                                       coingecko_id, kind, whitelisted)
                    VALUES (?,?,?,?,?,?,?,1)""",
                 (asset_key, chain, contract, symbol, 18 if not contract else 6,
                  coingecko_id, "native" if not contract else "erc20"))
    return {"asset_key": asset_key, "chain": chain, "contract": contract,
            "symbol": symbol, "coingecko_id": coingecko_id}


@pytest.fixture()
def conn(tmp_path):
    from portfolio import db as dbmod
    c = dbmod.connect(tmp_path / "p.db")
    dbmod.init(c)
    return c


async def test_native_and_token_prices_are_batched(conn):
    eth = register(conn, "ethereum:native", "ethereum", None, "ETH", "ethereum")
    usdc = register(conn, f"ethereum:{USDC}", "ethereum", USDC, "USDC")
    book, seen = make_book(conn, {
        ("GET", "simple/price"): {"ethereum": {"usd": 2384.93}},
        ("GET", "token_price"): {USDC: {"usd": 0.999838}}})
    await book.refresh([eth, usdc])
    assert book.usd("ethereum:native") == pytest.approx(2384.93)
    assert book.usd(f"ethereum:{USDC}") == pytest.approx(0.999838)
    assert len(seen) == 2, "one call per source, not one per asset"


async def test_token_unknown_to_coingecko_falls_back_to_dexscreener(conn):
    odd = register(conn, f"base:{ODD}", "base", ODD, "ODD")
    book, _ = make_book(conn, {
        ("GET", "token_price"): {},                       # CoinGecko never heard of it
        ("GET", "dexscreener"): {"pairs": [
            {"chainId": "base", "priceUsd": "0.4242",
             "baseToken": {"address": ODD}, "liquidity": {"usd": 50000.0}}]}})
    await book.refresh([odd])
    assert book.usd(f"base:{ODD}") == pytest.approx(0.4242)


async def test_asset_nobody_prices_stays_unpriced_not_zero(conn):
    """A missing price must never become a zero valuation."""
    odd = register(conn, f"base:{ODD}", "base", ODD, "ODD")
    book, _ = make_book(conn, {("GET", "token_price"): {},
                               ("GET", "dexscreener"): {"pairs": []}})
    await book.refresh([odd])
    assert book.usd(f"base:{ODD}") is None
    assert book.value(f"base:{ODD}", 10**18, 18) is None


async def test_cached_price_is_reused_within_the_ttl(conn):
    eth = register(conn, "ethereum:native", "ethereum", None, "ETH", "ethereum")
    book, seen = make_book(conn, {("GET", "simple/price"): {"ethereum": {"usd": 2384.93}}})
    await book.refresh([eth])
    second, seen2 = make_book(conn, {("GET", "simple/price"): {"ethereum": {"usd": 9999.0}}})
    await second.refresh([eth])
    assert second.usd("ethereum:native") == pytest.approx(2384.93)
    assert seen2 == [], "nothing refetched while the cached price is still fresh"


async def test_expired_price_is_refetched(conn):
    eth = register(conn, "ethereum:native", "ethereum", None, "ETH", "ethereum")
    book, _ = make_book(conn, {("GET", "simple/price"): {"ethereum": {"usd": 2384.93}}})
    await book.refresh([eth])
    stale, _ = make_book(conn, {("GET", "simple/price"): {"ethereum": {"usd": 2500.0}}}, ttl=0)
    await stale.refresh([eth])
    assert stale.usd("ethereum:native") == pytest.approx(2500.0)


async def test_a_shorter_ttl_passed_per_call_overrides_the_configured_one(conn):
    """The tick loop and a person asking want different freshness out of the
    same book: the loop pays four CoinGecko calls on a timer, a command pays
    them because someone is looking at the number. Without the override the
    long TTL would hand a tapped Refresh an hour-old price."""
    eth = register(conn, "ethereum:native", "ethereum", None, "ETH", "ethereum")
    book, _ = make_book(conn, {("GET", "simple/price"): {"ethereum": {"usd": 2384.93}}})
    await book.refresh([eth])

    asked, seen = make_book(conn, {("GET", "simple/price"): {"ethereum": {"usd": 2500.0}}},
                            ttl=60)
    await asked.refresh([eth], ttl_minutes=0)
    assert asked.usd("ethereum:native") == pytest.approx(2500.0)
    assert seen, "the configured hour must not survive an explicit override"


async def test_hyperliquid_quote_currency_is_worth_one_dollar(conn):
    usdc = register(conn, "hyperliquid:USDC", "hyperliquid", None, "USDC")
    btc = register(conn, "hyperliquid:BTC", "hyperliquid", None, "BTC")
    book, _ = make_book(conn, {("POST", "hyperliquid"): {"BTC": "77055.5"}})
    await book.refresh([usdc, btc])
    # USDC is the quote currency and never appears in allMids.
    assert book.usd("hyperliquid:USDC") == 1.0
    assert book.usd("hyperliquid:BTC") == pytest.approx(77055.5)


async def test_value_scales_by_decimals(conn):
    usdc = register(conn, f"ethereum:{USDC}", "ethereum", USDC, "USDC")
    book, _ = make_book(conn, {("GET", "token_price"): {USDC: {"usd": 1.0}}})
    await book.refresh([usdc])
    assert book.value(f"ethereum:{USDC}", 47_821_135, 6) == pytest.approx(47.821135)


async def test_token_with_an_explicit_coingecko_id_is_priced_as_that_asset(conn):
    """A vault share is listed nowhere by its own address, so the id names the
    asset it redeems for instead."""
    share = register(conn, f"bsc:{ODD}", "bsc", ODD, "vBTC", "wrapped-bitcoin")
    book, seen = make_book(conn, {
        ("GET", "simple/price"): {"wrapped-bitcoin": {"usd": 77982.0}}})
    await book.refresh([share])
    assert book.usd(f"bsc:{ODD}") == pytest.approx(77982.0)
    assert not any("token_price" in url for url in seen), \
        "an explicit id replaces the contract lookup, it does not race it"


def hl_client(all_mids, spot):
    """Both Hyperliquid calls hit one URL; only the body says which is which."""
    import json as jsonlib

    def handler(request: httpx.Request) -> httpx.Response:
        kind = jsonlib.loads(request.content)["type"]
        return httpx.Response(200, json=all_mids if kind == "allMids" else spot)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def spot_meta(tokens, pairs, contexts):
    return [{"tokens": [{"index": i, "name": n} for i, n in tokens.items()],
             "universe": pairs}, contexts]


async def test_spot_contexts_are_matched_by_name_not_by_position():
    """The trap that cost an afternoon: the context list is longer than the pair
    list and is not parallel to it. Zipping them prices UBTC off a dead market
    at six hundredths of a cent instead of eighty-one thousand dollars."""
    s = PriceSources(client=hl_client({}, spot_meta(
        {0: "USDC", 197: "UBTC"},
        [{"tokens": [197, 0], "name": "@142"}],
        [{"coin": "@999", "midPx": "0.00006", "dayNtlVlm": "0.0"},
         {"coin": "@142", "midPx": "81209.5", "dayNtlVlm": "50243349.0"}])))
    assert (await s.hyperliquid_spot_mids())["197"] == pytest.approx(81209.5)


async def test_spot_books_quoted_in_something_else_are_not_dollars():
    """UBTC/USDH exists and is empty. Reading its mid as a dollar price would
    value a bitcoin holding at whatever that other stablecoin's book says."""
    s = PriceSources(client=hl_client({}, spot_meta(
        {0: "USDC", 360: "USDH", 197: "UBTC"},
        [{"tokens": [197, 360], "name": "@234"}],
        [{"coin": "@234", "midPx": "1.0", "dayNtlVlm": "0.0"}])))
    assert await s.hyperliquid_spot_mids() == {}


async def test_the_busiest_dollar_book_wins():
    """One token can list several USDC books, most of them empty shells."""
    s = PriceSources(client=hl_client({}, spot_meta(
        {0: "USDC", 197: "UBTC"},
        [{"tokens": [197, 0], "name": "@142"}, {"tokens": [197, 0], "name": "@700"}],
        [{"coin": "@142", "midPx": "81209.5", "dayNtlVlm": "50243349.0"},
         {"coin": "@700", "midPx": "3.5", "dayNtlVlm": "12.0"}])))
    assert (await s.hyperliquid_spot_mids())["197"] == pytest.approx(81209.5)


async def test_a_spot_token_is_priced_by_its_index_not_its_name(conn):
    """Names on Hyperliquid spot are chosen by whoever deploys the token. Two
    holdings can both call themselves UBTC; only the index says which order
    book values which one, and pricing by name gives an impostor bitcoin's
    price."""
    real = register(conn, "hyperliquid:UBTC", "hyperliquid", "197", "UBTC")
    fake = register(conn, "hyperliquid:UBTC2", "hyperliquid", "981", "UBTC")
    book = PriceBook(conn, PriceSources(client=hl_client({"BTC": "81000.0"}, spot_meta(
        {0: "USDC", 197: "UBTC", 981: "UBTC"},
        [{"tokens": [197, 0], "name": "@142"}, {"tokens": [981, 0], "name": "@900"}],
        [{"coin": "@142", "midPx": "81209.5", "dayNtlVlm": "50243349.0"},
         {"coin": "@900", "midPx": "0.004", "dayNtlVlm": "3.0"}]))), ttl_minutes=15)
    await book.refresh([real, fake])
    assert book.usd("hyperliquid:UBTC") == pytest.approx(81209.5)
    assert book.usd("hyperliquid:UBTC2") == pytest.approx(0.004)


async def test_a_perp_row_that_kept_a_coin_name_is_still_priced(conn):
    """Assets registered before token indexes were recorded carry the coin name
    in the same column. Reading that as an index loses the price of every perp
    the watcher has held since before the change."""
    btc = register(conn, "hyperliquid:BTC", "hyperliquid", "BTC", "BTC")
    book = PriceBook(conn, PriceSources(client=hl_client({"BTC": "81000.0"}, spot_meta(
        {0: "USDC"}, [], []))), ttl_minutes=15)
    await book.refresh([btc])
    assert book.usd("hyperliquid:BTC") == pytest.approx(81000.0)
