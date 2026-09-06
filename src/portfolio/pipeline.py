"""One tick: probe every address, turn what changed into events, notify once.

Ordering matters here. Events are written and their notifications queued inside
one transaction; only then does anything leave the process. If the machine dies
mid-tick, the queued rows are still pending and the next tick delivers them -
which is why notifications live in their own table.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .chains.base import Cursor, Target
from .models import (Direction, EventKind, Message, Severity, format_display,
                     format_units)
from .prices.sources import NATIVE_IDS
from .retry import Unavailable

log = logging.getLogger(__name__)

ARROW = {Direction.IN: "←", Direction.OUT: "→", Direction.INTERNAL: "↔"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ScanResult:
    probed: int = 0
    changed: int = 0
    new_events: int = 0
    confirmed: int = 0
    below_threshold: int = 0
    failed: list[str] = field(default_factory=list)
    delivered: dict[str, str | None] = field(default_factory=dict)


def _asset_id(conn, asset_key: str, symbol: str, decimals: int,
              contract_hint: str = "") -> int:
    """Assets are registered from what the adapter reports, never guessed here."""
    row = conn.execute("SELECT id, contract FROM assets WHERE asset_key=?",
                       (asset_key,)).fetchone()
    if row:
        # Rows created before the venue reported an id of its own: fill it in,
        # or a Hyperliquid spot holding stays unpriceable forever.
        if contract_hint and not row["contract"]:
            conn.execute("UPDATE assets SET contract=? WHERE id=?",
                         (contract_hint, row["id"]))
        return row["id"]
    scope, _, tail = asset_key.partition(":")
    contract = None if tail == "native" else tail
    kind = "native" if contract is None else "erc20"
    if scope == "hyperliquid":
        contract, kind = contract_hint or None, "perp"
    cur = conn.execute(
        """INSERT INTO assets(asset_key, chain, contract, symbol, decimals,
                              coingecko_id, kind, whitelisted)
           VALUES (?,?,?,?,?,?,?,1)""",
        (asset_key, scope, contract, symbol or scope.upper(), decimals,
         NATIVE_IDS.get(scope) if kind == "native" else None, kind))
    return cur.lastrowid


def priceable_assets(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT asset_key, chain, contract, symbol, coingecko_id FROM assets")]


def _load_cursor(conn, chain: str, address_id: int, scope: str = "") -> Cursor:
    row = conn.execute(
        "SELECT last_block, last_signature, last_marker FROM cursors "
        "WHERE chain=? AND address_id=? AND scope=?", (chain, address_id, scope)).fetchone()
    if not row:
        return Cursor()
    return Cursor(last_block=row["last_block"], last_item=row["last_signature"],
                  last_marker=row["last_marker"])


def _save_cursor(conn, chain: str, address_id: int, cur: Cursor,
                 scope: str = "", ok: bool = True) -> None:
    conn.execute(
        """INSERT INTO cursors(chain, address_id, scope, last_block, last_signature,
                               last_marker, last_ok_at, fail_count)
           VALUES (?,?,?,?,?,?,?,0)
           ON CONFLICT(chain, address_id, scope) DO UPDATE SET
             last_block=excluded.last_block,
             last_signature=excluded.last_signature,
             last_marker=excluded.last_marker,
             last_ok_at=excluded.last_ok_at,
             fail_count=0""",
        (chain, address_id, scope, cur.last_block, cur.last_item, cur.last_marker,
         _now() if ok else None))


def _mark_failure(conn, chain: str, address_id: int, scope: str = "") -> int:
    conn.execute(
        """INSERT INTO cursors(chain, address_id, scope, fail_count) VALUES (?,?,?,1)
           ON CONFLICT(chain, address_id, scope) DO UPDATE SET
             fail_count = fail_count + 1""", (chain, address_id, scope))
    return conn.execute(
        "SELECT fail_count FROM cursors WHERE chain=? AND address_id=? AND scope=?",
        (chain, address_id, scope)).fetchone()["fail_count"]


def _record_transfer(conn, chain: str, scope: str, address_id: int,
                     asset_id: int, t, own: set[str], usd: float | None = None) -> str:
    """Insert or update one transfer. Returns 'new' | 'confirmed' | 'unchanged'.

    A transfer whose counterparty is one of our own wallets is marked internal,
    so moving funds between our addresses does not read as a deposit plus a
    withdrawal.
    """
    direction = Direction.INTERNAL if (t.counterparty in own) else t.direction
    status = "confirmed" if t.block_height else "pending"
    row = conn.execute(
        """SELECT id, status FROM events
           WHERE chain=? AND address_id=? AND kind=? AND uid=?""",
        (chain, address_id, EventKind.TRANSFER.value, t.uid)).fetchone()

    if row is None:
        conn.execute(
            """INSERT INTO events(chain, scope, address_id, asset_id, tx_hash, uid, kind,
                                  direction, amount_raw, usd, counterparty, block_height,
                                  ts, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chain, scope, address_id, asset_id, t.tx_hash, t.uid, EventKind.TRANSFER.value,
             direction.value, str(t.amount_raw), usd, t.counterparty, t.block_height,
             t.ts.isoformat(), status))
        return "new"

    if row["status"] == "pending" and status == "confirmed":
        conn.execute("UPDATE events SET status='confirmed', block_height=?, ts=? WHERE id=?",
                     (t.block_height, t.ts.isoformat(), row["id"]))
        return "confirmed"
    return "unchanged"


