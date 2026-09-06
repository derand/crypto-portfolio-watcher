import textwrap

import pytest
from pydantic import ValidationError

from portfolio import config as cfgmod

BASE = """
db_path: data/test.db
notify:
  telegram:
    enabled: false
addresses:
{addresses}
tokens: []
"""


def write(tmp_path, addresses):
    p = tmp_path / "c.yaml"
    p.write_text(BASE.format(addresses=textwrap.indent(addresses, "  ")))
    return p


def load(tmp_path, addresses):
    return cfgmod.load(write(tmp_path, addresses), tmp_path / "missing.env")


def test_hyperliquid_only_address_needs_no_chains(tmp_path):
    """The whole point of per-address watch: 7 of 10 addresses skip Etherscan."""
    cfg = load(tmp_path, """
- chain: evm
  address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  label: hl-1
  watch: [hyperliquid]
""")
    assert cfg.addresses[0].chains == []
    assert cfg.addresses[0].address.islower(), "addresses are normalised for comparison"


def test_native_watch_without_chains_is_rejected(tmp_path):
    with pytest.raises(ValidationError, match="chains is empty"):
        load(tmp_path, """
- chain: evm
  address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  label: main
  watch: [native]
""")


def test_typo_in_watch_is_rejected_at_load(tmp_path):
    """A typo must fail now, not silently poll nothing for a month."""
    with pytest.raises(ValidationError, match="unknown watch targets"):
        load(tmp_path, """
- chain: evm
  address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  label: main
  chains: [ethereum]
  watch: [hyperliqid]
""")


def test_unknown_evm_chain_is_rejected(tmp_path):
    with pytest.raises(ValidationError, match="unknown EVM chains"):
        load(tmp_path, """
- chain: evm
  address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  label: main
  chains: [ethereum, optimism]
  watch: [native]
""")


def test_duplicate_labels_rejected(tmp_path):
    with pytest.raises(ValidationError, match="duplicate labels"):
        load(tmp_path, """
- chain: evm
  address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  label: same
  watch: [hyperliquid]
- chain: evm
  address: "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
  label: same
  watch: [hyperliquid]
""")


