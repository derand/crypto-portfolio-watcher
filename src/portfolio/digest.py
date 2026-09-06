"""The daily summary: what everything is worth, and what quietly happened.

This is where the events that deliberately never alert finally get said out
loud - accrued yield, network fees, account drift. Anything that would be spam
every fifteen minutes is exactly right once a day.
"""

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import db as dbmod
from .protocols.hyperliquid import SPOT_WRAPPERS
from .models import (Block, Direction, EventKind, Message, Severity, flatten,
                     format_display, format_units)

log = logging.getLogger(__name__)

LAST_TOTAL = "digest:last_total"
LAST_DATE = "digest:last_date"


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def collect(conn, prices=None) -> dict:
    balances = _rows(conn, """
        SELECT a.label, s.asset_key, s.symbol, s.decimals, s.chain,
               b.amount_raw, b.updated_at
        FROM balances b
        JOIN addresses a ON a.id = b.address_id
        JOIN assets s ON s.id = b.asset_id
        WHERE a.enabled = 1 AND s.whitelisted = 1
              AND CAST(b.amount_raw AS INTEGER) != 0
        ORDER BY a.label, s.asset_key""")
    for row in balances:
        row["usd"] = (prices.value(row["asset_key"], int(row["amount_raw"]),
                                   row["decimals"]) if prices else None)

    positions = _rows(conn, """
        SELECT a.label, p.protocol, p.position_key, p.amount_raw, p.usd, p.extra,
               s.asset_key, s.symbol, s.decimals
        FROM positions p JOIN addresses a ON a.id = p.address_id
        LEFT JOIN assets s ON s.id = p.asset_id
        WHERE a.enabled = 1 ORDER BY a.label, p.position_key""")
    for row in positions:
        row["extra"] = json.loads(row["extra"] or "{}")

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent = _rows(conn, """
        SELECT e.kind, e.direction, e.amount_raw, e.usd, e.detail, e.ts,
               a.label, s.symbol, s.decimals
        FROM events e JOIN addresses a ON a.id = e.address_id
        LEFT JOIN assets s ON s.id = e.asset_id
        WHERE e.ts >= ? ORDER BY e.ts""", (since,))

    total = 0.0
    unpriced = 0
    for row in balances:
        if row["usd"] is None:
            unpriced += 1
        else:
            total += row["usd"]
    for row in positions:
        # Perp notionals are exposure, not holdings. What a perp does add is its
        # unrealised PnL: the margin behind it is already sitting in the spot
        # balance, but the profit on top of it is not.
        if row["position_key"].startswith("perp:"):
            try:
                total += float(row["extra"].get("unrealized_pnl") or 0)
            except (TypeError, ValueError):
                pass
            continue
        # Hyperliquid reserves perp margin inside the spot USDC balance - the
        # spot row's `hold` equals marginUsed exactly, and an account with USDC
        # and no position reports accountValue 0. Adding accountValue on top of
        # spot therefore counts the same dollars twice; on one bot that turned
        # $104 into $207.
        if row["position_key"] == "account":
            continue
        if row["usd"] is not None:
            total += row["usd"]
        elif prices is not None:
            # Ask the position which asset it holds. Deriving it from the key
            # works for "spot:USDC" and quietly fails for "staking:pending",
            # where the last segment is a bucket name rather than a coin.
            asset_key = (row["asset_key"]
                         or f"{row['protocol']}:{row['position_key'].split(':')[-1]}")
            value = prices.value(asset_key, int(row["amount_raw"]), _scale(row))
            if value is not None:
                total += value
                row["usd"] = value
            else:
                unpriced += 1

    # Stored in UTC, shown in local time: "as of 10:39" next to a scan the
    # reader watched run at 13:39 reads as a stale database.
    stamps = [r["updated_at"] for r in balances if r["updated_at"]]
    as_of = ""
    if stamps:
        try:
            local = datetime.fromisoformat(max(stamps)).astimezone()
            as_of = local.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            as_of = max(stamps)[:16].replace("T", " ")

    return {"balances": balances, "positions": positions, "recent": recent,
            "total": total, "unpriced": unpriced, "as_of": as_of}


def _scale(row) -> int:
    """Decimals of what a position holds.

    Hyperliquid scales everything to 8, so that used to be a safe constant.
    A validator balance is ETH at 18: reading it as 8 reports 32 ETH as three
    hundred billion and values the portfolio in the trillions.
    """
    return int(row["decimals"]) if row.get("decimals") is not None else 8