def _event_id(conn, chain: str, address_id: int, t) -> int:
    return conn.execute(
        """SELECT id FROM events WHERE chain=? AND address_id=? AND kind=? AND uid=?""",
        (chain, address_id, EventKind.TRANSFER.value, t.uid)).fetchone()["id"]


def _queue(conn, event_id: int, channels: list[str], requeue: bool = False) -> None:
    """Owe a message for this event on every channel.

    requeue=True is the pending -> confirmed follow-up: the row already exists
    and was already sent, so it must be put back into the queue. sent_at is kept
    deliberately - it is what tells the renderer this is a follow-up rather than
    a first sighting.
    """
    for ch in channels:
        if requeue:
            conn.execute(
                """INSERT INTO notifications(event_id, channel, status) VALUES (?,?, 'pending')
                   ON CONFLICT(event_id, channel) DO UPDATE SET status='pending'""",
                (event_id, ch))
        else:
            conn.execute(
                """INSERT INTO notifications(event_id, channel, status) VALUES (?,?, 'pending')
                   ON CONFLICT(event_id, channel) DO NOTHING""", (event_id, ch))


def _worth_saying(cfg, usd: float | None) -> bool:
    """Below the USD threshold the event is still recorded, just not announced.

    An unpriced asset always speaks up: silence must never be the consequence of
    not knowing what something is worth.
    """
    if usd is None:
        return True
    return abs(usd) >= cfg.thresholds.notify_usd


def _upsert_balance(conn, address_id: int, asset_id: int, snap,
                    usd: float | None = None) -> int | None:
    """Store the new balance, return the previous one (None if first sight)."""
    row = conn.execute("SELECT amount_raw FROM balances WHERE address_id=? AND asset_id=?",
                       (address_id, asset_id)).fetchone()
    previous = int(row["amount_raw"]) if row else None
    conn.execute(
        """INSERT INTO balances(address_id, asset_id, amount_raw, block_height, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(address_id, asset_id) DO UPDATE SET
             amount_raw=excluded.amount_raw, block_height=excluded.block_height,
             updated_at=excluded.updated_at""",
        (address_id, asset_id, str(snap.amount_raw), snap.block_height, _now()))
    conn.execute(
        """INSERT INTO balance_snapshots(address_id, asset_id, amount_raw, usd, ts)
           VALUES (?,?,?,?,?)""",
        (address_id, asset_id, str(snap.amount_raw), usd, _now()))
    return previous


