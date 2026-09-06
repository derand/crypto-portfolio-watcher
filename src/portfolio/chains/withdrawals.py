"""Beacon-chain withdrawals for an execution address.

EIP-4895 puts withdrawals in the block body rather than in a transaction, so no
transfer API reports them: Alchemy's getAssetTransfers returns nothing for an
address whose only income is staking rewards. Left unexplained, each arrival is
a balance that grew with no transfer behind it - money from nowhere, which is
exactly what the anomaly rule is meant to shout about.

The fix is to report them as what they are. Reconciliation then balances on its
own, the reward shows up as a real event, and nothing has to be silenced.

Etherscan rather than a beacon API on purpose: withdrawals land on the
execution layer, the free tier covers Ethereum, and the key is already needed
elsewhere. Validator balances are a different question and live in
protocols/beacon.py.
"""

import logging
from datetime import datetime, timezone

import httpx

from ..models import Direction, Transfer
from ..retry import Permanent, Unavailable, redact, with_retry

log = logging.getLogger(__name__)

ETHERSCAN = "https://api.etherscan.io/v2/api"
GWEI = 10 ** 9
NATIVE_DECIMALS = 18
PAGE = 1000
MAX_PAGES = 5


class BeaconWithdrawals:
    """Withdrawals paid to an execution address, newest history first."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None,
                 url: str = ETHERSCAN, chain_id: int = 1):
        self._key = api_key
        self._client = client
        self._own = client is None
        self._url = url
        self._chain_id = chain_id

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def since(self, address: str, start_block: int, tip: int) -> list[Transfer]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

        out: list[Transfer] = []
        for page in range(1, MAX_PAGES + 1):
            rows = await self._page(address, start_block, tip, page)
            for row in rows:
                made = self._to_transfer(row, address)
                if made is not None:
                    out.append(made)
            if len(rows) < PAGE:
                break
        else:
            log.warning("%s: more than %d pages of withdrawals; older ones skipped",
                        address, MAX_PAGES)
        out.sort(key=lambda x: (x.block_height or 0, x.uid))
        return out

    async def _page(self, address: str, start_block: int, tip: int, page: int) -> list:
        params = {"chainid": self._chain_id, "module": "account",
                  "action": "txsBeaconWithdrawal", "address": address,
                  "startblock": start_block, "endblock": tip,
                  "page": page, "offset": PAGE, "sort": "asc"}
        if self._key:
            params["apikey"] = self._key

        async def call():
            r = await self._client.get(self._url, params=params)
            if r.status_code >= 400:
                # Not raise_for_status(): Etherscan takes its key as `apikey=`
                # in the query string, and httpx puts the whole URL in the
                # message - which ends up in a Telegram failure line.
                raise RuntimeError(f"etherscan: HTTP {r.status_code}")
            return r.json()

        try:
            body = await with_retry(call, what="etherscan withdrawals")
        except Unavailable as e:
            raise Unavailable(redact(str(e), self._key)) from None
        result = body.get("result")
        if isinstance(result, list):
            return result
        # "No transactions found" is a normal empty answer, not a failure.
        message = str(body.get("message", ""))
        if "No transactions found" in message or "No records found" in message:
            return []
        raise Permanent(f"etherscan withdrawals: {message} {str(result)[:120]}")

    @staticmethod
    def _to_transfer(row: dict, address: str) -> Transfer | None:
        amount = int(row["amount"]) * GWEI          # the API answers in gwei
        if amount == 0:
            return None
        index = row["withdrawalIndex"]
        return Transfer(
            # There is no transaction to point at, so the withdrawal index is
            # the identity. It is unique per withdrawal and keeps the database's
            # dedup key working unchanged.
            tx_hash=f"beacon:{index}",
            uid=f"beacon:{index}",
            asset_key="ethereum:native",
            amount_raw=amount,
            direction=Direction.IN,
            counterparty=f"validator:{row['validatorIndex']}",
            block_height=int(row["blockNumber"]),
            ts=datetime.fromtimestamp(int(row["timestamp"]), timezone.utc),
            symbol="ETH",
            decimals=NATIVE_DECIMALS,
        )