DUST_USD = 1.0
"""Holdings worth less than this fold into one line. Four addresses hold ETH
dust that will never be spent; listing each of them pushes the real money off
the first screen."""

OTHER_SHARE = 0.01
"""Groups worth less than this fraction of the portfolio are summarised as
"other". A one-percent slice does not earn a line of its own next to a total."""

ONCHAIN = "on-chain"
"""The venue for anything held in a wallet. Everything else is a claim on a
protocol - a Hyperliquid balance is the exchange's promise, not a coin in an
address - and the two must not share a table."""

CHAIN_SHORT = {"ethereum": "eth", "arbitrum": "arb", "base": "base", "bsc": "bsc",
               "bitcoin": "btc", "hyperliquid": "hl"}


def _short(label: str, width: int = 8) -> str:
    """A label narrow enough for a phone, still recognisable.

    Hyphenated labels lose all but three letters of each part - savings-evm
    becomes sav-evm - because the alternative is a column wide enough to push
    the dollars off the screen.
    """
    if len(label) <= width:
        return label
    parts = label.split("-")
    if len(parts) > 1:
        cut = "-".join(p[:3] for p in parts)
        if len(cut) <= width:
            return cut
    return label[:width]


def _where(label: str, chain: str = "", key: str = "") -> str:
    """Which of my places this holding sits in: label plus chain or bucket.

    A position key carries its bucket after the colon - staking:pending,
    validator:999999 - and the bucket is the part that distinguishes two rows
    of the same coin at the same address.
    """
    if key.startswith("validator:"):
        # A validator index is unique across the whole beacon chain, so the
        # address label in front of it says nothing the index does not.
        return f"val·{key.partition(':')[2]}"
    if key:
        bucket = key.rpartition(":")[2] if ":" in key else key
        if key == "account":
            bucket = "perp"
        elif key.startswith("spot:"):
            bucket = "spot"
        return f"{_short(label)}·{bucket}"
    return f"{_short(label)}·{CHAIN_SHORT.get(chain, chain[:4])}"


def _asset_groups(cfg) -> tuple[dict, dict]:
    """symbol and asset_key to display group, straight from the whitelist.

    Hyperliquid's bridged wrappers are seeded here rather than configured: one
    UBTC is one BTC by construction, and the whitelist only speaks about EVM
    contracts. Grouping is presentation - the price still comes from the
    token's own order book - so a lookalike lands in the wrong group at worst,
    never at the wrong value.
    """
    by_key, by_symbol = {}, dict(SPOT_WRAPPERS)
    for token in (getattr(cfg, "tokens", ()) or ()) if cfg else ():
        if token.group:
            by_key[token.asset_key] = token.group
            by_symbol.setdefault(token.symbol, token.group)
    return by_key, by_symbol


def _holdings(data: dict, cfg) -> list[dict]:
    """Balances and non-perp positions as one list of comparable rows."""
    by_key, by_symbol = _asset_groups(cfg)

    def grouped(asset_key, symbol):
        return by_key.get(asset_key) or by_symbol.get(symbol) or symbol or "?"

    rows = []
    for r in data["balances"]:
        rows.append({"raw": int(r["amount_raw"]), "decimals": r["decimals"],
                     "symbol": r["symbol"], "usd": r["usd"], "label": r["label"],
                     "venue": ONCHAIN,
                     "where": _where(r["label"], chain=r["chain"]),
                     "group": grouped(r["asset_key"], r["symbol"])})
    for r in data["positions"]:
        key = r["position_key"]
        # "account" is the exchange's own view of margin already held in spot;
        # listing it as a holding is the double count that collect() drops.
        if key.startswith("perp:") or key == "account" or int(r["amount_raw"]) == 0:
            continue
        symbol = r["symbol"] or key.rpartition(":")[2]
        scale = _scale(r)
        # A validator's ETH is on a chain, not on an exchange: beacon positions
        # belong with the wallets, Hyperliquid's do not.
        venue = ONCHAIN if r["protocol"] == "beacon" else r["protocol"]
        rows.append({"raw": int(r["amount_raw"]), "decimals": scale,
                     "symbol": symbol, "usd": r["usd"], "label": r["label"],
                     "venue": venue,
                     "where": _where(r["label"], key=key),
                     "group": grouped(r["asset_key"], symbol)})
    return rows