def _record_accrual(conn, chain: str, scope: str, address_id: int, asset_id: int,
                    amount: int, detail: str, usd: float | None = None) -> None:
    """Yield or fees: recorded for the digest, never alerted.

    `usd` is what *accrued*, not what the holding is worth. The digest sums a
    day of these per asset and prints dollars beside the quantity; left unset,
    that column is empty forever and a day of yield is a number nobody can
    size. Negative for fees, which is the sign that keeps them out of the
    digest's earned total.
    """
    conn.execute(
        """INSERT INTO events(chain, scope, address_id, asset_id, tx_hash, uid, kind,
                              amount_raw, usd, ts, status, detail)
           VALUES (?,?,?,?,'',?,?,?,?,?, 'confirmed', ?)""",
        (chain, scope, address_id, asset_id, f"accrual:{_now()}",
         EventKind.ACCRUAL.value, str(amount), usd, _now(), detail))


def _record_debt_change(conn, chain: str, scope: str, address_id: int,
                        asset_id: int, residual: int, symbol: str,
                        decimals: int, usd: float | None) -> int:
    """Somebody borrowed or repaid. Recorded as a position change, and alerted.

    Not an anomaly: nothing here is unexplained. The balance of a debt token
    moves for exactly two reasons, and this is the one that was a decision -
    interest is handled above, silently, by being small.
    """
    verb = "borrowed" if residual < 0 else "repaid"
    detail = f"{verb} {format_display(abs(residual), decimals)} {symbol}".strip()
    cur = conn.execute(
        """INSERT INTO events(chain, scope, address_id, asset_id, tx_hash, uid, kind,
                              amount_raw, usd, ts, status, detail)
           VALUES (?,?,?,?,'',?,?,?,?,?, 'confirmed', ?)""",
        (chain, scope, address_id, asset_id, f"debt:{_now()}",
         EventKind.POSITION_CHANGE.value, str(residual), usd, _now(), detail))
    return cur.lastrowid


def _record_anomaly(conn, chain: str, scope: str, address_id: int, asset_id: int,
                    delta: int, explained: int) -> int:
    """Balance moved by more than the transfers we found explain.

    On Bitcoin this should never fire: fees are already inside the net amount.
    If it does, the adapter is wrong or history was truncated - and silently
    showing a balance nobody can account for is worse than a noisy alert.
    """
    residual = delta - explained
    cur = conn.execute(
        """INSERT INTO events(chain, scope, address_id, asset_id, tx_hash, uid, kind,
                              amount_raw, ts, status, detail)
           VALUES (?,?,?,?,'',?,?,?,?, 'confirmed', ?)""",
        (chain, scope, address_id, asset_id, f"reconcile:{_now()}",
         EventKind.ANOMALY.value, str(residual), _now(),
         f"balance moved {delta:+d} but transfers explain {explained:+d}"))
    return cur.lastrowid


async def scan_once(cfg, conn, router, adapters, sources=None, prices=None) -> ScanResult:
    res = ScanResult()
    if prices is not None:
        try:
            await prices.refresh(priceable_assets(conn))
        except Exception as e:                       # noqa: BLE001
            # Prices are advisory. Losing them must never stop the watching.
            log.warning("price refresh failed, continuing unpriced: %s", e)
    channels = router.channels
    own = cfg.own_addresses

    rows = conn.execute(
        "SELECT id, chain, address, label, watch, chains FROM addresses "
        "WHERE enabled=1").fetchall()

    for row in rows:
        adapter = adapters.get(row["chain"])
        if adapter is None:
            continue                                  # chain not implemented yet
        target = Target(address=row["address"], label=row["label"],
                        watch=frozenset(json.loads(row["watch"])),
                        chains=tuple(json.loads(row["chains"])))
        for scope in adapter.scopes(target):
            await _scan_scope(cfg, conn, res, adapter, target, row["id"], scope,
                              channels, own, prices)

    for row in rows:
        target = Target(address=row["address"], label=row["label"],
                        watch=frozenset(json.loads(row["watch"])),
                        chains=tuple(json.loads(row["chains"])))
        for name in sorted(target.watch):
            source = (sources or {}).get(name)
            if source is not None:
                await _scan_positions(cfg, conn, res, source, target, row["id"],
                                      channels, prices)

    res.delivered = await _deliver(conn, router)
    return res


