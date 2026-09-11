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


async def claim_reminders(cfg, conn, sources, prices=None) -> list[dict]:
    """The daily claim reminder, which can never cost the digest its delivery.

    Everything below is a convenience on top of the digest: the numbers it
    carries are worth saying, and none of them are worth the morning message
    not arriving. Delivery is what `digest.send` guards its baseline on, so a
    failure here has to end as an empty list rather than an exception.
    """
    try:
        return await _claim_reminders(cfg, conn, sources, prices)
    except Exception:  # noqa: BLE001 - a digest must survive a fee-read failure
        log.exception("daily LP fee reminder failed")
        return []


async def _claim_reminders(cfg, conn, sources, prices=None) -> list[dict]:
    """Price daily Uni v3 fee reads and retain only claim-worthy positions.

    The regular position scan has already established which NFTs are open and
    which two assets each holds. Reusing that state makes the daily path one
    simulated `collect()` per NFT instead of repeating NFT enumeration, pool
    discovery and price reads.
    """
    source = (sources or {}).get("univ3")
    read_fees = getattr(source, "claimable_fees", None)
    if read_fees is None:
        return []

    rows = conn.execute("""
        SELECT a.id AS address_id, a.address, a.label, p.position_key, p.extra,
               s.asset_key, s.symbol, s.decimals
          FROM positions p
          JOIN addresses a ON a.id=p.address_id
          JOIN assets s ON s.id=p.asset_id
         WHERE a.enabled=1 AND p.protocol='univ3'
         ORDER BY a.id, p.position_key""").fetchall()
    by_address: dict[int, list] = {}
    for row in rows:
        by_address.setdefault(row["address_id"], []).append(row)

    reminders = []
    for held in by_address.values():
        first = held[0]
        requested: dict[tuple[str, str], list[int]] = {}
        parsed = []
        for row in held:
            try:
                chain, token_id, leg = row["position_key"].split(":")
                extra = json.loads(row["extra"] or "{}")
                venue = extra["venue"]
                key = (chain, venue, int(token_id), int(leg))
            except (KeyError, TypeError, ValueError):
                continue
            requested.setdefault((chain, venue), []).append(key[2])
            parsed.append((key, row))
        if not requested:
            continue
        requested = {key: sorted(set(ids)) for key, ids in requested.items()}
        target = Target(address=first["address"], label=first["label"],
                        watch=frozenset(("univ3",)), chains=())
        try:
            fees = await read_fees(target, requested)
        except Exception:  # noqa: BLE001 - one address must not cost the others
            log.exception("%s/univ3: daily LP fee read failed", first["label"])
            continue

        totals: dict[tuple[str, str, int], dict] = {}
        for (chain, venue, token_id, leg), row in parsed:
            raw = fees.get((chain, venue, token_id))
            if raw is None or leg >= len(raw):
                continue
            amount = raw[leg]
            slot = totals.setdefault((chain, venue, token_id),
                                     {"usd": 0.0, "unpriced": []})
            if not amount:
                continue
            value = _usd_of(prices, row["asset_key"], amount, row["decimals"])
            if value is None:
                slot["unpriced"].append(
                    f"{_fmt(amount, row['decimals'])} {row['symbol']}")
            else:
                slot["usd"] += value
        for (chain, venue, token_id), slot in totals.items():
            # An unpriced leg always speaks: the two legs of one position are
            # claimed together, so dropping the NFT over the leg nobody has
            # quoted hides the dollars sitting in the other one.
            if slot["unpriced"] or slot["usd"] >= cfg.thresholds.claim_reminder_usd:
                reminders.append({"label": first["label"], "chain": chain,
                                  "venue": venue, "token_id": token_id,
                                  "usd": slot["usd"],
                                  "unpriced": slot["unpriced"]})
    return sorted(reminders, key=lambda row: -row["usd"])


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


