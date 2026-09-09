"""Config loading: .env + YAML, validated by pydantic.

Two rules drive the design here:
  - an address declares what to poll (`watch`), so most EVM addresses can skip
    Etherscan entirely and only be checked on Hyperliquid;
  - a typo in `watch` or an unknown chain must fail at load, not at 3am on tick 400.
"""

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .protocols import CHAIN_SCOPED_SOURCES, PROTOCOL_SOURCES

CHAINS = {"bitcoin", "evm", "solana"}
EVM_CHAINS = {"ethereum", "bsc", "arbitrum", "base", "polygon"}
WATCH = {"native", "tokens"} | PROTOCOL_SOURCES

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
# P2PKH/P2SH (base58, no 0OIl) or bech32/bech32m. Deliberately loose - it catches
# typos and placeholders at load time without reimplementing checksum validation.
_BTC_RE = re.compile(r"^(bc1[02-9ac-hj-np-z]{11,87}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$")


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE reader. Existing environment always wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def _expand(node):
    """Replace ${VAR} anywhere in the tree. Missing vars become '' and are
    caught later by the enabled-channel validators, so a disabled channel with
    an unset key is not an error."""
    if isinstance(node, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), node)
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    return node


class TelegramCfg(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    commands: bool = True
    """Answer /portfolio and friends in that chat while `watch` runs.

    Telegram hands each update to whoever asks for it first, so two watchers
    polling the same bot token would answer half the commands each. Leave it on
    in exactly one place.
    """

    @model_validator(mode="after")
    def _need_creds(self):
        if self.enabled and not (self.bot_token and self.chat_id):
            raise ValueError("telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are empty")
        return self


class NtfyCfg(BaseModel):
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""

    @model_validator(mode="after")
    def _need_topic(self):
        if self.enabled and not self.topic:
            raise ValueError("ntfy enabled but NTFY_TOPIC is empty")
        return self


class DiscordCfg(BaseModel):
    enabled: bool = False
    webhook_url: str = ""

    @model_validator(mode="after")
    def _need_url(self):
        if self.enabled and not self.webhook_url:
            raise ValueError("discord enabled but DISCORD_WEBHOOK_URL is empty")
        return self


class NotifyCfg(BaseModel):
    telegram: TelegramCfg = TelegramCfg()
    ntfy: NtfyCfg = NtfyCfg()
    discord: DiscordCfg = DiscordCfg()

    def enabled_channels(self) -> list[str]:
        return [n for n in ("telegram", "ntfy", "discord") if getattr(self, n).enabled]


class Thresholds(BaseModel):
    notify_usd: float = 1.0
    claim_reminder_usd: float = 50.0
    liq_distance_pct: float = 15.0
    """Shout when a perp's mark price is within this percent of liquidation."""


class PricesCfg(BaseModel):
    ttl_minutes: int = 15
    command_ttl_minutes: int = 1
    """How stale a price may be when a *person* asks - a typed /portfolio or a
    tap on Refresh - as opposed to the background loop above.

    The two numbers answer different questions. The background TTL is a quota
    decision: one refresh costs four CoinGecko calls, so the loop must not pay
    for freshness nobody is looking at. A tap is the opposite case - the total
    is on screen, and on a six-figure portfolio a minute of price movement is
    visible in dollars. The cost is bounded by how often a thumb presses a
    button rather than by a timer, which is why this can be small without
    threatening the monthly quota."""

    max_age_minutes: int = 180
    """How old a price may be before it stops counting as a price at all.

    A different question from the two above, which only decide when to go and
    ask again. This one decides what to do when asking failed: past this age the
    asset is reported as unpriced rather than valued at the last number anyone
    saw. Without it a `watch` process that lost CoinGecko kept quoting whatever
    it had cached, for days, with nothing on screen to say so.

    Comfortably longer than either TTL on purpose. Equal to a TTL it would blank
    the portfolio on a single failed request; three hours is twelve background
    refreshes, so it takes a real outage rather than a blip, and an unpriced
    holding is a state the digest already knows how to say out loud."""


class DigestCfg(BaseModel):
    enabled: bool = True
    hour: int = 9
    """Local hour to send the daily summary, 0-23."""
    table_width: int = 37
    """Characters that fit on one line of a monospace block on the phone that
    reads this. A Telegram <pre> inside a collapsed block wraps rather than
    scrolling, so the tables give up symbol and label characters to stay inside
    this. Measure it rather than guess - it moves with the font size set in the
    app: send a message of numbered lines of rising length inside
    <blockquote expandable><pre>, and take the last one that stays on one row."""

    dust_usd: float = 1.0
    """Holdings worth less than this are folded into one line per group. They
    still count toward the totals; they just do not each earn a row. Raise it
    when many addresses carry small change - a dozen bot wallets holding gas
    money push the real holdings off the screen."""


class ApiKeys(BaseModel):
    alchemy: str = ""
    etherscan: str = ""
    coingecko: str = ""


class AddressCfg(BaseModel):
    chain: Literal["bitcoin", "evm", "solana"]
    address: str
    label: str
    enabled: bool = True
    watch: list[str] = Field(default_factory=lambda: ["native"])
    chains: list[str] = Field(default_factory=list)
    validators: list[int] = Field(default_factory=list)

    @field_validator("watch")
    @classmethod
    def _known_watch(cls, v):
        bad = set(v) - WATCH
        if bad:
            raise ValueError(f"unknown watch targets {sorted(bad)}; allowed: {sorted(WATCH)}")
        if not v:
            raise ValueError("watch must not be empty")
        return v

    @field_validator("chains")
    @classmethod
    def _known_chains(cls, v):
        bad = set(v) - EVM_CHAINS
        if bad:
            raise ValueError(f"unknown EVM chains {sorted(bad)}; allowed: {sorted(EVM_CHAINS)}")
        return v

    @model_validator(mode="after")
    def _shape(self):
        if self.chain == "evm":
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", self.address):
                raise ValueError(f"{self.label}: not an EVM address: {self.address}")
            object.__setattr__(self, "address", self.address.lower())
            # native/tokens need a network list, and so does any source that
            # reads one network at a time; hyperliquid-only addresses do not.
            needs_chains = ({"native", "tokens"} | CHAIN_SCOPED_SOURCES) & set(self.watch)
            if needs_chains and not self.chains:
                raise ValueError(f"{self.label}: watch includes "
                                 f"{', '.join(sorted(needs_chains))} but chains is empty")
            if "beacon" in self.watch and not self.validators:
                raise ValueError(f"{self.label}: watch includes beacon but validators "
                                 f"is empty; withdrawals alone do not say whose they are")
            if self.validators and "beacon" not in self.watch:
                raise ValueError(f"{self.label}: validators listed but beacon is not "
                                 f"in watch, so nothing would read them")
        elif self.chain == "bitcoin":
            if not _BTC_RE.fullmatch(self.address):
                raise ValueError(f"{self.label}: not a Bitcoin address: {self.address}")
            if self.chains:
                raise ValueError(f"{self.label}: 'chains' only applies to EVM addresses")
            if set(self.watch) - {"native"}:
                raise ValueError(f"{self.label}: bitcoin supports watch: [native] only")
        return self

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.address}"