async def _scan_scope(cfg, conn, res, adapter, target, address_id, scope,
                      channels, own, prices=None) -> None:
    """One address in one network. Everything below is per-scope state."""
    chain = adapter.chain
    where = f"{target.label}/{scope}" if scope else target.label
    res.probed += 1
    cursor = _load_cursor(conn, chain, address_id, scope)

    try:
        probe = await adapter.probe(target, scope, cursor)
        if not probe.changed:
            conn.execute("BEGIN")
            _save_cursor(conn, chain, address_id, cursor, scope)
            conn.execute("COMMIT")
            return
        res.changed += 1
        state = await adapter.fetch(target, scope, cursor, probe)
    except Unavailable as e:
        conn.execute("BEGIN")
        fails = _mark_failure(conn, chain, address_id, scope)
        conn.execute("COMMIT")
        log.error("%s: %s (consecutive failures: %d)", where, e, fails)
        res.failed.append(f"{where}: {e}")
        return
    except Exception as e:                           # noqa: BLE001
        # Anything else a provider can raise: a Permanent (Alchemy answers 403
        # for a network merely disabled in the dashboard) or a shape change
        # that comes out as KeyError. It is this scope's problem, not the
        # tick's - letting it out skips every address after this one *and*
        # _deliver(), so one bad network holds back the alerts for all of them,
        # on every tick, for as long as the provider stays broken.
        conn.execute("BEGIN")
        fails = _mark_failure(conn, chain, address_id, scope)
        conn.execute("COMMIT")
        log.exception("%s: unexpected failure (consecutive: %d)", where, fails)
        res.failed.append(f"{where}: {e}")
        return

    conn.execute("BEGIN")
    try:
        baseline = cursor.is_fresh
        explained: dict[str, int] = {}
        for t in state.transfers:
            aid = _asset_id(conn, t.asset_key, t.symbol, t.decimals)
            usd = prices.value(t.asset_key, t.amount_raw, t.decimals) if prices else None
            outcome = _record_transfer(conn, chain, scope, address_id, aid, t, own, usd)
            if outcome == "new":
                # Only first sightings move the balance. A transfer confirming
                # was already counted while pending (the balance includes the
                # mempool), and a re-reported one was counted a tick ago.
                explained[t.asset_key] = explained.get(t.asset_key, 0) + t.effect
                res.new_events += 1
                if _worth_saying(cfg, usd):
                    _queue(conn, _event_id(conn, chain, address_id, t), channels)
                else:
                    res.below_threshold += 1
            elif outcome == "confirmed":
                res.confirmed += 1
                _queue(conn, _event_id(conn, chain, address_id, t), channels,
                       requeue=True)

        for snap in state.balances:
            aid = _asset_id(conn, snap.asset_key, snap.symbol, snap.decimals)
            usd = prices.value(snap.asset_key, snap.amount_raw, snap.decimals) if prices else None
            previous = _upsert_balance(conn, address_id, aid, snap, usd)
            if previous is None or baseline or state.truncated:
                continue
            delta = snap.amount_raw - previous
            accounted = explained.get(snap.asset_key, 0)
            residual = delta - accounted
            if residual == 0:
                continue
            # `usd` above prices the whole holding; an accrual is worth only the
            # part that moved. Same cached price, no extra round trip.
            accrued = (prices.value(snap.asset_key, residual, snap.decimals)
                       if prices else None)
            if snap.fee_bearing and residual < 0:
                # Gas: an approve, a failed swap, any contract call spends the
                # native coin without producing a transfer anyone wants alerted.
                _record_accrual(conn, chain, scope, address_id, aid, residual,
                                "network fees", accrued)
                continue
            if snap.debt:
                # Interest and borrowing move the same number, so a flag cannot
                # separate them - only size can. Under the threshold it is
                # interest, which must never ring; over it somebody borrowed or
                # repaid, which must. A debt token is required to carry a
                # coingecko_id precisely so this comparison always has an answer.
                if _worth_saying(cfg, accrued):
                    eid = _record_debt_change(conn, chain, scope, address_id, aid,
                                              residual, snap.symbol, snap.decimals,
                                              accrued)
                    _queue(conn, eid, channels)
                else:
                    _record_accrual(conn, chain, scope, address_id, aid, residual,
                                    "borrow interest", accrued)
                continue
            if snap.yield_bearing:
                # The balance is shares x current rate while transfers are in
                # shares: the two units never reconcile exactly, so treating
                # their difference as a missing transfer would alert forever.
                # A real withdrawal still alerts - as the ERC-20 transfer above.
                _record_accrual(conn, chain, scope, address_id, aid, residual,
                                "vault yield", accrued)
                continue
            eid = _record_anomaly(conn, chain, scope, address_id, aid, delta, accounted)
            _queue(conn, eid, channels)
            log.warning("%s: %s moved %+d, transfers explain %+d",
                        where, snap.symbol or snap.asset_key, delta, accounted)

        _save_cursor(conn, chain, address_id, state.cursor, scope)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if baseline:
        log.info("%s: baseline recorded, watching from now", where)