def _pending_batch(conn, channel: str) -> list[dict]:
    """Everything this one channel still owes, earlier ticks included.

    Per channel, because channels do not fall behind together. Built across all
    of them, the batch re-sent to whichever channel had already taken it - every
    tick, for as long as the other stayed broken - and `resent` was a MAX over
    the channels, so a first delivery to the channel that was behind arrived
    labelled "confirmed" because a different one had sent it already. With the
    channel fixed there is exactly one notifications row per event, which is why
    the GROUP BY is gone too.
    """
    rows = conn.execute(
        """SELECT e.id, e.kind, e.direction, e.amount_raw, e.counterparty,
                  e.status, e.detail, e.scope, e.uid, e.usd, a.label, a.chain,
                  s.symbol, s.decimals, s.kind AS asset_kind,
                  n.sent_at IS NOT NULL AS resent
           FROM notifications n
           JOIN events e ON e.id = n.event_id
           JOIN addresses a ON a.id = e.address_id
           LEFT JOIN assets s ON s.id = e.asset_id
           WHERE n.status = 'pending' AND n.channel = ?
           ORDER BY e.id""", (channel,)).fetchall()
    return [dict(r) for r in rows]


def queue_state(conn) -> dict:
    """What delivery still owes, and whether the queue is moving.

    There is no 'failed' count here because there is no 'failed' state: a
    notification that could not be sent stays pending and the next tick tries it
    again, which is the entire reason events and notifications are separate
    tables. Nothing ever wrote the status, so the "N failed" this replaces was a
    zero that could not become anything else - a diagnostic line that could only
    ever reassure. A queue that is not draining shows up as a pending row that
    keeps getting older, so that is what this reports, with the last thing a
    channel actually complained about.
    """
    row = conn.execute(
        """SELECT COUNT(*) AS n, MIN(e.ts) AS oldest
             FROM notifications x JOIN events e ON e.id = x.event_id
            WHERE x.status = 'pending'""").fetchone()
    last = conn.execute(
        """SELECT channel, error FROM notifications
            WHERE error IS NOT NULL ORDER BY id DESC LIMIT 1""").fetchone()
    return {"pending": row["n"], "oldest": row["oldest"],
            "error": f"{last['channel']}: {last['error']}" if last else ""}


def _who(counterparty: str | None) -> str:
    """The other side of a transfer, short enough to sit at the end of a line.

    An address is cut in the middle: both ends are what anyone checks one by,
    while the thirty characters between them cost the line more than they say.
    A counterparty holding a colon is a label this codebase wrote rather than an
    address a provider gave - "validator:806123" - and there the tail is the
    whole of the identity, so it is left alone. Cutting everything to twelve
    made every beacon withdrawal read "validator:80…", which names a different
    validator each time, and an unknown counterparty read "?…" - an ellipsis
    standing for nothing that was elided.
    """
    if not counterparty:
        return "?"
    if ":" in counterparty or len(counterparty) <= 13:
        return counterparty
    return f"{counterparty[:8]}…{counterparty[-4:]}"


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
        if e["resent"]:
            tail = "  ↳ confirmed"          # follow-up to a message already sent
        elif e["status"] != "confirmed":
            tail = "  (in mempool)"
        else:
            tail = ""
        where = f"{e['label']}/{e['scope']}" if e["scope"] else e["label"]
        worth = f" (${e['usd']:,.2f})" if e["usd"] else ""
        if e.get("asset_kind") == "debt":
            # A debt token is minted to the borrower and burned on repayment, so
            # the ERC-20 direction is the opposite of what happened to the
            # money. An arrow pointing in, next to a dollar figure, reads as a
            # $5,000 deposit when what happened was taking on $5,000 of debt -
            # and the counterparty is the zero address, which names nobody. Said
            # with the verb _record_debt_change already uses, so the two paths
            # that can notice a borrow report it the same way.
            verb = "borrowed" if e["direction"] == Direction.IN.value else "repaid"
            lines.append(f"{where}  {verb} {amount} "
                         f"{e['symbol'] or ''}{worth}{tail}".rstrip())
            continue
        arrow = ARROW.get(Direction(e["direction"]), "·") if e["direction"] else "·"
        who = _who(e["counterparty"])
        lines.append(f"{where}  {arrow} {amount} {e['symbol'] or ''}{worth}  "
                     f"{who}{tail}".rstrip())
    title = "Portfolio: 1 change" if len(events) == 1 else f"Portfolio: {len(events)} changes"
    return Message(title=title, body="\n".join(lines),
                   severity=Severity.HIGH if high else Severity.NORMAL,
                   kind=EventKind.TRANSFER)


