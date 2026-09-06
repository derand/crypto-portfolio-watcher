import httpx
import pytest

from portfolio.chains.withdrawals import BeaconWithdrawals
from portfolio.models import Direction
from portfolio.retry import Permanent

ME = "0xbeac017a3c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f"


def row(index, amount_gwei, block, validator=999999, ts=1787000000):
    return {"withdrawalIndex": str(index), "validatorIndex": str(validator),
            "address": ME, "amount": str(amount_gwei),
            "blockNumber": str(block), "timestamp": str(ts)}


def source(pages):
    """pages: list of response bodies, answered in order."""
    queue = list(pages)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=queue.pop(0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BeaconWithdrawals("key", client=client, url="http://etherscan/api"), seen


async def test_amounts_arrive_in_gwei_not_wei():
    """The API answers in gwei. Taking it for wei would report a reward of
    0.0144 ETH as 14 attowei and quietly lose every staking payment."""
    s, _ = source([{"status": "1", "result": [row(1, 14423633, 25865614)]}])
    got = await s.since(ME, 0, 99)
    assert got[0].amount_raw == 14423633 * 10**9      # 0.014423633 ETH
    assert (got[0].symbol, got[0].decimals) == ("ETH", 18)
    assert got[0].direction is Direction.IN
    assert got[0].asset_key == "ethereum:native"


async def test_withdrawal_index_is_the_dedup_key():
    """There is no transaction to point at, so the index has to carry identity:
    the database's UNIQUE(chain, tx_hash, log_index, address_id) rests on it."""
    s, _ = source([{"status": "1", "result": [row(141290454, 1, 10),
                                              row(141290455, 2, 11)]}])
    got = await s.since(ME, 0, 99)
    assert [t.uid for t in got] == ["beacon:141290454", "beacon:141290455"]
    assert [t.tx_hash for t in got] == ["beacon:141290454", "beacon:141290455"]
    assert got[0].counterparty == "validator:999999"


async def test_no_withdrawals_is_an_empty_answer_not_a_failure():
    """Etherscan says "No transactions found" with status 0. Treating that as
    an error would mark the provider down on every quiet address."""
    s, _ = source([{"status": "0", "message": "No transactions found", "result": []}])
    assert await s.since(ME, 0, 99) == []


async def test_a_real_error_is_not_swallowed():
    s, _ = source([{"status": "0", "message": "NOTOK",
                    "result": "Invalid API Key"}])
    with pytest.raises(Permanent, match="NOTOK"):
        await s.since(ME, 0, 99)


async def test_pagination_stops_on_a_short_page():
    full = [row(i, 100, 1000 + i) for i in range(1000)]
    s, seen = source([{"status": "1", "result": full},
                      {"status": "1", "result": [row(9999, 100, 3000)]}])
    got = await s.since(ME, 0, 99)
    assert len(got) == 1001
    assert [p["page"] for p in seen] == ["1", "2"]


async def test_the_block_window_is_passed_through():
    """Asking from the cursor rather than from genesis is what keeps a tick
    cheap once the address has years of history."""
    s, seen = source([{"status": "1", "result": []}])
    await s.since(ME, 25_000_000, 25_900_000)
    assert seen[0]["startblock"] == "25000000"
    assert seen[0]["endblock"] == "25900000"