def _pending_batch(conn) -> list[dict]:
    """Everything still undelivered, including leftovers from earlier ticks."""
    rows = conn.execute(
        """SELECT e.id, e.kind, e.direction, e.amount_raw, e.counterparty,
                  e.status, e.detail, e.scope, e.uid, e.usd, a.label, a.chain,
                  s.symbol, s.decimals,
                  MAX(n.sent_at IS NOT NULL) AS resent
           FROM notifications n
           JOIN events e ON e.id = n.event_id
           JOIN addresses a ON a.id = e.address_id
           LEFT JOIN assets s ON s.id = e.asset_id
           WHERE n.status = 'pending'
           GROUP BY e.id
           ORDER BY e.id""").fetchall()
    return [dict(r) for r in rows]


def render(events: list[dict]) -> Message:
    lines, high = [], False
    for e in events:
        if e["kind"] == EventKind.ANOMALY.value:
            high = True
            lines.append(f"⚠ {e['label']}: {e['detail']}")
            continue
        if e["kind"] == EventKind.POSITION_CHANGE.value:
            danger = ":liq:" in (e["uid"] or "")
            high = high or danger
            lines.append(f"{'⚠ ' if danger else ''}{e['label']}: {e['detail']}")
            continue
        amount = format_units(int(e["amount_raw"]), e["decimals"] or 0)
        arrow = ARROW.get(Direction(e["direction"]), "·") if e["direction"] else "·"
        who = e["counterparty"] or "?"
        if e["resent"]:
            tail = "  ↳ confirmed"          # follow-up to a message already sent
        elif e["status"] != "confirmed":
            tail = "  (in mempool)"
        else:
            tail = ""
        where = f"{e['label']}/{e['scope']}" if e["scope"] else e["label"]
        worth = f" (${e['usd']:,.2f})" if e["usd"] else ""
        lines.append(f"{where}  {arrow} {amount} {e['symbol'] or ''}{worth}  "
                     f"{who[:12]}…{tail}".rstrip())
    title = "Portfolio: 1 change" if len(events) == 1 else f"Portfolio: {len(events)} changes"
    return Message(title=title, body="\n".join(lines),
                   severity=Severity.HIGH if high else Severity.NORMAL,
                   kind=EventKind.TRANSFER)


async def _deliver(conn, router) -> dict[str, str | None]:
    batch = _pending_batch(conn)
    if not batch or not router.channels:
        return {}
    results = await router.send(render(batch))
    ids = [e["id"] for e in batch]
    conn.execute("BEGIN")
    try:
        for channel, err in results.items():
            if err is None:
                conn.executemany(
                    "UPDATE notifications SET status='sent', sent_at=? "
                    "WHERE event_id=? AND channel=?",
                    [(_now(), i, channel) for i in ids])
            else:
                conn.executemany(
                    "UPDATE notifications SET error=? WHERE event_id=? AND channel=?",
                    [(err[:500], i, channel) for i in ids])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return results


def _fmt(amount_raw: int, decimals: int) -> str:
    return format_units(amount_raw, decimals)


