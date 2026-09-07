"""Incoming Telegram commands: the watcher answering questions.

Long polling rather than a webhook. A webhook needs a public HTTPS endpoint,
which a machine behind a home NAT does not have; `getUpdates` needs nothing but
an outgoing connection, and one request sits open for half a minute instead of
costing anything while it waits.

Everything here reads. No command moves money and none ever will - the project
holds no keys (PLAN §12). The one expensive command is /scan, which buys a full
tick, so it shares the watch loop's lock and is rate limited.

The offset lives in `meta`. Without it a restart replays every command Telegram
still holds, and the answer to "what happened while you were down" is not a
week-old /scan running again.
"""

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import db as dbmod
from . import digest
from . import pipeline
from .models import Block, EventKind, Message, Severity, flatten
from .notify.telegram import TelegramNotifier, api, send_text
from .retry import Permanent

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram:offset"
LAST_TICK = "watch:last_tick"
"""Written by the watch loop after every successful tick; /health reads it."""

POLL_TIMEOUT = 30
"""Seconds Telegram holds the request open when there is nothing to say."""
BACKOFF = 5.0
"""Pause after the first failed poll. Doubles up to BACKOFF_CAP while Telegram
stays unreachable: a 502 comes back instantly, so a flat pause asks a dead
Telegram twelve times a minute for as long as it is dead."""
BACKOFF_CAP = 60.0
LOUD_AFTER = 5
"""Consecutive failed polls before the log stops calling it a hiccup."""
STALE_SECONDS = 300
"""A command older than this is not answered. The bot may have been down for a
day; a question asked yesterday is not a question now, and replying to it looks
like the bot talking to itself."""

NOT_MODIFIED = "message is not modified"
"""Telegram's wording for "the edit would change nothing". It arrives as a 400,
which this code otherwise reads as Permanent, so it has to be told apart from
the refusals that really do owe the chat a message."""
MIN_SCAN_GAP = 60.0

HELP = [
    ("portfolio", "what is held now, at today's prices"),
    ("digest", "the daily summary on demand (records nothing)"),
    ("status", "what the database holds: addresses, events, queue"),
    ("health", "is the watcher alive: last tick, stale cursors"),
    ("scan", "force a tick (expensive; alerts arrive separately)"),
    ("help", "this list"),
]

ALIAS = {"start": "help"}
"""Telegram sends /start on the first ever message; it means "what is this"."""


def _backoff(failures: int) -> float:
    """How long to wait before asking Telegram again, after `failures` in a row.

    Exponential, unlike `retry.with_retry`'s full jitter, and for the opposite
    reason: jitter is there to keep several addresses from retrying a provider
    in lockstep, and there is exactly one poll loop. Spreading the pause
    uniformly from zero would only undo the point of having one. The remaining
    jitter is the small kind, so the retries do not line up with whatever
    upstream schedule caused the outage.
    """
    delay = min(BACKOFF_CAP, BACKOFF * 2 ** (failures - 1))
    return delay * random.uniform(0.5, 1.0)


def _log_poll_failure(failures: int, exc: Exception) -> None:
    """Log a failed poll at the volume it has earned.

    Telegram answers 502 often enough, and holds a long poll open past its own
    timeout often enough, that a traceback per occurrence teaches the reader to
    skip them - and then the failure that matters, a token revoked or a host
    with no route out, looks exactly like the noise it is buried in. So a
    hiccup is one line, and only an outage that outlives LOUD_AFTER polls is
    worth a traceback. Past that it stays loud but stops repeating the stack:
    the backoff has already capped, and an hour of downtime should read as an
    hour of downtime, not as an hour of crashes.
    """
    if failures < LOUD_AFTER:
        log.warning("poll failed (%d): %s", failures, exc)
    elif failures == LOUD_AFTER:
        log.error("telegram unreachable, %d polls failed", failures, exc_info=exc)
    else:
        log.error("telegram unreachable, %d polls failed: %s", failures, exc)


LABEL = {"portfolio": "Portfolio", "digest": "Digest", "status": "Status",
         "health": "Health", "scan": "Scan", "help": "Help"}

BUTTONS = {
    "portfolio": ["portfolio", "digest", "scan"],
    "digest": ["digest", "portfolio"],
    "status": ["status", "health"],
    "health": ["health", "scan", "status"],
    "scan": ["portfolio", "health"],
    "help": ["portfolio", "digest", "status", "health"],
}
"""What to offer under each answer; the first entry repeats the command itself
and is drawn as Refresh.

Inline rather than a reply keyboard: a reply keyboard is a permanent strip
above the phone's own, which is the wrong price for six commands asked a few
times a day. These live under the message, scroll away with it, and a refresh
edits that message in place - which is what Telegram's own guidance asks for.
The command list (setMyCommands) stays the canonical way in."""