def _amount(raw: int, decimals: int, sig: int = 4) -> str:
    """A quantity for the table: significant digits, not decimal places.

    32.009710 and 0.020992 need different numbers of decimals to say the same
    thing, and a column padded for the worst case is a column that wraps. Two
    guards: the integer part is never rounded away (129,186 MAX stays itself),
    and a value too small to show at all becomes 1.4e-9 rather than "0" - a
    yield line reading zero looks like a broken watcher. Decimal throughout:
    this is an amount, not a dollar figure.
    """
    value = Decimal(raw).scaleb(-decimals)
    if not value:
        return "0"
    places = max(0, sig - 1 - value.copy_abs().adjusted())
    if places > 8:
        return f"{value:.1e}"
    text = format_units(int(value.scaleb(places).to_integral_value()), places)
    return text if text.strip("-0.") else f"{value:.1e}"


def _money(usd: float | None) -> str:
    """Dollars for a table column. Thousands become 80.5k: five characters
    instead of eight, on every row of the widest column in the digest."""
    if usd is None:
        return "—"
    if abs(usd) >= 1_000_000:
        return f"{usd / 1_000_000:,.1f}M"
    if abs(usd) >= 10_000:
        return f"{usd / 1_000:,.1f}k"
    return f"{usd:,.0f}"


def _align(quantities: list[str]) -> list[str]:
    """Pad on both sides of the decimal point, so the points line up.

    A whole number gets a space where the others have their point: printing
    "1250." instead of "1250" reads as a number that lost its tail.
    """
    split = [q.partition(".") for q in quantities]
    left = max((len(w) for w, _, _ in split), default=0)
    right = max((len(f) for _, _, f in split), default=0)
    if not right:
        return [f"{w:>{left}}" for w, _, _ in split]
    return [f"{w:>{left}}{point or ' '}{frac:<{right}}" for w, point, frac in split]


def _sort_key(row):
    # Unpriced last: an unknown price is not the same as no money.
    return (0 if row["usd"] is None else 1, row["usd"] or 0.0)


SYMBOL_WIDTH = 10
"""Longest symbol a table column will hold. aEthLidoWETH is twelve characters
and pushes the dollars off a phone screen; every other symbol held here fits."""

LABEL_WIDTH = 8
"""Longest address label inside a "where" cell, before the dot. Same number
_where already uses; named here because the table may cut it further."""

MIN_SYMBOL, MIN_LABEL, MIN_SIG = 6, 5, 3
"""Floors. Past these a row stops naming what it holds and where it sits, which
is worse than the wrap the trimming exists to avoid - so a table that still
does not fit is sent as it is."""

SIG = 4
"""Significant digits in a quantity, before the last resort of dropping one."""

TABLE_WIDTH = 37
"""Characters that fit on one line of a collapsed block on the phone.

Measured with a ruler message rather than guessed (PLAN §9): a bare <pre>
scrolls sideways and took all 44 widths sent, but the same <pre> inside
<blockquote expandable> wraps, and folds first at 38. A wrapped row is worse
than no table: the dollars land on their own line under the wrong holding.

One number for every table, collapsed or not. Which branch a table lands in
depends on how many accounts hold the balances, and a table whose width moves
with the account count is a table that wraps on a day nobody was watching.
"""


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def _narrow(where: str, label_width: int) -> str:
    """A "where" cell with only its label cut.

    What follows the dot is a chain or a bucket, and it is never touched: base
    and bsc differ in their last two characters, so cutting there quietly turns
    two places into one.
    """
    head, dot, tail = where.rpartition("·")
    if not dot:
        return _clip(where, label_width)
    return f"{_clip(head, label_width)}{dot}{tail}"


def _cells(shown: list[dict], sig: int, symbol_width: int, label_width: int):
    """The four columns at one level of trimming, and the width they need."""
    columns = ([_amount(r["raw"], r["decimals"], sig) for r in shown],
               [_clip(r["symbol"] or "?", symbol_width) for r in shown],
               [_narrow(r["where"], label_width) for r in shown],
               [_money(r["usd"]) for r in shown])
    columns = (_align(columns[0]),) + columns[1:]
    widths = [max((len(c) for c in col), default=0) for col in columns]
    return columns, widths, sum(widths) + 3


