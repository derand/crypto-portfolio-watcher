"""EVM networks via Alchemy.

Why Alchemy and not Etherscan: Etherscan's free tier refuses Base and BNB
outright ("Free API access is not supported for this chain"), and one
alchemy_getAssetTransfers call returns native, internal and ERC-20 movements
together, where Etherscan needs three separate endpoints.

BNB Smart Chain is covered as well, with one gap: the "internal" category is
not supported there (-32602), exactly as on Arbitrum, so BNB moved by a
contract shows up only as an unexplained balance delta. Beware that a network
also has to be enabled per-app in the Alchemy dashboard; until it is, every
method on it answers 403 - including eth_blockNumber, which makes a missing
toggle look like a missing network.
"""

import asyncio
import logging

import httpx
from eth_utils import keccak

from ..models import BalanceSnapshot, Direction, Probe, Transfer
from ..retry import Permanent, Unavailable, redact, with_retry
from .base import AddressState, Cursor, Target

log = logging.getLogger(__name__)

# network -> (alchemy subdomain, native symbol, supports the "internal" category)
NETWORKS = {
    "ethereum": ("eth-mainnet", "ETH", True),
    "arbitrum": ("arb-mainnet", "ETH", False),   # no internal transfers on Alchemy
    "base":     ("base-mainnet", "ETH", True),
    "bsc":      ("bnb-mainnet", "BNB", False),   # no internal transfers either
}
NATIVE_DECIMALS = 18
MAX_PAGES = 5
DISCOVER_PAGES = 20                        # ~100 tokens a page; spam runs deep
METADATA_BATCH = 20                        # metadata calls per JSON-RPC batch
BALANCES_BATCH = 100                       # contracts per alchemy_getTokenBalances
CALL_BATCH = 10
"""eth_calls per JSON-RPC batch, sized by the compute limit rather than by HTTP.

Alchemy's free tier allows about 330 compute units a second and charges 26 for
an eth_call, so a batch of fifty is four times over the line and answers 429 -
which a catalog sweep meets immediately, because it asks hundreds of questions
back to back with no think time between them. Ten fits; the sweep is a manual
command, so the extra round trips cost nobody anything."""
CALL_PACE = 1.0                            # seconds between sweep batches
RATE_MARKER_DP = 4                         # decimals of the rate the marker sees


def _rate_calldata(signature: str, decimals: int) -> str:
    """Calldata asking what one whole share currently redeems for.

    Two shapes in the wild: an ERC-4626 vault answers on the share itself and
    wants a share amount, while others publish a no-argument rate - on the token
    (wstETH's stEthPerToken()) or on a separate accountant contract. The
    argument is scaled by the *share*, which is not always the asset: an
    18-decimal share of 6-decimal USDC is an ordinary vault, not a curiosity.

    Asking per whole share rather than per actual balance keeps the probe to a
    single round trip, at the cost of the rate's own truncation: on a 6-decimal
    rate over 4390 shares that is a quarter of a cent, far under any threshold.
    """
    data = "0x" + keccak(text=signature)[:4].hex()
    if signature.endswith("(uint256)"):
        data += f"{10 ** decimals:064x}"
    return data


def _hex(value) -> int:
    if value in (None, "", "0x"):
        return 0
    return int(value, 16) if isinstance(value, str) else int(value)