def markup(command: str) -> dict | None:
    names = BUTTONS.get(command)
    if not names:
        return None
    row = [{"text": "\U0001f504 Refresh" if n == command else LABEL[n],
            "callback_data": f"cmd:{n}"} for n in names]
    return {"inline_keyboard": [row[i:i + 3] for i in range(0, len(row), 3)]}


def _ago(ts: str | None) -> str:
    """'4 min ago' for an ISO timestamp, or a plain dash.

    A silent portfolio and a watcher that died three days ago look identical in
    a table of balances; the difference is always this number.
    """
    if not ts:
        return "never"
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - when).total_seconds()
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    if secs < 172800:
        return f"{secs / 3600:.1f}h ago"
    return f"{int(secs // 86400)}d ago"


def status_message(conn) -> Message:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    pending = q("SELECT COUNT(*) FROM notifications WHERE status='pending'")
    failed = q("SELECT COUNT(*) FROM notifications WHERE status='failed'")
    lines = [
        f"addresses   {q('SELECT COUNT(*) FROM addresses WHERE enabled=1')} enabled",
        f"assets      {q('SELECT COUNT(*) FROM assets')}",
        f"events      {q('SELECT COUNT(*) FROM events')}",
        f"notify      {pending} pending, {failed} failed",
    ]
    blocks = [Block(lines=lines)]
    recent = [dict(r) for r in conn.execute(
        "SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT 10").fetchall()]
    if recent:
        blocks.append(Block(title="recent", collapsed=True, lines=[
            f"{r['ts'][5:16]}  {r['kind']:<16} {(r['detail'] or '')[:40]}" for r in recent]))
    return Message(title="Status", body=flatten(blocks), blocks=blocks,
                   severity=Severity.LOW, kind=EventKind.SERVICE)


def health_message(cfg, conn) -> Message:
    """Is the watcher actually working, and which source went quiet.

    `fail_count` and `last_ok_at` are already kept per cursor; nothing reads
    them until something breaks, and by then nobody is at a terminal.
    """
    last_tick = dbmod.get_meta(conn, LAST_TICK)
    sent_today = dbmod.get_meta(conn, digest.LAST_DATE)
    lines = [
        f"last tick   {_ago(last_tick)}",
        f"interval    {cfg.interval_minutes} min",
        f"digest      {'sent today' if sent_today == datetime.now().date().isoformat() else 'not sent today'}"
        f", hour {cfg.digest.hour}",
    ]
    pending = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE status='pending'").fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE status='failed'").fetchone()[0]
    lines.append(f"notify      {pending} pending, {failed} failed")
    blocks = [Block(lines=lines)]

    # Three missed intervals: one slow provider is normal, three in a row is not.
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=3 * cfg.interval_minutes)).isoformat()
    stale = [dict(r) for r in conn.execute(
        """SELECT a.label, c.chain, c.scope, c.last_ok_at, c.fail_count
             FROM cursors c JOIN addresses a ON a.id = c.address_id
            WHERE a.enabled = 1 AND (c.last_ok_at IS NULL OR c.last_ok_at < ?)
         ORDER BY c.last_ok_at""", (cutoff,)).fetchall()]
    if stale:
        blocks.append(Block(title=f"stale ({len(stale)})", lines=[
            f"{r['label']:<12} {(r['scope'] or r['chain']):<10} "
            f"{_ago(r['last_ok_at']):>10}  fail {r['fail_count']}" for r in stale]))
    else:
        blocks.append(Block(lines=["all cursors fresh"], mono=False))
    return Message(title="Health", body=flatten(blocks), blocks=blocks,
                   severity=Severity.LOW, kind=EventKind.SERVICE)


def help_message() -> Message:
    block = Block(lines=[f"/{name} — {what}" for name, what in HELP], mono=False)
    return Message(title="Commands", body=flatten([block]), blocks=[block],
                   severity=Severity.LOW, kind=EventKind.SERVICE)


