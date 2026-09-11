import json
import threading

import httpx
import pytest

import multicall
from portfolio import catalog
from portfolio import config as cfgmod
from portfolio import discover
from portfolio.chains import abi
from portfolio.chains.evm import MULTICALL3, EvmAdapter

ME = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
ALSO_ME = "0x2222222222222222222222222222222222222222"

PROVIDER = "0x46261bc7d0e9f2a35b4c6d8e0f1a2b3c4d5e6f70"   # PoolAddressesProvider
DATA = "0x46262ca8e1f0a3b46c5d7e9f0a1b2c3d4e5f6071"       # its data provider
AWETH = "0x46263db9f201b4c57d6e8f0a1b2c3d4e5f607182"      # a receipt we hold
AUSDC = "0x46264eca0312c5d68e7f9a0b1c2d3e4f50617283"      # one we do not
WETH = "0x46265fdb1423d6e79f8a0b1c2d3e4f5061728394"       # the underlying

COMPTROLLER = "0xacc07a1b2c3d4e5f60718293a4b5c6d7e8f90a1b"
CUSDC = "0xacc18b2c3d4e5f60718293a4b5c6d7e8f90a1b2c"
USDC = "0xacc29c3d4e5f60718293a4b5c6d7e8f90a1b2c3d"

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
tokens: []
"""

# Kept at column zero: YAML indented to match a test body is no longer YAML.
SECOND_ADDRESS = """
  - chain: evm
    address: "{me}"
    label: second
    chains: [ethereum]
    watch: [native, tokens]
"""

WHITELISTED = """
tokens:
  - chain: ethereum
    contract: "{contract}"
    symbol: aEthWETH
    decimals: 18
