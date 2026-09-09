"""Telegram Bot API sendMessage.

HTML parse mode, not MarkdownV2: token symbols and addresses are full of
characters MarkdownV2 requires escaping, and one missed escape rejects the whole
message. HTML needs only three characters escaped.

Blocks become tables: `<pre>` for the monospace font the aligned columns need,
wrapped in `<blockquote expandable>` when the block is bulk the reader should be
able to skip. Both entities survive on the same range - Telegram stores
`expandable_blockquote` and `pre` side by side - so the table keeps its
alignment inside the collapsed section.
"""

import html
import logging

import httpx

from ..models import Block, Message, Severity
from ..retry import Permanent, with_retry

log = logging.getLogger(__name__)

ICON = {Severity.LOW: "·", Severity.NORMAL: "•", Severity.HIGH: "⚠"}
API = "https://api.telegram.org"
LIMIT = 3800
"""Telegram rejects anything past 4096 characters. The margin covers the tags,
which are counted, and keeps a table from being split mid-row."""


def _render(block: Block) -> str:
    body = "\n".join(html.escape(line) for line in block.lines)
    if body and block.mono:
        body = f"<pre>{body}</pre>"
    if body and block.collapsed:
        body = f"<blockquote expandable>{body}</blockquote>"
    if block.title:
        head = f"<b>{html.escape(block.title)}</b>"
        return f"{head}\n{body}" if body else head
    return body


def _split(block: Block, limit: int) -> list[Block]:
    """A block too long to send at all, cut into sendable pieces.

    Only the first piece keeps the title: repeating it would read as a second
    section rather than a continuation.
    """
    pieces, cur = [], []
    for line in block.lines:
        cur.append(line)
        if sum(len(x) + 1 for x in cur) > limit // 2:
            pieces.append(cur)
            cur = []
    if cur:
        pieces.append(cur)
    return [Block(lines=lines, title=block.title if i == 0 else "",
                  collapsed=block.collapsed, mono=block.mono)
            for i, lines in enumerate(pieces)] or [block]


def chunks(head: str, blocks: list[Block], limit: int = LIMIT) -> list[str]:
    """One message per chunk, split on block boundaries.

    A digest that has grown past the limit must arrive in two messages rather
    than be rejected whole - a summary nobody receives is worse than a long one.
    """
    out: list[str] = []
    cur = head
    for block in blocks:
        for piece in ([block] if len(_render(block)) <= limit else _split(block, limit)):
            text = _render(piece)
            if not text:
                continue
            if cur and len(cur) + len(text) + 2 > limit:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n{text}" if cur else text
    if cur:
        out.append(cur)
    return out


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0):
        self._token = bot_token
        self._chat = chat_id
        self._timeout = timeout

    def texts(self, msg: Message) -> list[str]:
        head = f"{ICON.get(msg.severity, '•')} <b>{html.escape(msg.title)}</b>"
        if msg.blocks:
            return chunks(head, msg.blocks)
        # A body-only Message goes through the same splitter. pipeline.render()
        # builds one from every pending event, and a backlog outgrows 4096
        # characters - at which point Telegram 400s, a 400 here is Permanent,
        # the rows stay pending, and the next tick sends the same message only
        # longer. Alerts then never arrive again.
        return chunks(head, [Block(lines=msg.body.split("\n"), mono=False)])

    async def send(self, msg: Message, chat_id: str | None = None) -> None:
        """Deliver msg. `chat_id` overrides the configured chat, which is how a
        reply goes back to whoever asked rather than to the alert channel.

        Each part is retried on its own. Retrying the whole message instead -
        which is what the router used to do - re-sends the parts that already
        arrived: one 500 on part two of three put the header and part one in the
        chat a second time, and still reported success. A part that fails every
        attempt raises, and the notification rows stay pending, so the next tick
        tries the message again; that repeats a delivered part at most once,
        against losing the alert entirely.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            parts = self.texts(msg)
            for i, text in enumerate(parts, 1):
                what = "telegram send" if len(parts) == 1 else f"telegram send {i}/{len(parts)}"
                await with_retry(
                    lambda text=text: send_text(client, self._token,
                                                chat_id or self._chat, text),
                    what=what)


async def api(client, token: str, method: str, payload: dict) -> dict:
    """One Bot API call, with Telegram's own reason in the error.

    Shared by the outgoing notifier and the incoming command bot: both need the
    same distinction between "retrying may help" and "asking again is pointless".
    """
    r = await client.post(f"{API}/bot{token}/{method}", json=payload)
    if r.status_code == 200:
        return r.json()
    # Telegram puts the actual reason in the body; the status alone is useless.
    detail = f"telegram {method} {r.status_code}: {r.text[:300]}"
    # 401 bad token, 400 bad chat_id: retrying asks the same question again.
    if 400 <= r.status_code < 500 and r.status_code != 429:
        raise Permanent(detail)
    raise RuntimeError(detail)


async def send_text(client, token: str, chat_id: str, text: str,
                    reply_markup: dict | None = None) -> dict:
    payload = {"chat_id": chat_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await api(client, token, "sendMessage", payload)