def _upsert_position(conn, address_id: int, p, asset_id: int | None) -> None:
    conn.execute(
        """INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                 amount_raw, usd, extra, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(address_id, protocol, position_key) DO UPDATE SET
             asset_id=excluded.asset_id, amount_raw=excluded.amount_raw,
             usd=excluded.usd, extra=excluded.extra, updated_at=excluded.updated_at""",
        (address_id, p.protocol, p.key, asset_id, str(p.amount_raw), p.usd,
         json.dumps(p.extra), _now()))


def _position_event(conn, chain: str, address_id: int, uid: str, detail: str,
                    amount_raw: int, kind: str = EventKind.POSITION_CHANGE.value) -> int | None:
    """Insert one position event; None if it was already recorded.

    The uid carries the new state, so an unchanged position produces the same
    uid and is deduplicated by the database rather than by careful bookkeeping.
    """
    existing = conn.execute(
        "SELECT id FROM events WHERE chain=? AND address_id=? AND kind=? AND uid=?",
        (chain, address_id, kind, uid)).fetchone()
    if existing:
        return None
    cur = conn.execute(
        """INSERT INTO events(chain, scope, address_id, tx_hash, uid, kind,
                              amount_raw, ts, status, detail)
           VALUES (?,'',?,'',?,?,?,?, 'confirmed', ?)""",
        (chain, address_id, uid, kind, str(amount_raw), _now(), detail))
    return cur.lastrowid


async def _scan_positions(cfg, conn, res, source, target, address_id, channels,
                          prices=None) -> None:
    chain = source.name
    res.probed += 1
    cursor = _load_cursor(conn, chain, address_id)

    try:
        positions, marker = await source.fetch(target)
    except Unavailable as e:
        conn.execute("BEGIN")
        fails = _mark_failure(conn, chain, address_id)
        conn.execute("COMMIT")
        log.error("%s/%s: %s (consecutive failures: %d)", target.label, chain, e, fails)
        res.failed.append(f"{target.label}/{chain}: {e}")
        return
    except Exception as e:                           # noqa: BLE001
        # Same reasoning as _scan_scope: one broken source must not cost the
        # delivery of what every other source already found.
        conn.execute("BEGIN")
        fails = _mark_failure(conn, chain, address_id)
        conn.execute("COMMIT")
        log.exception("%s/%s: unexpected failure (consecutive: %d)",
                      target.label, chain, fails)
        res.failed.append(f"{target.label}/{chain}: {e}")
        return

    if marker == cursor.last_marker:
        conn.execute("BEGIN")
        _save_cursor(conn, chain, address_id, cursor)
        conn.execute("COMMIT")
        return
    res.changed += 1
    baseline = cursor.is_fresh

    conn.execute("BEGIN")
    try:
        previous = {r["position_key"]: r for r in conn.execute(
            """SELECT p.position_key, p.amount_raw, p.extra, s.decimals
                 FROM positions p LEFT JOIN assets s ON s.id = p.asset_id
                WHERE p.address_id=? AND p.protocol=?""",
            (address_id, source.name))}
        seen = set()

        for p in positions:
            seen.add(p.key)
            prev = previous.get(p.key)
            before = int(prev["amount_raw"]) if prev else None
            asset_id = (_asset_id(conn, p.asset_key, p.symbol, p.decimals,
                                  getattr(p, "contract", ""))
                        if p.asset_key else None)
            _upsert_position(conn, address_id, p, asset_id)

            if p.key == "account" or p.accrues:
                # Account value moves with unrealised PnL, a validator balance
                # with every epoch. Digest material, not an alert - otherwise an
                # open position notifies forever.
                if before is not None and before != p.amount_raw and not baseline:
                    detail = ("hyperliquid account value" if p.key == "account"
                              else f"{p.protocol} {p.key}")
                    moved = p.amount_raw - before
                    # A position without an asset_key has nothing to price it
                    # by: "staking:pending" is a bucket, not a coin.
                    _record_accrual(conn, chain, "", address_id, asset_id, moved,
                                    detail,
                                    prices.value(p.asset_key, moved, p.decimals)
                                    if prices and p.asset_key else None)
                continue

            if not baseline:
                eid = None
                if before is None:
                    eid = _position_event(
                        conn, chain, address_id, f"{p.key}:{p.amount_raw}",
                        _opened_text(p), p.amount_raw)
                elif before != p.amount_raw:
                    eid = _position_event(
                        conn, chain, address_id, f"{p.key}:{p.amount_raw}",
                        f"{p.key} {_fmt(before, p.decimals)} → "
                        f"{_fmt(p.amount_raw, p.decimals)} {p.symbol}", p.amount_raw)
                if eid:
                    res.new_events += 1
                    # The same threshold transfers get. A Hyperliquid spot
                    # balance drifts by a fraction of a cent every time funding
                    # settles, and without this every tick announces it: the
                    # position path used to queue unconditionally. Measured in
                    # the *change*, not the position - a $500 deposit still
                    # speaks, a 1.2-cent funding payment does not.
                    moved = (prices.value(p.asset_key, abs(p.amount_raw - (before or 0)),
                                          p.decimals)
                             if prices is not None and p.asset_key else None)
                    if _worth_saying(cfg, moved):
                        _queue(conn, eid, channels)
                    else:
                        res.below_threshold += 1

            eid = _liq_event(conn, cfg, chain, address_id, p)
            if eid:
                res.new_events += 1
                _queue(conn, eid, channels)

        for key, row in previous.items():
            if key in seen or key == "account":
                continue
            conn.execute("DELETE FROM positions WHERE address_id=? AND protocol=? "
                         "AND position_key=?", (address_id, source.name, key))
            if baseline:
                continue
            before = int(row["amount_raw"])
            # row["decimals"] rather than a literal 8: it happens to be right
            # for Bitcoin and for Hyperliquid's scale, and wrong by ten orders
            # of magnitude for an exited 18-decimal validator.
            eid = _position_event(conn, chain, address_id, f"{key}:closed:{before}",
                                  f"closed {key} ({_fmt(before, row['decimals'] or 8)})",
                                  before)
            if eid:
                res.new_events += 1
                _queue(conn, eid, channels)

        _save_cursor(conn, chain, address_id, Cursor(last_marker=marker, last_item=marker))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if baseline:
        log.info("%s/%s: baseline recorded, watching from now", target.label, chain)