def _plans():
    """Levels of trimming, widest first: each step gives up the least
    informative character left.

    The symbol's tail goes first - aArbwstETH is still itself as aArbwst… -
    then the address label, and only last a significant digit of the quantity,
    which is the one column that is a measurement rather than a name.
    """
    for width in range(SYMBOL_WIDTH, MIN_SYMBOL - 1, -1):
        yield SIG, width, LABEL_WIDTH
    for width in range(LABEL_WIDTH - 1, MIN_LABEL - 1, -1):
        yield SIG, MIN_SYMBOL, width
    yield MIN_SIG, MIN_SYMBOL, MIN_LABEL


def _names(rows: list[dict]) -> str:
    """The symbols behind a folded line, so nothing disappears unnamed."""
    counted = Counter(r["symbol"] or "?" for r in rows)
    parts = [s if n == 1 else f"{s}x{n}" for s, n in counted.most_common(4)]
    if len(counted) > 4:
        parts.append(f"+{len(counted) - 4} more")
    return ", ".join(parts)


def _table(rows: list[dict], dust_usd: float = DUST_USD,
           width: int = TABLE_WIDTH) -> list[str]:
    """Columns sized to their contents, then to the screen: quantity, symbol,
    place, dollars.

    Padding to a fixed width is what makes this a table rather than a list, and
    only a monospace channel gets the benefit - hence Block.mono. Sizing to the
    contents alone is what wrapped it: thirteen addresses and a twelve-character
    symbol built a 41-character line, and the phone folded every row of it. So
    the columns are asked to fit `width` as well, giving up characters in the
    order _plans sets. Dollars are never cut - the number being looked for
    cannot be the one that is missing.

    Two kinds of row are folded into a single line: small change, and holdings
    with no price. Small change is summed, because it is still money; unpriced
    holdings are only named and counted, because adding them up would invent a
    number. Thirteen addresses produce a dozen of each.
    """
    dust = [r for r in rows if r["usd"] is not None and r["usd"] < dust_usd]
    unpriced = [r for r in rows if r["usd"] is None]
    folded = {id(r) for r in dust} | {id(r) for r in unpriced}
    shown = [r for r in rows if id(r) not in folded]
    shown.sort(key=_sort_key, reverse=True)
    for sig, symbol_width, label_width in _plans():
        columns, widths, needed = _cells(shown, sig, symbol_width, label_width)
        if needed <= width:
            break
    quantities, symbols, wheres, money = columns
    wq, ws, ww, wm = widths
    lines = [f"{q} {sym:<{ws}} {where:<{ww}} {m:>{wm}}"
             for q, sym, where, m in zip(quantities, symbols, wheres, money)]
    # The folded lines start at the left edge: padding them into the quantity
    # column buys nothing and makes the longest line in the digest.
    if dust:
        tail = f"+{len(dust)} under ${dust_usd:,.0f}"
        lines.append(f"{tail:<{wq + ws + ww + 2}} "
                     f"{_money(sum(r['usd'] for r in dust)):>{wm}}")
    if unpriced:
        lines.append(_clip(f"+{len(unpriced)} unpriced: {_names(unpriced)}", width))
    return lines


def _unit_price(rows: list[dict], group: str) -> float | None:
    """What one unit of the group's own coin costs, read off a holding of it.

    Only rows whose symbol *is* the group name qualify: wstETH is ETH-denominated
    but not one for one, so pricing the group by it would overstate the coins.
    """
    for row in sorted(rows, key=_sort_key, reverse=True):
        if row["symbol"] == group and row["usd"] and row["raw"] > 0:
            units = row["raw"] / 10 ** row["decimals"]
            if units:
                return row["usd"] / units
    return None


def _summary(groups: dict[str, list[dict]], total: float) -> list[str]:
    """One line per group: how much of the coin, how many dollars, what share.

    The coin quantity is derived from dollars, so it is an approximation for any
    group holding more than one flavour of the asset - marked with ~ for that
    reason. It answers "how much ETH do I have", which no single row does.
    """
    order = sorted(groups, key=lambda g: -sum(r["usd"] or 0 for r in groups[g]))
    cells = []
    for name in order:
        rows = groups[name]
        worth = sum(r["usd"] or 0 for r in rows)
        price = _unit_price(rows, name)
        # A dollar group's coin count is its dollar figure printed twice.
        stable = price is not None and abs(price - 1.0) < 0.02
        qty = f"~{worth / price:,.4f} {name}" if price and not stable else ""
        share = f"{worth / total * 100:.0f}%" if total else "—"
        cells.append((name, qty, f"${worth:,.0f}", share))
    wn = max((len(c[0]) for c in cells), default=0)
    wq = max((len(c[1]) for c in cells), default=0)
    wv = max((len(c[2]) for c in cells), default=0)
    return [f"{n:<{wn}}  {q:>{wq}}  {v:>{wv}} {s:>4}" for n, q, v, s in cells]