class EvmAdapter:
    chain = "evm"

    def __init__(self, api_key: str, tokens: dict[str, list] | None = None,
                 client: httpx.AsyncClient | None = None,
                 url_template: str = "https://{net}.g.alchemy.com/v2/{key}",
                 withdrawals=None):
        self._key = api_key
        self._tokens = tokens or {}          # network -> [TokenCfg]
        self._withdrawals = withdrawals      # BeaconWithdrawals, or None
        self._client = client
        self._own = client is None
        self._url = url_template
        self._id = 0
        self._warned: set[str] = set()

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._withdrawals is not None:
            await self._withdrawals.aclose()

    def scopes(self, t: Target) -> list[str]:
        # beacon counts: withdrawals land in the native balance, so an address
        # watching only them still has to be polled here.
        if not ({"native", "tokens", "beacon"} & t.watch):
            return []                        # hyperliquid-only address: nothing here
        # Beacon withdrawals are an Ethereum L1 fact, not a user preference,
        # and config only demands `chains` when native/tokens are watched. An
        # address watching beacon alone would otherwise get an empty scope list
        # and never be polled - the balances would still arrive from the beacon
        # source, so the gap is silent: numbers present, withdrawals missing.
        chains = t.chains or (("ethereum",) if "beacon" in t.watch else ())
        usable = []
        for c in chains:
            if c in NETWORKS:
                usable.append(c)
            elif c not in self._warned:
                # Say it out loud rather than quietly watching nothing.
                self._warned.add(c)
                log.warning("%s: no transfer source for %s yet; skipping it", t.label, c)
        return usable

    async def _rpc(self, scope: str, calls: list[tuple[str, list]],
                   allow_errors: bool = False, attempts: int = 3,
                   base: float = 1.0) -> list:
        """One HTTP request carrying a JSON-RPC batch.

        Batching is the point: a quiet tick for one address on one network costs
        a single round trip even though it asks three questions.

        `allow_errors` returns None for the calls that failed instead of raising
        on the first one. The tick wants the opposite - a probe that half worked
        is a probe that lies - but a catalog sweep asks a dozen contracts a
        question some of them do not answer, and one revert there is data, not
        a failure.
        """
        net = NETWORKS[scope][0]
        payload = []
        for method, params in calls:
            self._id += 1
            payload.append({"jsonrpc": "2.0", "id": self._id,
                            "method": method, "params": params})
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        url = self._url.format(net=net, key=self._key)

        async def call():
            r = await self._client.post(url, json=payload)
            if r.status_code in (401, 403):
                raise Permanent(f"alchemy {r.status_code}: check ALCHEMY_API_KEY")
            if r.status_code >= 400:
                # Not raise_for_status(): its message carries the whole URL, and
                # the Alchemy key is a path segment of it. This text reaches the
                # scan result and from there a Telegram message.
                raise RuntimeError(f"alchemy {scope}: HTTP {r.status_code}")
            return r.json()

        try:
            body = await with_retry(call, what=f"alchemy {scope}",
                                    attempts=attempts, base=base)
        except Unavailable as e:
            raise Unavailable(redact(str(e), self._key)) from None
        if isinstance(body, dict):
            body = [body]
        by_id = {item["id"]: item for item in body}
        out = []
        for item in payload:
            got = by_id.get(item["id"], {})
            if "error" in got:
                msg = got["error"].get("message", "")
                if not allow_errors:
                    raise Permanent(f"alchemy {item['method']} on {scope}: {msg}")
                log.debug("%s on %s: %s", item["method"], scope, msg)
                out.append(None)
                continue
            out.append(got.get("result"))
        return out

    async def eth_call_many(self, scope: str, calls: list[tuple[str, str]],
                            chunk: int = CALL_BATCH) -> list:
        """`eth_call` a list of (to, calldata), several per round trip.

        Answers line up with `calls`; a call that reverted or hit a contract
        with no such method comes back None. Chunked because a batch of a
        hundred is refused by the provider rather than served slowly, and a
        catalog sweep across an Aave market reaches that size easily.
        """
        out: list = []
        for i in range(0, len(calls), chunk):
            if i:
                # Pace the batches rather than only retrying them. Enumerating
                # every reserve of every market on Ethereum is a hundred-odd
                # calls issued back to back - some 3000 compute units inside one
                # second against a limit of 330 - and backoff only stretches the
                # failure out. One batch a second stays under the line; the
                # sweep is a manual command and can afford the wall clock.
                await asyncio.sleep(CALL_PACE)
            batch = [("eth_call", [{"to": to, "data": data}, "latest"])
                     for to, data in calls[i:i + chunk]]
            # A sweep is the one caller that reliably meets the per-second
            # compute limit: it asks hundreds of questions back to back with no
            # think time between them. Waiting longer is free here - nobody is
            # watching a manual command - and a 429 that ends the run costs the
            # whole sweep.
            out.extend(await self._rpc(scope, batch, allow_errors=True,
                                       attempts=5, base=2.0))
        return out

    def _whitelist(self, scope: str) -> list:
        return self._tokens.get(scope, [])

    async def held_tokens(self, t: Target, scope: str) -> dict[str, int]:
        """Every ERC-20 this address holds a non-zero balance of.

        Deliberately unused by the tick. The whitelist stays manual (PLAN §2)
        precisely because an address accumulates hundreds of airdropped tokens;
        this only proposes candidates for a human to approve.
        """
        out: dict[str, int] = {}
        page = None
        for _ in range(DISCOVER_PAGES):
            params = [t.address, "erc20"]
            if page:
                params.append({"pageKey": page})
            (res,) = await self._rpc(scope, [("alchemy_getTokenBalances", params)])
            for entry in (res or {}).get("tokenBalances", []):
                raw = _hex(entry.get("tokenBalance"))
                if raw:
                    out[entry["contractAddress"].lower()] = raw
            page = (res or {}).get("pageKey")
            if not page:
                break
        else:
            log.warning("%s/%s: stopped after %d pages of token balances",
                        t.label, scope, DISCOVER_PAGES)
        return out

    async def balances_of(self, scope: str, address: str,
                          contracts: list[str]) -> dict[str, int]:
        """Balances of named contracts, a hundred per request.

        The same call `probe` uses, and the reason a catalog sweep is affordable
        at all: asking `balanceOf` as one eth_call per token costs 26 compute
        units each and meets the free tier's per-second limit inside one Aave
        market, while this answers a hundred contracts in a single request.
        """
        out: dict[str, int] = {}
        for i in range(0, len(contracts), BALANCES_BATCH):
            chunk = contracts[i:i + BALANCES_BATCH]
            (res,) = await self._rpc(scope, [("alchemy_getTokenBalances",
                                              [address, chunk])],
                                     attempts=5, base=2.0)
            for entry in (res or {}).get("tokenBalances", []):
                raw = _hex(entry.get("tokenBalance"))
                if raw:
                    out[entry["contractAddress"].lower()] = raw
        return out

    async def token_metadata(self, scope: str, contracts: list[str]) -> dict[str, dict]:
        """Symbol and decimals, several per round trip."""
        out: dict[str, dict] = {}
        for i in range(0, len(contracts), METADATA_BATCH):
            chunk = contracts[i:i + METADATA_BATCH]
            results = await self._rpc(scope, [("alchemy_getTokenMetadata", [c])
                                              for c in chunk])
            for contract, meta in zip(chunk, results):
                out[contract] = meta or {}
        return out

    async def probe(self, t: Target, scope: str, cursor: Cursor) -> Probe:
        """Three questions, one round trip: tip, native balance, token balances.

        Balances are the probe rather than a separate step because a token move
        leaves the native balance untouched - watching only ETH would miss a
        USDC transfer entirely.
        """
        whitelist = self._whitelist(scope) if "tokens" in t.watch else []
        contracts = [tok.contract for tok in whitelist]
        rate_tokens = [tok for tok in whitelist if tok.rate_call]
        calls = [("eth_blockNumber", []),
                 ("eth_getBalance", [t.address, "latest"])]
        if contracts:
            calls.append(("alchemy_getTokenBalances", [t.address, contracts]))
        for tok in rate_tokens:
            calls.append(("eth_call", [
                {"to": tok.rate_from or tok.contract,
                 "data": _rate_calldata(tok.rate_call, tok.share_decimals)}, "latest"]))

        results = await self._rpc(scope, calls)
        tip = _hex(results[0])
        native = _hex(results[1])
        token_balances = {}
        if contracts:
            for entry in (results[2] or {}).get("tokenBalances", []):
                token_balances[entry["contractAddress"].lower()] = _hex(entry.get("tokenBalance"))
        rates = {tok.contract: _hex(raw) for tok, raw
                 in zip(rate_tokens, results[3 if contracts else 2:])}

        parts = [str(native)] + [f"{k}={v}" for k, v in sorted(token_balances.items())]
        # The rate drifts every block. At full precision it would make every tick
        # "changed" and turn the cheap probe into a transfer fetch; truncated to
        # four decimals it moves a few times a day, which is often enough to keep
        # the reported value honest.
        for tok in sorted(rate_tokens, key=lambda x: x.contract):
            coarse = 10 ** max(tok.decimals - RATE_MARKER_DP, 0)
            parts.append(f"{tok.contract}@{rates[tok.contract] // coarse}")
        marker = ":".join(parts)
        return Probe(changed=marker != cursor.last_marker, marker=marker,
                     raw={"tip": tip, "native": native, "tokens": token_balances,
                          "rates": rates})

    def _balances(self, scope: str, probe: Probe) -> list[BalanceSnapshot]:
        symbol = NETWORKS[scope][1]
        out = [BalanceSnapshot(asset_key=f"{scope}:native", amount_raw=probe.raw["native"],
                               decimals=NATIVE_DECIMALS, symbol=symbol,
                               block_height=probe.raw["tip"], fee_bearing=True)]
        rates = probe.raw.get("rates") or {}
        for tok in self._whitelist(scope):
            if tok.contract not in probe.raw["tokens"]:
                continue
            amount = probe.raw["tokens"][tok.contract]
            rate = rates.get(tok.contract)
            if rate:
                # Report what the shares redeem for, not the share count: the
                # yield lives entirely in the rate, so a raw balance would look
                # frozen while the position quietly grows.
                amount = amount * rate // 10 ** tok.share_decimals
            if tok.debt:
                # A debt token's balance is what is owed. Negating it here, at
                # the one place a balance is built, is what makes every total
                # downstream subtract it - the digest, the portfolio and the
                # residual arithmetic all take the sign for granted.
                amount = -amount
            out.append(BalanceSnapshot(
                asset_key=f"{scope}:{tok.contract}",
                amount_raw=amount,
                decimals=tok.decimals, symbol=tok.symbol,
                block_height=probe.raw["tip"],
                debt=tok.debt,
                yield_bearing=bool(rate) or tok.yield_bearing))
        return out

    async def fetch(self, t: Target, scope: str, cursor: Cursor,
                    probe: Probe) -> AddressState:
        tip = probe.raw["tip"]
        state = AddressState(balances=self._balances(scope, probe),
                             cursor=Cursor(last_block=tip, last_item=cursor.last_item,
                                           last_marker=probe.marker))
        if cursor.is_fresh:
            # Adopt the current balances and start watching from this block.
            return state

        start = (cursor.last_block or tip) + 1
        if start > tip:
            return state

        transfers, truncated = await self._transfers(t, scope, start, tip)
        state.transfers = transfers
        state.truncated = truncated
        return state

    async def _transfers(self, t: Target, scope: str, start: int, tip: int):
        categories = ["external", "erc20"]
        if NETWORKS[scope][2]:
            categories.insert(1, "internal")
        base = {"fromBlock": hex(start), "toBlock": hex(tip), "category": categories,
                "withMetadata": True, "excludeZeroValue": True,
                "maxCount": "0x3e8", "order": "asc"}

        allowed = {tok.contract: tok for tok in self._whitelist(scope)}
        out: list[Transfer] = []
        truncated = False

        for field, direction in (("toAddress", Direction.IN), ("fromAddress", Direction.OUT)):
            page = None
            for _ in range(MAX_PAGES):
                params = dict(base, **{field: t.address})
                if page:
                    params["pageKey"] = page
                (result,) = await self._rpc(scope, [("alchemy_getAssetTransfers", [params])])
                for raw in (result or {}).get("transfers", []):
                    made = self._to_transfer(raw, scope, direction, allowed, t.address)
                    if made is not None:
                        out.append(made)
                page = (result or {}).get("pageKey")
                if not page:
                    break
            else:
                truncated = True
                log.warning("%s/%s: more than %d pages of %s transfers",
                            t.label, scope, MAX_PAGES, field)

        if "beacon" in t.watch and scope == "ethereum":
            if self._withdrawals is None:
                if "beacon" not in self._warned:
                    self._warned.add("beacon")
                    log.warning("%s: beacon withdrawals need ETHERSCAN_API_KEY; "
                                "staking income will look like an anomaly", t.label)
            else:
                out += await self._withdrawals.since(t.address, start, tip)

        out.sort(key=lambda x: (x.block_height or 0, x.uid))
        return out, truncated

    @staticmethod
    def _to_transfer(raw: dict, scope: str, direction: Direction,
                     allowed: dict, address: str) -> Transfer | None:
        contract = (raw.get("rawContract") or {}).get("address")
        if raw.get("category") == "erc20":
            if contract is None:
                return None
            contract = contract.lower()
            # Invariant: only whitelisted tokens are ever tracked. This is the
            # line that keeps airdropped scam tokens out of the notifications.
            token = allowed.get(contract)
            if token is None:
                return None
            asset_key, symbol, decimals = f"{scope}:{contract}", token.symbol, token.decimals
            debt = token.debt
        else:
            asset_key = f"{scope}:native"
            symbol, decimals = NETWORKS[scope][1], NATIVE_DECIMALS
            debt = False

        # rawContract.value is the exact integer; the sibling "value" field is a
        # float and silently loses precision on large amounts.
        amount = _hex((raw.get("rawContract") or {}).get("value"))
        if amount == 0:
            return None

        counterparty = raw.get("from") if direction is Direction.IN else raw.get("to")
        if counterparty and counterparty.lower() == address.lower():
            return None                       # self-transfer: no net movement

        ts_text = (raw.get("metadata") or {}).get("blockTimestamp")
        from datetime import datetime, timezone
        ts = (datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
              if ts_text else datetime.now(timezone.utc))

        # Debt tokens are minted to the borrower and burned on repayment, and
        # Alchemy reports both as ordinary transfers from and to the zero
        # address. Receiving one is not income: it moves the recorded balance
        # down, because the recorded balance is negative. Without this the
        # transfer and the balance disagree by twice the amount, and the
        # reconciliation that is supposed to explain a borrow invents an
        # anomaly instead.
        effect = None
        if debt:
            effect = -amount if direction is Direction.IN else amount

        return Transfer(
            tx_hash=raw["hash"],
            uid=raw["uniqueId"],              # stable per movement, not per tx
            asset_key=asset_key,
            amount_raw=amount,
            direction=direction,
            counterparty=counterparty.lower() if counterparty else None,
            block_height=_hex(raw.get("blockNum")),
            ts=ts,
            symbol=symbol,
            decimals=decimals,
            balance_effect=effect,
        )