class CommandBot:
    """Reads commands from one chat and answers them. Nothing else.

    `scan` is the watch loop's own tick, passed in rather than built here: two
    scans running at once against the same database would double every fetch,
    so /scan must be the same serialized call the schedule uses.
    """

    def __init__(self, cfg, conn, prices, *, scan=None, client=None,
                 poll_timeout: int = POLL_TIMEOUT):
        self._cfg = cfg
        self._conn = conn
        self._prices = prices
        self._scan = scan
        self._token = cfg.notify.telegram.bot_token
        self._chat = str(cfg.notify.telegram.chat_id)
        self._poll_timeout = poll_timeout
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(poll_timeout + 15))
        self._owns_client = client is None
        self._render = TelegramNotifier(self._token, self._chat)
        self._offset: int | None = None
        self._last_scan: float | None = None
        """None, not 0.0: time.monotonic() counts from the start of the
        process inside a container, so a zero marker is indistinguishable from
        "scanned a moment ago" for the first minute of every restart."""
        self._handlers = {
            "portfolio": self._portfolio,
            "digest": self._digest,
            "status": lambda: status_message(self._conn),
            "health": lambda: health_message(self._cfg, self._conn),
            "scan": self._scan_now,
            "help": help_message,
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- transport ---------------------------------------------------------

    async def _resume(self) -> None:
        """Pick up where the last run stopped, or skip the backlog entirely.

        A cold start with no stored offset asks for the last update only and
        throws it away: whatever accumulated while the bot did not exist is
        history, not a queue of work.
        """
        stored = dbmod.get_meta(self._conn, OFFSET_KEY)
        if stored:
            self._offset = int(stored)
            return
        result = (await api(self._client, self._token, "getUpdates",
                            {"offset": -1, "timeout": 0})).get("result", [])
        self._offset = (result[-1]["update_id"] + 1) if result else None
        if self._offset:
            dbmod.set_meta(self._conn, OFFSET_KEY, str(self._offset))
            log.info("skipping %d update(s) from before this run", len(result))

    async def _updates(self) -> list[dict]:
        payload = {"timeout": self._poll_timeout,
                   "allowed_updates": ["message", "callback_query"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        return (await api(self._client, self._token, "getUpdates", payload)).get("result", [])

    async def _reply(self, chat_id: str, msg: Message, command: str = "help",
                     edit: int | None = None) -> str | None:
        """Send the answer, or rewrite the message the button hangs under.

        Editing needs the whole answer to be one message; a digest long enough
        to be split cannot replace a single one, so it arrives fresh instead.

        Returns what the button's toast should say, or None when the answer is
        already on screen.
        """
        texts = self._render.texts(msg)
        buttons = markup(command)
        if edit is not None and len(texts) == 1:
            try:
                await api(self._client, self._token, "editMessageText",
                          {"chat_id": chat_id, "message_id": edit, "text": texts[0],
                           "parse_mode": "HTML", "disable_web_page_preview": True,
                           "reply_markup": buttons})
                return None
            except Permanent as e:
                # A refused edit must not look like a broken token. Which
                # refusal it is decides the answer, though:
                if NOT_MODIFIED in str(e):
                    # Telegram says the text is identical, i.e. nothing moved
                    # since the last tap. A second copy of the same numbers is
                    # the worst possible answer to "anything new?" - say so on
                    # the button and leave the chat untouched.
                    log.info("nothing changed since the last tap")
                    return "No change"
                # Anything else (too old to edit, gone) still owes an answer.
                log.info("edit declined, sending instead: %s", e)
        for i, text in enumerate(texts):
            # The buttons go on the last part only: attaching them to each
            # piece of a split digest draws three rows of them.
            await send_text(self._client, self._token, chat_id, text,
                            buttons if i == len(texts) - 1 else None)
        return None

    async def _announce(self) -> None:
        """Register the command menu. Cosmetic, so a failure is not fatal."""
        try:
            await api(self._client, self._token, "setMyCommands",
                      {"commands": [{"command": n, "description": d} for n, d in HELP]})
        except Exception as e:  # noqa: BLE001
            log.warning("setMyCommands failed: %s", e)

    async def run(self) -> None:
        """Poll forever. Never raises into the watch loop.

        A bad token disables commands and leaves the watcher watching: losing
        the ability to ask questions must not cost the alerts.
        """
        failures = 0
        while True:
            # Startup must be no more fragile than the loop: `_resume` is a
            # getUpdates like any other, and a 502 answering it used to end the
            # task outright - commands gone until someone restarted the
            # process, over the same hiccup the loop shrugs off.
            try:
                await self._resume()
                await self._announce()
                break
            except asyncio.CancelledError:
                raise
            except Permanent as e:
                log.error("telegram commands off: %s", e)
                return
            except Exception as e:  # noqa: BLE001
                failures += 1
                _log_poll_failure(failures, e)
                await asyncio.sleep(_backoff(failures))
        log.info("answering commands in chat %s", self._chat)
        failures = 0
        while True:
            try:
                for update in await self._updates():
                    # Confirm before handling: a command that crashes the
                    # handler must not be replayed on every restart.
                    self._offset = update["update_id"] + 1
                    dbmod.set_meta(self._conn, OFFSET_KEY, str(self._offset))
                    try:
                        await self.handle(update)
                    except Exception:  # noqa: BLE001
                        # Including a 400 from sendMessage: one unanswerable
                        # update must not end the conversation.
                        log.exception("update %d failed", update["update_id"])
                if failures >= LOUD_AFTER:
                    # Only worth saying when the silence was loud enough to
                    # have worried somebody reading the log.
                    log.info("telegram back after %d failed poll(s)", failures)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Permanent as e:
                log.error("telegram commands off: %s", e)
                return
            except Exception as e:  # noqa: BLE001
                failures += 1
                _log_poll_failure(failures, e)
                await asyncio.sleep(_backoff(failures))

    # ---- dispatch ----------------------------------------------------------

    async def handle(self, update: dict) -> str | None:
        """Answer one update. Returns the command handled, for tests."""
        if "callback_query" in update:
            return await self._tap(update["callback_query"])
        msg = update.get("message") or update.get("edited_message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        if chat != self._chat:
            # Anyone who finds the bot's name can write to it.
            log.debug("ignoring message from chat %s", chat)
            return None
        sent = msg.get("date")
        if sent and time.time() - sent > STALE_SECONDS:
            log.info("ignoring a command %ds old", int(time.time() - sent))
            return None
        text = (msg.get("text") or "").strip()
        # "/portfolio@my_bot arg" - the suffix appears whenever the chat is a group.
        name = text.split()[0].lstrip("/").split("@")[0].lower() if text.startswith("/") else ""
        name = ALIAS.get(name, name)
        if name not in self._handlers:
            await self._reply(chat, help_message(), "help")
            return None
        await self._reply(chat, await self._run(name), name)
        return name

    async def _tap(self, query: dict) -> str | None:
        """A button press: same commands, answered by rewriting the message.

        No staleness check here, unlike a typed command. The date on the
        message a button hangs under is when the *bot* wrote it, and tapping
        Refresh on yesterday's portfolio is a question asked now.
        """
        message = query.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        name = str(query.get("data", "")).removeprefix("cmd:")
        if chat != self._chat or name not in self._handlers:
            await self._ack(query.get("id"))
            return None
        note = ""
        try:
            answer = await self._run(name)
            note = await self._reply(chat, answer, name,
                                     edit=message.get("message_id")) or ""
        finally:
            # Unacknowledged, the button spins until Telegram gives up on it.
            # The note is the only answer when the message did not change.
            await self._ack(query.get("id"), note)
        return name

    async def _ack(self, query_id, text: str = "") -> None:
        if not query_id:
            return
        try:
            await api(self._client, self._token, "answerCallbackQuery",
                      {"callback_query_id": query_id, "text": text})
        except Exception as e:  # noqa: BLE001
            log.debug("answerCallbackQuery failed: %s", e)

    async def _run(self, name: str) -> Message:
        """The command itself. A failure is an answer, never an exception:
        a broken price source must not cost the whole command channel."""
        try:
            result = self._handlers[name]()
            return await result if asyncio.iscoroutine(result) else result
        except Exception as e:  # noqa: BLE001
            log.exception("/%s failed", name)
            return Message(title="Failed", body=f"/{name}: {e}",
                           severity=Severity.NORMAL, kind=EventKind.SERVICE)

    # ---- commands ----------------------------------------------------------

    async def _fresh_prices(self) -> None:
        """Prices at the command TTL, not the loop's: PricesCfg says why."""
        if self._prices is not None:
            await self._prices.refresh(
                pipeline.priceable_assets(self._conn),
                ttl_minutes=self._cfg.prices.command_ttl_minutes)

    async def _portfolio(self) -> Message:
        await self._fresh_prices()
        return digest.render_state(self._conn, digest.collect(self._conn, self._prices),
                                   self._cfg)

    async def _digest(self) -> Message:
        """The daily message on demand - rendered, never recorded.

        Recording would move the baseline the morning digest measures the day
        against, so asking for it at lunch would silently shrink tomorrow's
        "change since yesterday".
        """
        await self._fresh_prices()
        return digest.render(self._conn, digest.collect(self._conn, self._prices),
                             self._cfg)

    async def _scan_now(self) -> Message:
        if self._scan is None:
            return Message(title="Scan", body="the watch loop is not running - nothing to scan",
                           severity=Severity.NORMAL, kind=EventKind.SERVICE)
        if self._last_scan is not None:
            waited = time.monotonic() - self._last_scan
            if waited < MIN_SCAN_GAP:
                return Message(title="Scan",
                               body=f"just scanned; try again in {MIN_SCAN_GAP - waited:.0f}s",
                               severity=Severity.LOW, kind=EventKind.SERVICE)
        self._last_scan = time.monotonic()
        res = await self._scan()
        body = (f"probed {res.probed}, changed {res.changed}, "
                f"new {res.new_events}, confirmed {res.confirmed}")
        if res.failed:
            body += "\nfailed: " + ", ".join(res.failed[:5])
        return Message(title="Scan", body=body,
                       severity=Severity.LOW, kind=EventKind.SERVICE)
