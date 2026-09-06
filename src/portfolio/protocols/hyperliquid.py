"""Hyperliquid perps, spot and staking.

No API key, no account: three POSTs to the public info endpoint return the whole
picture. That is why this sits early in the plan rather than with the other
protocols - it covers most of the watched EVM addresses for free.

Staking lives in its own account that the two clearinghouse calls know nothing
about, and it is split three ways: delegated to a validator, sitting idle in the
staking account, and queued for withdrawal. All three are reported separately
because reading only "delegated" shows zero for an address that has undelegated
and is waiting out the queue - which looks exactly like the coins being gone.

Sizes and prices arrive as decimal strings ("-1.5", "3412.7"). They are scaled
to integers here and never touch a float, except USD figures used only for
display and thresholds.
"""

import logging
from decimal import Decimal, InvalidOperation

import httpx

from ..chains.base import Target
from ..models import Position
from ..retry import with_retry

log = logging.getLogger(__name__)

API = "https://api.hyperliquid.xyz/info"
SCALE = 8                                  # decimals used for every scaled amount
STAKE_SYMBOL = "HYPE"                      # the only stakeable asset on Hyperliquid

# Unit's spot wrappers: one UBTC is one BTC, bridged onto Hyperliquid. Used for
# grouping only - the price still comes from the token's own order book, so a
# token that merely calls itself UBTC cannot borrow bitcoin's price.
SPOT_WRAPPERS = {"UBTC": "BTC", "UETH": "ETH", "USOL": "SOL"}

# delegatorSummary field -> our position key suffix
STAKE_BUCKETS = (("delegated", "delegated"),
                 ("undelegated", "undelegated"),
                 ("totalPendingWithdrawal", "pending"))


def _dec(text) -> Decimal:
    try:
        return Decimal(str(text))
    except (InvalidOperation, TypeError):
        return Decimal(0)


def _scaled(text) -> int:
    return int(_dec(text) * (10 ** SCALE))


class HyperliquidSource:
    name = "hyperliquid"

    def __init__(self, url: str = API, client: httpx.AsyncClient | None = None):
        self._url = url
        self._client = client
        self._own = client is None

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _info(self, body: dict) -> dict:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)

        async def call():
            r = await self._client.post(self._url, json=body)
            r.raise_for_status()
            return r.json()

        return await with_retry(call, what=f"hyperliquid {body['type']}")

    async def fetch(self, t: Target) -> tuple[list[Position], str]:
        perp = await self._info({"type": "clearinghouseState", "user": t.address})
        spot = await self._info({"type": "spotClearinghouseState", "user": t.address})
        stake = await self._info({"type": "delegatorSummary", "user": t.address})

        positions = (self._account(perp) + self._perps(perp) + self._spot(spot)
                     + self._staking(stake))
        # The marker deliberately excludes unrealised PnL: it drifts every second
        # with the mark price, and treating that as "something changed" would make
        # every tick look busy while telling us nothing.
        marker = "|".join(f"{p.key}={p.amount_raw}"
                          for p in positions if p.key != "account")
        return positions, marker

    @staticmethod
    def _account(state: dict) -> list[Position]:
        """The perp account's equity - reported, but never money of its own.

        Hyperliquid reserves perp margin inside the spot USDC balance: the spot
        row's `hold` equals marginUsed exactly, and an address holding USDC with
        no open position reports accountValue 0. So this figure is margin that
        is already in the spot balance plus unrealised PnL, and a portfolio that
        adds it to spot counts the same dollars twice.
        """
        summary = state.get("marginSummary") or {}
        value = _dec(summary.get("accountValue"))
        return [Position(
            protocol="hyperliquid", key="account", symbol="USD",
            amount_raw=_scaled(value), decimals=SCALE,
            asset_key="hyperliquid:account", usd=float(value),
            extra={"withdrawable": str(state.get("withdrawable", "0")),
                   "margin_used": str(summary.get("totalMarginUsed", "0")),
                   "position_value": str(summary.get("totalNtlPos", "0"))})]

    @staticmethod
    def _perps(state: dict) -> list[Position]:
        out = []
        for entry in state.get("assetPositions") or []:
            pos = entry.get("position") or {}
            coin = pos.get("coin")
            size = _dec(pos.get("szi"))
            if not coin or size == 0:
                continue
            notional = abs(_dec(pos.get("positionValue")))
            liq = pos.get("liquidationPx")
            mark = notional / abs(size) if size else Decimal(0)
            extra = {
                "side": "long" if size > 0 else "short",
                "entry_px": str(pos.get("entryPx") or ""),
                "liq_px": str(liq or ""),
                "mark_px": str(mark),
                "unrealized_pnl": str(pos.get("unrealizedPnl") or "0"),
                "margin_used": str(pos.get("marginUsed") or "0"),
                "leverage": str((pos.get("leverage") or {}).get("value", "")),
            }
            distance = _liq_distance(mark, _dec(liq) if liq else None)
            if distance is not None:
                extra["liq_distance_pct"] = f"{distance:.2f}"
            out.append(Position(
                protocol="hyperliquid", key=f"perp:{coin}", symbol=coin,
                amount_raw=_scaled(size), decimals=SCALE,
                asset_key=f"hyperliquid:{coin}", usd=float(notional), extra=extra))
        return out

    @staticmethod
    def _spot(state: dict) -> list[Position]:
        out = []
        for bal in state.get("balances") or []:
            total = _dec(bal.get("total"))
            if total == 0:
                continue                    # zero rows are listed for every token
            coin = bal.get("coin", "?")
            token = bal.get("token")
            out.append(Position(
                protocol="hyperliquid", key=f"spot:{coin}", symbol=coin,
                amount_raw=_scaled(total), decimals=SCALE,
                asset_key=f"hyperliquid:{coin}",
                contract="" if token is None else str(token),
                extra={"hold": str(bal.get("hold", "0"))}))
        return out


    @staticmethod
    def _staking(summary: dict) -> list[Position]:
        """The staking account, split by what the coins can currently do.

        Zero buckets are dropped like zero spot rows, which makes moving between
        them read correctly downstream: undelegating closes staking:delegated and
        opens staking:pending, rather than silently rewriting one number.
        """
        out = []
        for source_field, bucket in STAKE_BUCKETS:
            amount = _dec(summary.get(source_field))
            if amount == 0:
                continue
            extra = {}
            if bucket == "pending":
                # The API gives a count, never an unlock time; say how many.
                extra["withdrawals"] = str(summary.get("nPendingWithdrawals", 0))
            out.append(Position(
                protocol="hyperliquid", key=f"staking:{bucket}", symbol=STAKE_SYMBOL,
                amount_raw=_scaled(amount), decimals=SCALE,
                asset_key=f"hyperliquid:{STAKE_SYMBOL}", extra=extra))
        return out


def _liq_distance(mark: Decimal, liq: Decimal | None) -> Decimal | None:
    """How far the mark price is from liquidation, in percent."""
    if liq is None or liq <= 0 or mark <= 0:
        return None
    return abs(mark - liq) / mark * 100