def test_env_expansion_and_enabled_channel_check(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("""
notify:
  telegram:
    enabled: true
    bot_token: ${TG_TOKEN}
    chat_id: ${TG_CHAT}
addresses: []
tokens: []
""")
    monkeypatch.setenv("TG_TOKEN", "secret")
    monkeypatch.setenv("TG_CHAT", "1")
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    assert cfg.notify.telegram.bot_token == "secret"
    assert cfg.notify.enabled_channels() == ["telegram"]

    monkeypatch.delenv("TG_TOKEN")
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN"):
        cfgmod.load(p, tmp_path / "missing.env")


def test_disabled_channel_tolerates_missing_secret(tmp_path):
    """An unset DISCORD_WEBHOOK_URL must not block startup while discord is off."""
    p = tmp_path / "c.yaml"
    p.write_text("""
notify:
  discord:
    enabled: false
    webhook_url: ${NOT_SET_ANYWHERE}
addresses: []
tokens: []
""")
    cfg = cfgmod.load(p, tmp_path / "missing.env")
    assert cfg.notify.enabled_channels() == []


def test_bitcoin_rejects_evm_only_options(tmp_path):
    with pytest.raises(ValidationError, match="watch: \\[native\\] only"):
        load(tmp_path, """
- chain: bitcoin
  address: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa
  label: btc
  watch: [tokens]
""")


@pytest.mark.parametrize("addr", [
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",                                # P2PKH
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",                                # P2SH
    "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97",    # P2WSH
])
def test_real_bitcoin_addresses_are_accepted(tmp_path, addr):
    cfg = load(tmp_path, f"""
- chain: bitcoin
  address: "{addr}"
  label: btc
  watch: [native]
""")
    assert cfg.addresses[0].address == addr


@pytest.mark.parametrize("addr", [
    "bc1qexample",           # the placeholder shipped in the example file
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfN0",  # base58 never contains '0'
    "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
])
def test_malformed_bitcoin_addresses_are_rejected(tmp_path, addr):
    """Catch it here, not after a month of silently watching nothing."""
    with pytest.raises(ValidationError, match="not a Bitcoin address"):
        load(tmp_path, f"""
- chain: bitcoin
  address: "{addr}"
  label: btc
  watch: [native]
""")


def test_enabled_address_with_no_usable_adapter_is_reported(tmp_path):
    """An EVM address plus a forgotten API key must not read as 'nothing to do'."""
    from portfolio.chains import missing_sources

    cfg = load(tmp_path, """
- chain: evm
  address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  label: main
  chains: [ethereum]
  watch: [native]
""")
    assert cfg.api_keys.alchemy == ""
    problems = missing_sources(cfg)
    assert len(problems) == 1
    assert "ALCHEMY_API_KEY" in problems[0]

    cfg.api_keys.alchemy = "key"
    assert missing_sources(cfg) == []


def test_disabled_address_is_not_reported_as_missing(tmp_path):
    from portfolio.chains import missing_sources

    cfg = load(tmp_path, """
- chain: evm
  address: "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  label: main
  enabled: false
  chains: [ethereum]
  watch: [native]
""")
    assert missing_sources(cfg) == []


def test_rate_call_requires_naming_the_underlying_asset():
    """A rate with nothing to convert to is a number with no meaning."""
    with pytest.raises(ValidationError, match="coingecko_id"):
        cfgmod.TokenCfg(chain="bsc", contract="0x" + "1" * 40, symbol="SHARE",
                        decimals=18, rate_call="convertToAssets(uint256)")


def test_rate_call_implies_yield_bearing():
    """A climbing rate is yield by definition; the config should not have to
    say so twice, and forgetting the second flag would alert forever."""
    t = cfgmod.TokenCfg(chain="bsc", contract="0x" + "1" * 40, symbol="SHARE",
                        decimals=18, coingecko_id="wrapped-bitcoin",
                        rate_call="convertToAssets(uint256)")
    assert t.yield_bearing is True


def test_rate_call_accepts_both_shapes_found_in_the_wild():
    """A 4626 share answers for itself and wants the amount; a vault with a
    separate accountant publishes a rate that takes nothing."""
    for sig in ("convertToAssets(uint256)", "getRate()"):
        t = cfgmod.TokenCfg(chain="bsc", contract="0x" + "1" * 40, symbol="SHARE",
                            decimals=18, coingecko_id="wrapped-bitcoin", rate_call=sig)
        assert t.rate_call == sig


def test_rate_call_must_look_like_a_view_method():
    with pytest.raises(ValidationError, match="rate_call"):
        cfgmod.TokenCfg(chain="bsc", contract="0x" + "1" * 40, symbol="SHARE",
                        decimals=18, coingecko_id="wrapped-bitcoin",
                        rate_call="getRate(address,uint256)")


def test_rate_from_without_rate_call_is_rejected():
    """An address with no question to ask it is a typo, not a config."""
    with pytest.raises(ValidationError, match="rate_from"):
        cfgmod.TokenCfg(chain="base", contract="0x" + "1" * 40, symbol="SHARE",
                        decimals=8, coingecko_id="wrapped-bitcoin",
                        rate_from="0x" + "2" * 40)


def test_beacon_watch_requires_validator_indexes():
    """Withdrawals alone do not say whose they are, and the principal cannot be
    found without an index."""
    with pytest.raises(ValidationError, match="validators"):
        cfgmod.AddressCfg(chain="evm", address="0x" + "1" * 40, label="v",
                          chains=["ethereum"], watch=["native", "beacon"])


def test_validators_without_beacon_watch_are_rejected():
    with pytest.raises(ValidationError, match="beacon"):
        cfgmod.AddressCfg(chain="evm", address="0x" + "1" * 40, label="v",
                          chains=["ethereum"], watch=["native"], validators=[999999])


def test_a_debt_token_must_name_what_it_is_denominated_in():
    """Interest is told from a borrow by what the change is worth. With no price
    there is no size, so every tick's interest would look like a new loan and
    alert - forever."""
    with pytest.raises(ValidationError, match="coingecko_id"):
        cfgmod.TokenCfg(chain="ethereum", contract="0x" + "1" * 40,
                        symbol="variableDebtUSDC", decimals=6, debt=True)


def test_debt_and_yield_bearing_cannot_both_be_set():
    """They are two rules for the same residual and yield_bearing is checked
    first, so the pair silences interest whatever its size - and a six-figure
    borrow passes without a word."""
    with pytest.raises(ValidationError, match="debt"):
        cfgmod.TokenCfg(chain="ethereum", contract="0x" + "1" * 40,
                        symbol="variableDebtUSDC", decimals=6,
                        coingecko_id="usd-coin", debt=True, yield_bearing=True)


def test_a_debt_token_with_a_price_is_accepted():
    t = cfgmod.TokenCfg(chain="ethereum", contract="0x" + "1" * 40,
                        symbol="variableDebtUSDC", decimals=6,
                        coingecko_id="usd-coin", debt=True)
    assert t.debt is True and t.yield_bearing is False