def _opened_text(p) -> str:
    side = p.extra.get("side")
    if side:
        bits = [f"opened {side} {_fmt(abs(p.amount_raw), p.decimals)} {p.symbol}"]
        if p.extra.get("entry_px"):
            bits.append(f"@ {_px(p.extra['entry_px'])}")
        if p.extra.get("leverage"):
            bits.append(f"{p.extra['leverage']}x")
        if p.extra.get("liq_px"):
            bits.append(f"liq {_px(p.extra['liq_px'])}")
        return " ".join(bits)
    return f"{p.key} {_fmt(p.amount_raw, p.decimals)} {p.symbol}"


def _px(text: str) -> str:
    """Trim exchange price strings: 71408.1953557468 tells nobody anything."""
    try:
        return f"{float(text):,.2f}"
    except (TypeError, ValueError):
        return str(text)


def _liq_event(conn, cfg, chain: str, address_id: int, p) -> int | None:
    """Warn once per percentage point as a position approaches liquidation.

    Bucketing by whole percent is what stops this from firing every tick while
    still speaking up again when the risk gets worse.
    """
    raw = p.extra.get("liq_distance_pct")
    if raw is None:
        return None
    distance = float(raw)
    if distance > cfg.thresholds.liq_distance_pct:
        return None
    bucket = int(distance)
    return _position_event(
        conn, chain, address_id, f"{p.key}:liq:{bucket}",
        f"{p.symbol} {p.extra.get('side', '')} within {distance:.1f}% of liquidation "
        f"(mark {_px(p.extra.get('mark_px'))}, liq {_px(p.extra.get('liq_px'))})",
        p.amount_raw, kind=EventKind.POSITION_CHANGE.value)