class TokenCfg(BaseModel):
    chain: str
    contract: str
    symbol: str
    decimals: int
    coingecko_id: str = ""
    rate_call: str = ""
    """View method giving what one whole share redeems for, scaled by this
    token's own decimals. Either form works: "convertToAssets(uint256)" is
    handed 1e{decimals} shares, "stEthPerToken()" takes nothing."""
    rate_from: str = ""
    """Contract answering rate_call, when it is not the token itself. Some
    vaults publish the rate on a separate accountant; a share that answers for
    itself needs none of this."""
    share_decimals: int = 0
    """Decimals of the share, when they differ from the asset's. A USDC vault
    holds 18-decimal shares and answers convertToAssets in USDC's 6; a share
    using one scale for both leaves this at its default, `decimals`."""
    group: str = ""
    """Which holding this counts as in the digest: aBasWETH and WSTETH are both
    ETH. Presentation only - never a price or an amount - and deliberately not
    guessed from the symbol, because ETHFI is not ETH. Empty means the token
    stands as its own group."""
    debt: bool = False
    """True when the balance is money *owed*, not held - a variable debt token.

    Such a token is an ordinary ERC-20 with an ordinary balance, so it rides the
    same probe as everything else and costs no extra request. What it needs is a
    sign: the position is recorded negative, which is what makes the portfolio
    total subtract it instead of celebrating it.

    Interest is the mirror of vault yield - the balance climbs on its own and
    must not alert - but unlike yield it cannot simply be silenced, because
    borrowing more moves the same number. The two are told apart by size: a
    residual under `thresholds.notify_usd` is interest, anything larger is a
    borrow or a repayment. That rule needs a price, which is why coingecko_id
    is required below."""
    yield_bearing: bool = False
    """True when the balance grows on its own - an Aave aToken rebasing, or a
    share whose rate_call climbs. Such growth is yield, so it goes to the digest
    as `accrual`; without this flag it reads as money from nowhere and alerts
    every few hours forever."""

    @model_validator(mode="after")
    def _shape(self):
        if self.chain not in EVM_CHAINS:
            raise ValueError(f"{self.symbol}: unknown chain {self.chain}")
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", self.contract):
            raise ValueError(f"{self.symbol}: not a contract address: {self.contract}")
        if self.rate_from:
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", self.rate_from):
                raise ValueError(f"{self.symbol}: rate_from is not an address: "
                                 f"{self.rate_from}")
            object.__setattr__(self, "rate_from", self.rate_from.lower())
            if not self.rate_call:
                raise ValueError(f"{self.symbol}: rate_from without rate_call "
                                 f"names a contract but never asks it anything")
        if self.share_decimals and not self.rate_call:
            raise ValueError(f"{self.symbol}: share_decimals without rate_call "
                             f"scales nothing")
        if self.rate_call:
            if not re.fullmatch(r"\w+\((?:uint256)?\)", self.rate_call):
                raise ValueError(f"{self.symbol}: rate_call must look like "
                                 f"name() or name(uint256), got {self.rate_call!r}")
            if not self.coingecko_id:
                # The share is not the priced thing; without naming the asset it
                # converts to, a rate is a number with no meaning.
                raise ValueError(f"{self.symbol}: rate_call needs coingecko_id "
                                 f"naming the underlying asset")
            # A climbing rate is yield by definition; saying so twice is noise.
            object.__setattr__(self, "yield_bearing", True)
            if not self.share_decimals:
                object.__setattr__(self, "share_decimals", self.decimals)
        if self.debt:
            if self.yield_bearing:
                # Both flags claim the residual, and yield_bearing is checked
                # first: interest would be silenced whatever its size, and a
                # six-figure borrow would pass without a word.
                raise ValueError(f"{self.symbol}: debt and yield_bearing are "
                                 f"different rules for the same residual; "
                                 f"a debt token needs only debt: true")
            if not self.coingecko_id:
                # Without a price there is no size, and without a size interest
                # cannot be told from a borrow - so every tick would alert.
                raise ValueError(f"{self.symbol}: debt needs coingecko_id naming "
                                 f"the borrowed asset; interest is told from a "
                                 f"borrow by what it is worth")
        object.__setattr__(self, "contract", self.contract.lower())
        return self

    @property
    def asset_key(self) -> str:
        return f"{self.chain}:{self.contract}"