async def _deliver(conn, router) -> dict[str, str | None]:
    """Hand each channel what it is still owed, and record the outcome per channel.

    One channel at a time, and one batch per channel: a shared batch meant that
    a Discord outage made the next tick send Telegram a second copy of every
    alert it had already delivered.
    """
    results: dict[str, str | None] = {}
    for channel in router.channels:
        batch = _pending_batch(conn, channel)
        if not batch:
            continue
        err = await router.send_to(channel, render(batch))
        results[channel] = err
        ids = [e["id"] for e in batch]
        conn.execute("BEGIN")
        try:
            if err is None:
                # error=NULL: a stale message from an outage that has since
                # cleared would otherwise be reported by queue_state forever.
                conn.executemany(
                    "UPDATE notifications SET status='sent', sent_at=?, error=NULL "
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
    """Store the position, and append to its history when it actually moved.

    The state row is overwritten in place, so the history is a table of its
    own: quantities are the one part of the portfolio that no provider can
    hand back later, while prices are recorded independently and a value is
    the two multiplied.
    """
    previous = conn.execute(
        """SELECT amount_raw, extra FROM positions
            WHERE address_id=? AND protocol=? AND position_key=?""",
        (address_id, p.protocol, p.key)).fetchone()
    extra = json.dumps(p.extra)
    now = _now()
    conn.execute(
        """INSERT INTO positions(address_id, protocol, position_key, asset_id,
                                 amount_raw, usd, extra, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(address_id, protocol, position_key) DO UPDATE SET
             asset_id=excluded.asset_id, amount_raw=excluded.amount_raw,
             usd=excluded.usd, extra=excluded.extra, updated_at=excluded.updated_at""",
        (address_id, p.protocol, p.key, asset_id, str(p.amount_raw), p.usd,
         extra, now))
    if previous is None or _position_moved(previous, p.amount_raw, p.extra):
        _record_position_snapshot(conn, address_id, p.protocol, p.key, asset_id,
                                  p.amount_raw, p.usd, extra, now)


def _position_moved(previous, amount_raw: int, extra: dict) -> bool:
    """Whether this position is worth another row of history.

    The amount, for everything that holds a quantity. For a leveraged position
    also its unrealised PnL: a perp's size sits unchanged for weeks while what
    it is worth moves every tick, and that number is the one thing here that
    cannot be recomputed afterwards from a price - it needs the entry price the
    venue reported at the time. Recording on change rather than on every tick
    is what keeps this table smaller than `balance_snapshots`, which writes a
    row per fetched asset whether or not it moved.
    """
    if int(previous["amount_raw"] or 0) != amount_raw:
        return True
    if not extra.get("side"):
        return False
    return _extra(previous).get("unrealized_pnl") != extra.get("unrealized_pnl")


def _record_position_snapshot(conn, address_id: int, protocol: str, key: str,
                              asset_id: int | None, amount_raw: int,
                              usd: float | None, extra: str, ts: str) -> None:
    conn.execute(
        """INSERT INTO position_snapshots(address_id, protocol, position_key,
                                          asset_id, amount_raw, usd, extra, ts)
           VALUES (?,?,?,?,?,?,?,?)""",
        (address_id, protocol, key, asset_id, str(amount_raw), usd, extra, ts))


def _position_event(conn, chain: str, address_id: int, uid: str, detail: str,
                    amount_raw: int, kind: str = EventKind.POSITION_CHANGE.value,
                    pnl_usd: float | None = None,
                    usd: float | None = None) -> int | None:
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
                              amount_raw, usd, ts, status, detail, pnl_usd)
           VALUES (?,'',?,'',?,?,?,?,?, 'confirmed', ?, ?)""",
        (chain, address_id, uid, kind, str(amount_raw), usd, _now(), detail, pnl_usd))
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

    previous = {r["position_key"]: r for r in conn.execute(
        """SELECT p.position_key, p.amount_raw, p.extra, p.asset_id, s.decimals,
                  s.symbol, s.asset_key
             FROM positions p LEFT JOIN assets s ON s.id = p.asset_id
            WHERE p.address_id=? AND p.protocol=?""",
        (address_id, source.name))}
    # Deliberately outside the transaction below: this asks the venue over the
    # network, and a transaction held open across a request is a lock held for
    # as long as the provider feels like taking.
    trades = ({} if baseline else
              await _closed_trades(conn, source, target, chain, address_id,
                                   previous, positions))

    conn.execute("BEGIN")
    try:
        seen = set()

        for p in positions:
            seen.add(p.key)
            prev = previous.get(p.key)
            before = int(prev["amount_raw"]) if prev else None
            asset_id = (_asset_id(conn, p.asset_key, p.symbol, p.decimals,
                                  getattr(p, "contract", ""))
                        if p.asset_key else None)
            # Before the accrues shortcut below: a position whose amount is
            # deliberately silent can still have crossed a line.
            eid = _state_event(conn, cfg, chain, address_id, p, prev)
            if eid:
                res.new_events += 1
                _queue(conn, eid, channels)

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
                # What the move was worth, signed, computed once: the threshold
                # below decides with it and the line says it, so the number that
                # rang and the number that is read are the same number.
                moved = _usd_of(prices, p.asset_key,
                                p.amount_raw - (before or 0), p.decimals)
                # Except on a leveraged position, where it is a notional rather
                # than money. A perp's dollars sitting in the same parentheses
                # as a spot balance's would read as the same kind of thing.
                shown = None if p.extra.get("side") else moved
                if before is None:
                    eid = _position_event(
                        conn, chain, address_id, f"{p.key}:{p.amount_raw}",
                        _opened_text(p, shown), p.amount_raw, usd=shown)
                elif before != p.amount_raw:
                    trade = trades.get(p.key)
                    eid = _position_event(
                        conn, chain, address_id, f"{p.key}:{p.amount_raw}",
                        _changed_text(p, before, shown) + _trade_tail(trade),
                        p.amount_raw,
                        pnl_usd=trade.pnl_usd if trade else None, usd=shown)
                if eid:
                    res.new_events += 1
                    # The same threshold transfers get. A Hyperliquid spot
                    # balance drifts by a fraction of a cent every time funding
                    # settles, and without this every tick announces it: the
                    # position path used to queue unconditionally. Measured in
                    # the *change*, not the position - a $500 deposit still
                    # speaks, a 1.2-cent funding payment does not.
                    if _worth_saying(cfg, moved):
                        _queue(conn, eid, channels)
                    else:
                        res.below_threshold += 1

        for key, row in previous.items():
            if key in seen or key == "account":
                continue
            # Zero before the delete, and unconditionally: a position that is
            # gone from the state table but whose history stops at its last
            # size reads, to anything carrying the last known value forward, as
            # still being held - forever. The terminal row is what ends it.
            _record_position_snapshot(conn, address_id, source.name, key,
                                      row["asset_id"], 0, None, row["extra"], _now())
            conn.execute("DELETE FROM positions WHERE address_id=? AND protocol=? "
                         "AND position_key=?", (address_id, source.name, key))
            if baseline:
                continue
            before = int(row["amount_raw"])
            # row["decimals"] rather than a literal 8: it happens to be right
            # for Bitcoin and for Hyperliquid's scale, and wrong by ten orders
            # of magnitude for an exited 18-decimal validator.
            trade = trades.get(key)
            worth = (None if _extra(row).get("side") else
                     _usd_of(prices, row["asset_key"], before, row["decimals"] or 8))
            eid = _position_event(conn, chain, address_id, f"{key}:closed:{before}",
                                  _closed_text(key, before, row, trade, worth), before,
                                  pnl_usd=trade.pnl_usd if trade else None, usd=worth)
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


def _usd_of(prices, asset_key: str, amount_raw: int, decimals: int) -> float | None:
    """What an amount is worth, or None when nobody has quoted the asset."""
    if prices is None or not asset_key:
        return None
    return prices.value(asset_key, amount_raw, decimals)


def _usd_tail(usd: float | None, signed: bool = False) -> str:
    """The dollar figure a position line carries, in the parentheses transfers
    already use.

    Nothing at all when the price is unknown: "($0.00)" is a claim that the
    thing is worthless, and an unpriced asset is one nobody has quoted - the
    same reason `_worth_saying` lets it through rather than silencing it.
    Anything under a cent says so instead of rounding to zero, because a line
    about dust must still be readable as dust.
    """
    if usd is None:
        return ""
    sign = ("+" if usd >= 0 else "-") if signed else ""
    if abs(usd) < 0.01:
        return f" ({sign}<$0.01)"
    return f" ({sign}${abs(usd):,.2f})"


def _opened_text(p, usd: float | None = None) -> str:
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
    return f"{p.key} {_fmt(p.amount_raw, p.decimals)} {p.symbol}{_usd_tail(usd)}"


def _px(text: str) -> str:
    """Trim exchange price strings: 71408.1953557468 tells nobody anything.

    How much to trim depends on the coin. Two decimals is plenty of bitcoin and
    is useless for a coin that trades at $0.81, where it hides more than a
    percent - and a percent is the whole trade.
    """
    try:
        value = float(text)
    except (TypeError, ValueError):
        return str(text)
    size = abs(value)
    places = 2 if size >= 100 else 4 if size >= 1 else 6
    out = f"{value:,.{places}f}"
    return out.rstrip("0").rstrip(".") if "." in out else out


def _changed_text(p, before: int, usd: float | None = None) -> str:
    """Signed, because the arrow says which way and not how much: a balance
    going 0.5 → 65.5 USDC and one going 0.0008 → 0.00001 UBTC are two sides of
    one sale, and only the dollars make them recognisable as that."""
    return (f"{p.key} {_fmt(before, p.decimals)} → "
            f"{_fmt(p.amount_raw, p.decimals)} {p.symbol}"
            f"{_usd_tail(usd, signed=True)}")


def _closed_text(key: str, before: int, row, trade,
                 usd: float | None = None) -> str:
    """The line a closed position leaves behind.

    With a Trade it mirrors `_opened_text`, so the two ends of the same position
    read as a pair. Without one it says only what it knows - what closed and how
    big it was. That is the whole point of the venue's fills being optional:
    a close whose result we cannot look up gets no number rather than a number
    from the last snapshot, which would be wrong by however far the price moved
    while nobody was looking.
    """
    decimals = row["decimals"] or 8
    plain = f"closed {key} {_fmt(before, decimals)}{_usd_tail(usd)}"
    if trade is None:
        return plain
    extra = _extra(row)
    side = extra.get("side", "")
    symbol = row["symbol"] or key.split(":")[-1]
    verb = "liquidated" if trade.liquidated else "closed"
    bits = [verb, side, _fmt(abs(before), decimals), symbol]
    if trade.exit_px:
        bits.append(f"@ {_px(trade.exit_px)}")
    return " ".join(b for b in bits if b) + _trade_tail(trade, exit_px=False)


def _trade_tail(trade, exit_px: bool = True) -> str:
    """The numbers a Trade adds to a line, in the order they are read.

    The profit first, because it is the question; the fee after it, because it
    is not deducted from it. Nothing at all when there is no Trade - an empty
    string here is what keeps every other position source's text unchanged.
    """
    if trade is None:
        return ""
    parts = []
    if exit_px and trade.exit_px:
        parts.append(f"@ {_px(trade.exit_px)}")
    parts.append(f"pnl {trade.pnl_usd:+,.2f}")
    if trade.fee_usd:
        parts.append(f"fee {trade.fee_usd:,.2f}")
    if exit_px and trade.liquidated:
        # The close branch says it in the verb; a reduce has no verb to say it in.
        parts.append("(liquidation)")
    return "   " + "   ".join(parts)


def _extra(row) -> dict:
    try:
        return json.loads(row["extra"] or "{}")
    except (ValueError, TypeError):
        return {}


async def _closed_trades(conn, source, target, chain: str, address_id: int,
                         previous: dict, positions: list) -> dict:
    """Ask the venue what the positions that just shrank or vanished made.

    Only when one did: this is a request, and a tick where nothing closed must
    not pay for it - that is the same bargain the cheap probe makes. A source
    with nothing to say is the normal case, and a source that fails to answer
    costs the message its numbers and nothing else.
    """
    ask = getattr(source, "trades", None)
    if ask is None:
        return {}
    now = {p.key: p.amount_raw for p in positions}
    closed = [
        key for key, row in previous.items()
        if key != "account" and (
            key not in now
            or abs(now[key]) < abs(int(row["amount_raw"]))
            # A flip realises the whole old position while the key never
            # disappears and the size may even grow: long 112 to short 200.
            or now[key] * int(row["amount_raw"]) < 0)]
    if not closed:
        return {}
    try:
        return await ask(target, _since_ms(conn, chain, address_id)) or {}
    except Exception as e:                           # noqa: BLE001
        log.warning("%s/%s: closed %s, but no trade detail (%s)",
                    target.label, chain, ", ".join(sorted(closed)), e)
        return {}


def _since_ms(conn, chain: str, address_id: int) -> int | None:
    """When this address was last scanned, in the milliseconds venues speak."""
    row = conn.execute(
        "SELECT last_ok_at FROM cursors WHERE chain=? AND address_id=? AND scope=''",
        (chain, address_id)).fetchone()
    if not row or not row["last_ok_at"]:
        return None
    try:
        when = datetime.fromisoformat(row["last_ok_at"])
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp() * 1000)


def _state_event(conn, cfg, chain: str, address_id: int, p, prev) -> int | None:
    """An alert about a position's *state* rather than its size.

    Separate from the amount diff, and reached even for positions marked
    `accrues`, because the two answer different questions. A perp's value moves
    every second and a range position's composition moves with every trade -
    both are noise - yet each can cross a line that is worth exactly one
    message: liquidation coming close, liquidity falling out of range.
    """
    return (_liq_event(conn, cfg, chain, address_id, p)
            or _range_event(conn, chain, address_id, p, prev))


def _range_event(conn, chain: str, address_id: int, p, prev) -> int | None:
    """Concentrated liquidity left, or re-entered, its price range.

    Reported on the transition rather than on the state, and read from what was
    stored last tick: a position that is simply sitting outside its range is old
    news, while the moment it stopped earning is the news. Uniswap's own
    convention decides the edge - the upper bound is exclusive.
    """
    now = p.extra.get("in_range")
    if now is None:
        return None
    was = None
    if prev is not None and prev["extra"]:
        try:
            was = json.loads(prev["extra"]).get("in_range")
        except (ValueError, TypeError):
            was = None
    if was is None or was == now:
        return None                     # first sight, or nothing moved
    venue = p.extra.get("venue", p.protocol)
    pair = p.extra.get("token_id", "")
    if now == "false":
        text = (f"{venue} #{pair} {p.symbol} left its range - the liquidity "
                f"stopped earning fees")
    else:
        text = f"{venue} #{pair} {p.symbol} is back in range and earning again"
    return _position_event(
        conn, chain, address_id, f"{p.key}:range:{now}:{p.extra.get('tick', '')}",
        text, p.amount_raw)


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