def _split_groups(rows: list[dict], total: float) -> dict[str, list[dict]]:
    """Group the holdings, folding the slivers into "other".

    Merging needs at least two small groups: renaming a single one to "other"
    hides its name and saves nothing.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    small = [g for g, rs in groups.items()
             if total > 0 and sum(r["usd"] or 0 for r in rs) < total * OTHER_SHARE]
    if len(small) > 1:
        other = [r for g in small for r in groups.pop(g)]
        groups["other"] = other
    return groups


def _last24h(recent: list[dict], width: int = TABLE_WIDTH) -> tuple[str, list[str]]:
    """The day in one line, with the detail behind it.

    Accruals are aggregated per asset: the vaults produce a row every tick, and
    thirty-two lines of "vault yield: 0" say less than four lines of totals.
    """
    transfers = [e for e in recent if e["kind"] == EventKind.TRANSFER.value]
    accruals = [e for e in recent if e["kind"] == EventKind.ACCRUAL.value]
    changes = [e for e in recent if e["kind"] == EventKind.POSITION_CHANGE.value]
    anomalies = [e for e in recent if e["kind"] == EventKind.ANOMALY.value]

    parts = []
    if transfers:
        # Internal transfers are excluded, not signed: a move between two of our
        # own wallets is recorded on both sides, and scoring "not in" as
        # negative counted a $10k shuffle as net -$20,000. Marking a transfer
        # internal exists precisely so it does not read as money moving.
        external = [e for e in transfers
                    if e["direction"] != Direction.INTERNAL.value]
        net = sum((e["usd"] or 0) * (1 if e["direction"] == "in" else -1)
                  for e in external)
        moved = f"{len(transfers)} transfers"
        if len(external) != len(transfers):
            moved += f" ({len(transfers) - len(external)} internal)"
        parts.append(f"{moved}, net ${net:+,.2f}")
    else:
        parts.append("no transfers")

    totals: dict[tuple, list] = {}
    for e in accruals:
        key = (e["detail"] or "accrued", e["symbol"] or "", e["decimals"] or 8)
        entry = totals.setdefault(key, [0, 0.0])
        entry[0] += int(e["amount_raw"] or 0)
        entry[1] += e["usd"] or 0.0
    earned = {k: v for k, v in totals.items() if v[0] > 0}
    if earned:
        parts.append(f"yield in {len(earned)} asset{'s' if len(earned) > 1 else ''}")
    if changes:
        parts.append(f"{len(changes)} position change{'s' if len(changes) > 1 else ''}")
    if anomalies:
        parts.append(f"{len(anomalies)} unexplained")

    # A table, not sentences: five lines of "vault yield" down the left edge
    # read as noise, while five amounts under one another read as a day.
    # (symbol, amount, what it was, tail, whether the tail may be cut). The
    # amount stays raw: how many digits of it survive is decided by _fit, which
    # is the only place that knows what is left of the line.
    cells = []
    for (what, symbol, decimals), (raw, usd) in sorted(
            earned.items(), key=lambda kv: -kv[1][1]):
        cells.append((symbol, raw, decimals, what.split()[0] if what else "yield",
                      f"${usd:,.2f}" if usd else "", False))
    for e in anomalies:
        # A label may be shortened; a dollar figure may not.
        cells.append((e["symbol"] or "", int(e["amount_raw"] or 0),
                      e["decimals"] or 8, "anomaly", e["label"], True))
    if not cells:
        return ", ".join(parts), []
    return ", ".join(parts), _fit(cells, width)


def _fit(cells: list[tuple], width: int) -> list[str]:
    """The 24h table, narrowed by the same ladder as the holdings table.

    It sits in the same kind of block - a <pre> behind a tap - so it folds at
    the same place, and an anomaly carrying a long label is exactly the row
    that would find that edge on the one morning it matters.
    """
    for sig, symbol_width, label_width in _plans():
        symbols = [_clip(sym, symbol_width) for sym, _, _, _, _, _ in cells]
        amounts = _align([_amount(raw, dec, sig) for _, raw, dec, _, _, _ in cells])
        kinds = [kind for _, _, _, kind, _, _ in cells]
        tails = [_short(tail, label_width) if cut else tail
                 for _, _, _, _, tail, cut in cells]
        widths = [max(len(c) for c in col)
                  for col in (symbols, amounts, kinds, tails)]
        if sum(widths) + 3 <= width:
            break
    ws, _wa, wk, _wt = widths
    return [f"{sym:<{ws}} {amount} {kind:<{wk}} {tail}".rstrip()
            for sym, amount, kind, tail in zip(symbols, amounts, kinds, tails)]


def _header(conn, data: dict, since_digest: bool) -> list[str]:
    """Total, and what it is being compared with.

    The digest compares against the last digest; the portfolio view compares
    against nothing and says instead how old the balances are, because prices
    are live while quantities are only as fresh as the last scan.
    """
    line = f"${data['total']:,.2f}"
    if since_digest:
        previous = dbmod.get_meta(conn, LAST_TOTAL)
        if previous is not None:
            try:
                before = float(previous)
                delta = data["total"] - before
                pct = (delta / before * 100) if before else 0.0
                line += f"   {delta:+,.2f} ({pct:+.2f}% since last digest)"
            except ValueError:
                pass
    lines = [line]
    if not since_digest and data["as_of"]:
        lines.append(f"balances as of {data['as_of']}, prices now")
    if data["unpriced"]:
        lines.append(f"({data['unpriced']} holdings have no price and are not counted)")
    return lines


def _instruments(rows: list[dict], total: float) -> Block | None:
    """The one table that does mix venues: how much of each asset in total."""
    groups = _split_groups(rows, total)
    return Block(lines=_summary(groups, total)) if groups else None


def _venue_blocks(rows: list[dict], total: float, dust_usd: float,
                  width: int = TABLE_WIDTH) -> list[Block]:
    """Detail, split by where the money sits.

    On-chain holdings and a Hyperliquid balance are different kinds of thing -
    one is in a wallet, the other is a claim on an exchange - and a table that
    interleaves them invites reading an exchange balance as a holding. Within
    on-chain the split is by instrument; within an exchange it is by account,
    which is how an exchange is actually operated.
    """
    blocks = []
    for venue in sorted({r["venue"] for r in rows},
                        key=lambda v: (v != ONCHAIN, v)):
        here = [r for r in rows if r["venue"] == venue]
        worth = sum(r["usd"] or 0 for r in here)
        share = f"{worth / total * 100:.0f}%" if total else "—"
        title = f"{venue.upper()}  ${worth:,.0f}  {share}"
        if venue == ONCHAIN:
            groups = _split_groups(here, worth)
            blocks.append(Block(title=title, lines=[], mono=False))
            for name in sorted(groups, key=lambda g: -sum(r["usd"] or 0 for r in groups[g])):
                inside = sum(r["usd"] or 0 for r in groups[name])
                blocks.append(Block(title=f"· {name}  ${inside:,.0f}",
                                    lines=_table(groups[name], dust_usd, width),
                                    collapsed=True))
            continue
        accounts: dict[str, float] = {}
        for row in here:
            accounts[row["label"]] = accounts.get(row["label"], 0.0) + (row["usd"] or 0)
        # One line per account only when that is fewer lines than the detail.
        # Eight accounts holding nine balances is the same table twice.
        detail = _table(here, dust_usd, width)
        if len(accounts) * 2 <= len(detail):
            order = sorted(accounts, key=lambda a: -accounts[a])
            wa = max(len(a) for a in order)
            money = {a: _money(v) for a, v in accounts.items()}
            wv = max(len(m) for m in money.values())
            blocks.append(Block(title=title,
                                lines=[f"{a:<{wa}} {money[a]:>{wv}}" for a in order]))
            blocks.append(Block(lines=detail, collapsed=True))
        else:
            blocks.append(Block(title=title, lines=detail))
    return blocks


def render_state(conn, data: dict, cfg=None) -> Message:
    """The portfolio as it stands: everything held, nothing about the day."""
    settings = getattr(cfg, "digest", None)
    dust_usd = getattr(settings, "dust_usd", DUST_USD)
    width = getattr(settings, "table_width", TABLE_WIDTH)
    rows = _holdings(data, cfg)
    blocks = [Block(lines=_header(conn, data, since_digest=False), mono=False)]
    instruments = _instruments(rows, data["total"])
    if instruments:
        blocks.append(instruments)
    blocks.extend(_venue_blocks(rows, data["total"], dust_usd, width))
    blocks.extend(_perp_blocks(data))
    return Message(title="Portfolio", body=flatten(blocks), blocks=blocks,
                   severity=Severity.LOW, kind=EventKind.SERVICE)


def _perp_blocks(data: dict) -> list[Block]:
    """Perps are exposure, not holdings, so they never join a total."""
    perps = [p for p in data["positions"] if p["position_key"].startswith("perp:")]
    if not perps:
        return []
    lines = []
    for row in perps:
        extra = row["extra"]
        size = _amount(int(row["amount_raw"]), _scale(row))
        risk = extra.get("liq_distance_pct")
        tail = f"  liq {risk}% away" if risk else ""
        # PnL, not notional, is the part that counts as money - say both, so the
        # number in the total can be found in the table it came from.
        try:
            pnl = f"  pnl {float(extra.get('unrealized_pnl') or 0):+,.2f}"
        except (TypeError, ValueError):
            pnl = ""
        lines.append(f"{_short(row['label'])} {extra.get('side', ''):<5} {size} "
                     f"{row['position_key'].replace('perp:', '')}"
                     f"  ${_money(row['usd'] or 0)}{pnl}{tail}")
    return [Block(title="PERPS (exposure, not held)", lines=lines)]


def _perp_line(data: dict) -> str:
    """Open perps in one line, including the nearest liquidation.

    The full table lives in `pw portfolio`, but a position drifting toward
    liquidation must not be invisible in the one message that is read daily.
    """
    perps = [p for p in data["positions"] if p["position_key"].startswith("perp:")]
    if not perps:
        return ""
    risks = [float(p["extra"].get("liq_distance_pct") or 0) for p in perps]
    risks = [r for r in risks if r]
    closest = f", closest liq {min(risks):.2f}% away" if risks else ""
    return f"perps: {len(perps)} open{closest}"


def render(conn, data: dict, cfg=None) -> Message:
    """The daily digest: what it is all worth, and what happened since yesterday.

    Deliberately short. The full breakdown lives in `pw portfolio`, which can be
    asked for at any time; a morning message that has to be scrolled to reach
    the part about the day defeats the point of having one.
    """
    width = getattr(getattr(cfg, "digest", None), "table_width", TABLE_WIDTH)
    rows = _holdings(data, cfg)
    blocks = [Block(lines=_header(conn, data, since_digest=True), mono=False)]
    instruments = _instruments(rows, data["total"])
    if instruments:
        blocks.append(instruments)

    summary, detail = _last24h(data["recent"], width)
    perps = _perp_line(data)
    blocks.append(Block(lines=[f"24h: {summary}"] + ([perps] if perps else []),
                        mono=False))
    if detail:
        blocks.append(Block(lines=detail, collapsed=True))
    return Message(title="Daily digest", body=flatten(blocks), blocks=blocks,
                   severity=Severity.LOW, kind=EventKind.SERVICE)


async def send(cfg, conn, router, prices=None) -> tuple[Message, dict]:
    """Send the daily digest and record what tomorrow measures against.

    Returns the message and the per-channel results. The baseline is written
    only when a channel actually took it: router.send reports failures by
    returning them rather than raising, so writing unconditionally meant a
    Telegram outage at 9am both lost that day's digest and moved the baseline
    tomorrow's "change since yesterday" is measured from. Leaving LAST_DATE
    unset also makes due() stay true, so the next tick simply tries again.
    """
    data = collect(conn, prices)
    message = render(conn, data, cfg)
    results = await router.send(message)
    failed = {ch: err for ch, err in results.items() if err}
    if failed or not results:
        log.warning("digest not recorded, nothing delivered: %s",
                    failed or "no channels")
        return message, results
    conn.execute("BEGIN")
    try:
        dbmod.set_meta(conn, LAST_TOTAL, f"{data['total']:.2f}")
        dbmod.set_meta(conn, LAST_DATE, date.today().isoformat())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return message, results


def due(cfg, conn, now: datetime | None = None) -> bool:
    """True once per day, after the configured hour."""
    if not cfg.digest.enabled:
        return False
    now = now or datetime.now()
    if now.hour < cfg.digest.hour:
        return False
    return dbmod.get_meta(conn, LAST_DATE) != now.date().isoformat()