class Config(BaseModel):
    interval_minutes: int = 15
    db_path: str = "data/portfolio.db"
    thresholds: Thresholds = Thresholds()
    prices: PricesCfg = PricesCfg()
    digest: DigestCfg = DigestCfg()
    notify: NotifyCfg = NotifyCfg()
    api_keys: ApiKeys = ApiKeys()
    addresses: list[AddressCfg] = Field(default_factory=list)
    tokens: list[TokenCfg] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self):
        seen = set()
        for a in self.addresses:
            if a.key in seen:
                raise ValueError(f"duplicate address {a.address}")
            seen.add(a.key)
        labels = [a.label for a in self.addresses]
        dupes = {x for x in labels if labels.count(x) > 1}
        if dupes:
            raise ValueError(f"duplicate labels: {sorted(dupes)}")
        return self

    def validators_by_address(self) -> dict[str, list[int]]:
        """Lowercased address -> validator indexes, for the beacon source."""
        return {a.address.lower(): list(a.validators)
                for a in self.addresses if a.enabled and a.validators}

    def tokens_by_chain(self) -> dict[str, list[TokenCfg]]:
        out: dict[str, list[TokenCfg]] = {}
        for t in self.tokens:
            out.setdefault(t.chain, []).append(t)
        return out

    @property
    def own_addresses(self) -> set[str]:
        """Used to classify a transfer between our own wallets as internal."""
        return {a.address for a in self.addresses}


def load(config_path: str | Path = "config/portfolio.yaml",
         env_path: str | Path = ".env") -> Config:
    load_dotenv(Path(env_path))
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found - copy config/portfolio.example.yaml and fill it in")
    raw = yaml.safe_load(p.read_text()) or {}
    return Config.model_validate(_expand(raw))