"""


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(CFG.format(db=tmp_path / "c.db", me=ME))
    return cfgmod.load(p, tmp_path / "missing.env")


# --- ABI encoding, so the fake provider speaks what a real one speaks --------

def word(n: int) -> str:
    return f"{n:064x}"


def enc_uint(*values: int) -> str:
    return "0x" + "".join(word(v) for v in values)


def enc_addr(a: str) -> str:
    return "0x" + word(int(a, 16))


def enc_addr_array(addresses) -> str:
    body = word(32) + word(len(addresses))
    return "0x" + body + "".join(word(int(a, 16)) for a in addresses)


def enc_string(text: str) -> str:
    raw = text.encode()
    pad = (-len(raw)) % 32
    return "0x" + word(32) + word(len(raw)) + raw.hex() + "00" * pad


def enc_sym_addr_array(pairs) -> str:
    """array of struct { string symbol; address token }, the shape Aave answers.

    The struct is dynamic, so its own head is two words - the offset of the
    string and the address - and the string starts 64 bytes in, not 32. Getting
    that wrong is what makes a decoder read a length out of an address.
    """
    heads, tails, offset = [], [], len(pairs) * 32
    for symbol, address in pairs:
        raw = symbol.encode()
        pad = (-len(raw)) % 32
        item = word(64) + word(int(address, 16)) + word(len(raw)) + raw.hex() + "00" * pad
        heads.append(word(offset))
        tails.append(item)
        offset += len(item) // 2
    return "0x" + word(32) + word(len(pairs)) + "".join(heads) + "".join(tails)


# --- the fake network -------------------------------------------------------

def adapter(answers, balances=None, seen=None):
    """answers: (to, calldata) -> hex result for eth_call; anything else reverts.
    balances: (contract, holder) -> raw, answered as alchemy_getTokenBalances.

    Balances are keyed by holder as well as contract, because the sweep asks the
    same hundred contracts for every wallet; a mock that ignored the holder
    would report one wallet's position for all of them and look like a working
    multi-address sweep.
    """
    held = balances or {}
    calls = seen if seen is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)
        out = []
        for call in batch:
            if call["method"] == "alchemy_getTokenBalances":
                who, contracts = call["params"]
                calls.append(("balances", who.lower()))
                out.append({"jsonrpc": "2.0", "id": call["id"], "result": {
                    "tokenBalances": [
                        {"contractAddress": c,
                         "tokenBalance": hex(held.get((c.lower(), who.lower()), 0))}
                        for c in contracts]}})
                continue
            params = call["params"][0]
            to, data = params["to"].lower(), params["data"]
            if to == MULTICALL3:
                # Recorded the way this handler records a plain call: by
                # selector, so the assertions below keep their meaning.
                for sub_to, sub_data in multicall.subcalls(data):
                    calls.append((sub_to, sub_data[:10]))
                out.append({"jsonrpc": "2.0", "id": call["id"],
                            "result": multicall.answer(answers, data)})
                continue
            calls.append((to, data[:10]))
            got = answers.get((to, data))
            if got is None:
                out.append({"jsonrpc": "2.0", "id": call["id"],
                            "error": {"code": 3, "message": "execution reverted"}})
            else:
                out.append({"jsonrpc": "2.0", "id": call["id"], "result": got})
        return httpx.Response(200, json=out)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EvmAdapter("key", {}, client=client,
                      url_template="http://{net}/{key}"), calls


def sel(sig):
    return abi.selector(sig)


AAVE_ENTRY = catalog.Entry(protocol="aave-v3", chain="ethereum", kind="aave_v3",
                           address=PROVIDER)
COMP_ENTRY = catalog.Entry(protocol="compound-v2", chain="ethereum",
                           kind="compound_v2", address=COMPTROLLER)

AAVE_HELD = {(AWETH, ME): 2_500_000_000_000_000_000}

AAVE_NET = {
    (PROVIDER, sel("getPoolDataProvider()")): enc_addr(DATA),
    (DATA, sel("getAllATokens()")): enc_sym_addr_array(
        [("aEthWETH", AWETH), ("aEthUSDC", AUSDC)]),
    (AWETH, sel("symbol()")): enc_string("aEthWETH"),
    (AWETH, sel("decimals()")): enc_uint(18),
    (AWETH, sel("UNDERLYING_ASSET_ADDRESS()")): enc_addr(WETH),
    (WETH, sel("decimals()")): enc_uint(18),
}


# --- the decoders -----------------------------------------------------------

def test_a_two_word_answer_does_not_become_one_enormous_number():
    """Several Compound v2 forks return (uint error, uint value) where the
    original returns one uint. Reading the payload as a single integer gives a
    number 10^60 too large - which looks like a very high exchange rate rather
    than like a bug, and would silently multiply a position by 10^60."""
    one_word = abi.decode_uint(enc_uint(1234))
    two_words = abi.decode_uint(enc_uint(0, 1234))
    assert one_word == 1234
    assert two_words == 0, "the first word is the answer, not the concatenation"


def test_symbols_of_different_lengths_all_decode():
    """Every entry in Aave's answer sits at its own offset. Assuming a fixed
    stride works until a market lists a token whose symbol crosses 32 bytes,
    and then every address after it is read from the middle of a string."""
    pairs = [("aEthWETH", AWETH), ("aEthEtherFiweETH", AUSDC),
             ("aEthLidoWETHwithAVeryLongName", WETH)]
    assert abi.decode_symbol_address_array(enc_sym_addr_array(pairs)) == [
        (s, a) for s, a in pairs]


def _within(seconds, fn, payload):
    """Run a decoder and fail rather than hang.

    What is guarded here is not a wrong answer but an unbounded loop, and there
    is no pytest-timeout in this project to notice one: without this the suite
    would stop instead of reporting.
    """
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("v", fn(payload)),
                              daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{fn.__name__} did not finish in {seconds}s"
    return out["v"]


def test_an_array_claiming_more_entries_than_the_payload_holds_is_refused():
    """A stale catalog address points at a contract answering some other ABI,
    and `catalog-check` exists to ask exactly those questions. Handed a
    plausible offset and a huge length word, the decoder walked the count it was
    given - appending zero addresses until memory ran out. That is neither of
    the two answers this module is allowed to give, and no caller can catch it.
    """
    payload = enc_uint(32, 2 ** 40)
    assert _within(5, abi.decode_address_array, payload) is None
    assert _within(5, abi.decode_symbol_address_array, payload) is None


def test_a_truncated_array_is_refused_rather_than_padded_with_zeroes():
    """The quiet half of the same bug: slicing past the end of a bytes object is
    not an error and int.from_bytes(b"") is zero, so a half-received answer used
    to decode cleanly into addresses nobody sent. A zero address means
    "unconfigured" everywhere else in this module, so the result read as a
    market listing nothing rather than as a read that failed."""
    promised_three_sent_one = enc_uint(32, 3, int(AWETH, 16))
    assert abi.decode_address_array(promised_three_sent_one) is None


def test_a_well_formed_array_is_still_read_whole():
    """The refusals above must not cost the answer they exist to protect."""
    assert abi.decode_address_array(enc_addr_array([AWETH, AUSDC])) == [AWETH, AUSDC]
    assert abi.decode_address_array(enc_addr_array([])) == []


def test_a_reverting_call_decodes_to_none_rather_than_raising():
    """A sweep asks a dozen contracts a question some of them do not implement.
    One `None` is data; an exception ends the run for every other protocol."""
    assert abi.decode_uint(None) is None
    assert abi.decode_address("0x") is None
    assert abi.decode_symbol_address_array("not hex") is None
    assert abi.decode_address(enc_addr("0x" + "0" * 40)) is None, \
        "the zero address means unconfigured, not an address"


# --- the shipped catalog ----------------------------------------------------

def test_the_shipped_catalog_is_well_formed():
    """It is data, not config: a typo here is a bug that ships, and it fails
    quietly - a wrong address lists nothing and the positions behind it read as
    absent rather than as an error."""
    entries = catalog.load()
    assert entries, "the catalog is empty"
    for e in entries:
        assert e.kind in catalog.KINDS
        assert e.address == e.address.lower()
        assert len(e.address) == 42 and e.address.startswith("0x")
    labels = [e.label for e in entries]
    assert len(labels) == len(set(labels)), "one row per protocol per chain"


def test_the_catalog_names_no_holding():
    """The file is public. It says which protocols a reader can ask, never
    which are held - that distinction is the whole of PLAN §14."""
    entries = catalog.load()
    assert all(not hasattr(e, "amount") for e in entries)
    assert {e.chain for e in entries} <= {"ethereum", "arbitrum", "base",
                                          "bsc", "polygon"}


# --- the sweep --------------------------------------------------------------

async def test_only_receipt_tokens_with_a_balance_are_proposed(cfg):
    """A market lists every token it has ever supported; an address holds two of
    them. Proposing the whole listing would bury the position in sixty-five
    rows of zero."""
    a, _ = adapter(AAVE_NET, AAVE_HELD)
    rows = await discover.collect_protocols(cfg, a, [AAVE_ENTRY])
    assert [r["symbol"] for r in rows] == ["aEthWETH"]
    assert rows[0]["holders"] == {"main": 2_500_000_000_000_000_000}
    assert rows[0]["protocol"] == "aave-v3/ethereum"

    text = discover.render_protocols(rows)
    assert f'contract: "{AWETH}"' in text
    assert "yield_bearing: true" in text, "an aToken rebases; without the flag it alerts"
    assert WETH in text, "the underlying is named so the coingecko id can be found"


async def test_a_ctoken_is_proposed_as_a_share_against_a_rate(cfg):
    """A cToken balance does not move while the position grows: the value is in
    exchangeRateStored(), scaled by 1e18 whatever the underlying's decimals are.
    Proposed as a plain balance it would report eight decimals of share as if
    they were dollars, and never grow."""
    net = {
        (COMPTROLLER, sel("getAllMarkets()")): enc_addr_array([CUSDC]),
        (CUSDC, sel("symbol()")): enc_string("cUSDC"),
        (CUSDC, sel("decimals()")): enc_uint(8),
        (CUSDC, sel("underlying()")): enc_addr(USDC),
        (USDC, sel("decimals()")): enc_uint(6),
    }
    # 5000 cUSDC at eight decimals
    a, _ = adapter(net, {(CUSDC, ME): 500_000_000_000})
    rows = await discover.collect_protocols(cfg, a, [COMP_ENTRY])
    assert [r["symbol"] for r in rows] == ["cUSDC"]

    text = discover.render_protocols(rows)
    assert 'rate_call: "exchangeRateStored()"' in text
    assert "share_decimals: 18" in text
    assert "decimals: 6" in text, "the position is measured in the underlying"
    assert "yield_bearing" not in text, "the rate implies it; saying it twice is noise"


async def test_a_stale_entry_does_not_take_the_rest_of_the_sweep_down(cfg):
    """A catalog address goes stale when a protocol redeploys. That must cost
    one warning and that protocol's positions, not the whole run - the same
    rule the tick follows for a failing scope."""
    dead = catalog.Entry(protocol="gone", chain="ethereum", kind="aave_v3",
                         address="0x" + "0" * 39 + "1")
    a, _ = adapter(AAVE_NET, AAVE_HELD)
    rows = await discover.collect_protocols(cfg, a, [dead, AAVE_ENTRY])
    assert [r["symbol"] for r in rows] == ["aEthWETH"]


async def test_a_market_is_enumerated_once_for_every_address(tmp_path):
    """Enumeration does not depend on whose balance is asked. Repeating it per
    address turns a twelve-wallet sweep into twelve times the requests for an
    answer that cannot have changed."""
    p = tmp_path / "c.yaml"
    base = CFG.format(db=tmp_path / "c.db", me=ME)
    p.write_text(base.replace("tokens: []", "")
                 + SECOND_ADDRESS.format(me=ALSO_ME) + "tokens: []\n")
    cfg = cfgmod.load(p, tmp_path / "missing.env")

    a, calls = adapter(AAVE_NET, AAVE_HELD | {(AWETH, ALSO_ME): 10 ** 18})
    rows = await discover.collect_protocols(cfg, a, [AAVE_ENTRY])

    assert rows[0]["holders"] == {"main": 2_500_000_000_000_000_000,
                                  "second": 10 ** 18}
    listing = [c for c in calls if c[1] == sel("getAllATokens()")]
    assert len(listing) == 1, f"enumerated {len(listing)} times"


async def test_a_position_already_whitelisted_is_flagged_not_reproposed(tmp_path):
    """Discovery is run repeatedly. Re-proposing what is already watched trains
    the reader to skip the output, which is how the one new line gets missed."""
    p = tmp_path / "c.yaml"
    p.write_text(CFG.format(db=tmp_path / "c.db", me=ME)
                 .replace("tokens: []", WHITELISTED.format(contract=AWETH)))
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    a, _ = adapter(AAVE_NET, AAVE_HELD)
    rows = await discover.collect_protocols(cfg, a, [AAVE_ENTRY])
    assert rows[0]["known"] is True
    text = discover.render_protocols(rows)
    assert "[already in config]" in text
    assert "Paste into" not in text, "nothing new to paste"


async def test_a_native_coin_market_is_still_a_share(cfg):
    """Venus lists vBNB, a cToken for the native coin, and a native coin has no
    contract to ask for decimals. Inferring the shape from "eight decimals over
    a different underlying" gets this one wrong and proposes it as a plain
    rebasing balance - which would count eight decimals of share as if they
    were BNB, off by ten orders of magnitude."""
    VBNB = "0xacc3ad4e5f60718293a4b5c6d7e8f90a1b2c3d4e"
    net = {
        (COMPTROLLER, sel("getAllMarkets()")): enc_addr_array([VBNB]),
        (VBNB, sel("symbol()")): enc_string("vBNB"),
        (VBNB, sel("decimals()")): enc_uint(8),
        # no underlying(): the native market has no token behind it
    }
    a, _ = adapter(net, {(VBNB, ME): 100_000_000})
    rows = await discover.collect_protocols(cfg, a, [COMP_ENTRY])
    assert rows[0]["underlying"] is None

    text = discover.render_protocols(rows)
    assert 'rate_call: "exchangeRateStored()"' in text
    assert "decimals: 18" in text, "the position is measured in the native coin"
    assert "the native coin" in text


async def test_a_debt_token_is_proposed_as_debt(cfg):
    """A variable debt token is an ordinary ERC-20, so watching it costs nothing
    extra - it rides the same probe. What it needs is the flag that gives it a
    sign; proposed without one it would be added to net worth instead of taken
    off it."""
    VDEBT = "0x46266ea1b2c3d4e5f60718293a4b5c6d7e8f9012"
    net = dict(AAVE_NET)
    net[(DATA, sel("getAllReservesTokens()"))] = enc_sym_addr_array([("WETH", WETH)])
    net[(DATA, abi.encode_address("getReserveTokensAddresses(address)", WETH))] = (
        "0x" + word(int(AWETH, 16)) + word(0) + word(int(VDEBT, 16)))
    net[(VDEBT, sel("symbol()"))] = enc_string("variableDebtEthWETH")
    net[(VDEBT, sel("decimals()"))] = enc_uint(18)
    net[(VDEBT, sel("UNDERLYING_ASSET_ADDRESS()"))] = enc_addr(WETH)

    a, _ = adapter(net, {(VDEBT, ME): 3 * 10 ** 18})
    rows = await discover.collect_protocols(cfg, a, [AAVE_ENTRY])
    assert [r["symbol"] for r in rows] == ["variableDebtEthWETH"]
    assert rows[0]["debt"] is True

    text = discover.render_protocols(rows)
    assert "debt: true" in text
    assert "yield_bearing" not in text, "the debt rule replaces it, never joins it"
    assert "owed" in text, "the table says which way the money points"


async def test_an_emptied_position_is_not_reported_as_one(cfg):
    """Withdrawing the liquidity leaves the NFT in the wallet holding nothing.
    Counting NFTs would report five positions to an address that has none, and
    tell it to start watching something that is not there - which was exactly
    what the first live run said."""
    NFPM = "0x46267ab2c3d4e5f60718293a4b5c6d7e8f901234"
    entry = catalog.Entry(protocol="pancakeswap-v3", chain="ethereum",
                          kind="univ3", address=NFPM)
    live = "0x" + word(0) * 7 + word(12345) + word(0) * 4
    dead = "0x" + word(0) * 7 + word(0) + word(0) * 4
    net = {
        (NFPM, abi.encode_address("balanceOf(address)", ME)): enc_uint(2),
        (NFPM, abi.selector("tokenOfOwnerByIndex(address,uint256)")
         + word(int(ME, 16)) + word(0)): enc_uint(11),
        (NFPM, abi.selector("tokenOfOwnerByIndex(address,uint256)")
         + word(int(ME, 16)) + word(1)): enc_uint(22),
        (NFPM, abi.selector("positions(uint256)") + word(11)): live,
        (NFPM, abi.selector("positions(uint256)") + word(22)): dead,
    }
    a, _ = adapter(net)
    rows = await discover.collect_nfts(cfg, a, [entry])
    assert rows[0]["count"] == 2 and rows[0]["live"] == 1

    text = discover.render_nfts(rows)
    assert "1 with liquidity" in text and "1 closed but not burned" in text
    assert "Add `univ3`" in text, "the live one is still worth watching"


async def test_an_address_whose_positions_are_all_closed_is_not_told_to_watch(cfg):
    NFPM = "0x46268bc3d4e5f60718293a4b5c6d7e8f90123456"
    entry = catalog.Entry(protocol="pancakeswap-v3", chain="ethereum",
                          kind="univ3", address=NFPM)
    net = {
        (NFPM, abi.encode_address("balanceOf(address)", ME)): enc_uint(1),
        (NFPM, abi.selector("tokenOfOwnerByIndex(address,uint256)")
         + word(int(ME, 16)) + word(0)): enc_uint(11),
        (NFPM, abi.selector("positions(uint256)") + word(11)):
            "0x" + word(0) * 12,
    }
    a, _ = adapter(net)
    text = discover.render_nfts(await discover.collect_nfts(cfg, a, [entry]))
    assert "0 with liquidity" in text
    assert "Add `univ3`" not in text
