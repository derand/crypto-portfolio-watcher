import httpx
import pytest

from portfolio.chains.base import Cursor, Target
from portfolio.chains.bitcoin import BitcoinAdapter, format_btc
from portfolio.models import Direction

ME = "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97"
THEM = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def stats(funded=0, spent=0, count=0):
    return {"funded_txo_sum": funded, "spent_txo_sum": spent, "tx_count": count,
            "funded_txo_count": 0, "spent_txo_count": 0}


def addr_doc(confirmed=(0, 0, 0), mem=(0, 0, 0)):
    return {"address": ME, "chain_stats": stats(*confirmed), "mempool_stats": stats(*mem)}


def tx(txid, vin, vout, height=800000, block_time=1700000000):
    status = ({"confirmed": True, "block_height": height, "block_time": block_time}
              if height else {"confirmed": False})
    return {"txid": txid, "status": status,
            "vin": [{"prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout]}


TARGET = Target(address=ME, label="btc", watch=frozenset({"native"}))


def adapter(routes):
    """routes: path -> payload, or a zero-arg callable for changing responses.

    A plain list is a payload (a page of transactions), never a response queue -
    conflating the two is how the first version of this helper lied to us.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        payload = routes[path]
        if callable(payload):
            payload = payload()
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t")
    a = BitcoinAdapter(base_url="", client=client, min_interval=0)
    return a, calls


async def test_probe_costs_one_call_and_carries_the_balance():
    a, calls = adapter({f"/address/{ME}": addr_doc(confirmed=(500, 200, 3))})
    p = await a.probe(TARGET, "", Cursor())
    assert p.changed is True
    assert len(calls) == 1, "probe must be a single request"
    assert p.raw["chain_stats"]["funded_txo_sum"] == 500


async def test_probe_reports_unchanged_when_marker_matches():
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(500, 200, 3))})
    first = await a.probe(TARGET, "", Cursor())
    a2, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(500, 200, 3))})
    second = await a2.probe(TARGET, "", Cursor(last_marker=first.marker))
    assert second.changed is False


async def test_mempool_arrival_changes_the_marker():
    """A pending deposit must wake the tick even though confirmed stats are equal."""
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(500, 200, 3))})
    quiet = await a.probe(TARGET, "", Cursor())
    a2, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(500, 200, 3), mem=(100, 0, 1))})
    busy = await a2.probe(TARGET, "", Cursor(last_marker=quiet.marker))
    assert busy.changed is True


async def test_spend_with_change_is_netted_not_gross():
    """The bug this guards: an outgoing spend returns most of the input as change
    back to the same address. Counting outputs alone reports a huge fake deposit."""
    spend = tx("t1", vin=[(ME, 1_000_000)], vout=[(THEM, 300_000), (ME, 699_000)])
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(699_000, 0, 2)),
                    f"/address/{ME}/txs": [spend]})
    state = await a.fetch(TARGET, "", Cursor(last_item="older", last_marker="x"),
                          await a.probe(TARGET, "", Cursor()))
    assert len(state.transfers) == 1
    t = state.transfers[0]
    assert t.direction is Direction.OUT
    assert t.amount_raw == 301_000, "300k sent + 1k fee"
    assert t.counterparty == THEM


async def test_incoming_transfer_and_counterparty():
    deposit = tx("t2", vin=[(THEM, 500_000)], vout=[(ME, 499_000)])
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(499_000, 0, 1)),
                    f"/address/{ME}/txs": [deposit]})
    state = await a.fetch(TARGET, "", Cursor(last_item="older", last_marker="x"),
                          await a.probe(TARGET, "", Cursor()))
    t = state.transfers[0]
    assert (t.direction, t.amount_raw, t.counterparty) == (Direction.IN, 499_000, THEM)


async def test_unconfirmed_transfer_is_marked_pending():
    pending = tx("t3", vin=[(THEM, 10_000)], vout=[(ME, 9_000)], height=None)
    a, _ = adapter({f"/address/{ME}": addr_doc(mem=(9_000, 0, 1)),
                    f"/address/{ME}/txs": [pending]})
    state = await a.fetch(TARGET, "", Cursor(last_item="older", last_marker="x"),
                          await a.probe(TARGET, "", Cursor()))
    assert state.transfers[0].block_height is None


async def test_first_sight_takes_a_baseline_instead_of_replaying_history():
    """Years of history must not arrive as notifications on day one."""
    old = [tx(f"h{i}", vin=[(THEM, 1000)], vout=[(ME, 900)]) for i in range(50)]
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(45_000, 0, 50)),
                    f"/address/{ME}/txs": old})
    state = await a.fetch(TARGET, "", Cursor(), await a.probe(TARGET, "", Cursor()))
    assert state.transfers == []
    assert state.cursor.last_item == "h0", "cursor parked at the newest tx"
    assert state.balances[0].amount_raw == 45_000


async def test_walk_stops_at_the_cursor():
    seen = [tx("new1", vin=[(THEM, 1000)], vout=[(ME, 900)]),
            tx("new2", vin=[(THEM, 2000)], vout=[(ME, 1900)]),
            tx("known", vin=[(THEM, 3000)], vout=[(ME, 2900)])]
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(5700, 0, 3)),
                    f"/address/{ME}/txs": seen})
    state = await a.fetch(TARGET, "", Cursor(last_item="known", last_marker="x"),
                          await a.probe(TARGET, "", Cursor()))
    assert [t.tx_hash for t in state.transfers] == ["new1", "new2"]
    assert state.cursor.last_item == "new1"


async def test_self_transfer_producing_no_net_change_is_skipped():
    """Consolidating your own UTXOs moves nothing but the fee; not a transfer."""
    consolidate = tx("t4", vin=[(ME, 500), (ME, 500)], vout=[(ME, 1000)])
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(1000, 0, 1)),
                    f"/address/{ME}/txs": [consolidate]})
    state = await a.fetch(TARGET, "", Cursor(last_item="older", last_marker="x"),
                          await a.probe(TARGET, "", Cursor()))
    assert state.transfers == []


@pytest.mark.parametrize("sats,text", [
    (0, "0"), (1, "0.00000001"), (100_000_000, "1"),
    (12_345_000, "0.12345"), (5_743_251_519, "57.43251519"),
])
def test_format_btc(sats, text):
    assert format_btc(sats) == text


async def test_the_cursor_never_parks_on_an_unconfirmed_transaction():
    """The walk stops at last_item. Parking it on a pending transaction means
    that when the transaction confirms it is the stopping point and is never
    read again - so the event keeps saying "(in mempool)" for good and the
    pending → confirmed requeue is dead for Bitcoin. The cursor stays on the
    newest confirmed transaction instead; re-reading the ones above it costs
    nothing, because events are deduplicated by uid."""
    pending = tx("new", vin=[(THEM, 500_000)], vout=[(ME, 500_000)], height=0)
    settled = tx("old", vin=[(THEM, 100_000)], vout=[(ME, 100_000)], height=799_999)
    a, _ = adapter({f"/address/{ME}": addr_doc(confirmed=(100_000, 0, 1), mem=(500_000, 0, 1)),
                    f"/address/{ME}/txs": [pending, settled]})
    state = await a.fetch(TARGET, "", Cursor(last_item="older", last_marker="x"),
                          await a.probe(TARGET, "", Cursor()))
    assert state.cursor.last_item == "old", "the pending one must not become the wall"


async def test_a_window_of_only_pending_transactions_leaves_the_cursor_alone():
    """Nothing confirmed means nothing to park on. Advancing anyway would skip
    the whole window the moment it confirms."""
    pending = tx("new", vin=[(THEM, 500_000)], vout=[(ME, 500_000)], height=0)
    a, _ = adapter({f"/address/{ME}": addr_doc(mem=(500_000, 0, 1)),
                    f"/address/{ME}/txs": [pending]})
    state = await a.fetch(TARGET, "", Cursor(last_item="older", last_marker="x"),
                          await a.probe(TARGET, "", Cursor()))
    assert state.cursor.last_item == "older"
    assert len(state.transfers) == 1, "the pending transfer is still reported"
