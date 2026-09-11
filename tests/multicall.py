"""A fake Multicall3, for the transports that answer eth_call by (to, calldata).

Every mock transport in this suite serves one call at a time out of a table.
Aggregation puts all of them inside a single call to one address, so without
this the tables stop matching, the adapter falls back to plain calls, and the
tests that count calls go on passing while covering the path that was replaced.

Decoding what `encode_aggregate3` wrote is also the only check on the encoder
that does not restate it: this reads the offsets back independently, so a head
word pointing one word off dispatches the wrong calldata and the test that
reads a position fails rather than quietly approving the payload. The live
cross-check against four chains is recorded at `EvmAdapter.MULTICALL3`.
"""

WORD = 32


def _word(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + WORD], "big")


def subcalls(data: str) -> list[tuple[str, str]]:
    """The (to, calldata) pairs inside an aggregate3 payload."""
    b = bytes.fromhex(data[10:])          # past "0x" and the selector
    head = _word(b, 0)
    count = _word(b, head)
    base = head + WORD
    out = []
    for i in range(count):
        item = base + _word(b, base + i * WORD)
        to = f"0x{_word(b, item):040x}"
        at = item + _word(b, item + WORD * 2)
        length = _word(b, at)
        out.append((to, "0x" + b[at + WORD:at + WORD + length].hex()))
    return out


def results(answers: list[tuple[bool, str | None]]) -> str:
    """Encode `(bool success, bytes returnData)[]`, as the contract answers."""
    items = []
    for ok, payload in answers:
        raw = bytes.fromhex((payload or "0x")[2:])
        items.append(f"{1 if ok else 0:064x}"
                     + f"{WORD * 2:064x}"
                     + f"{len(raw):064x}"
                     + raw.hex()
                     + "00" * (-len(raw) % WORD))
    heads, at = [], WORD * len(items)
    for item in items:
        heads.append(f"{at:064x}")
        at += len(item) // 2
    return ("0x" + f"{WORD:064x}" + f"{len(items):064x}"
            + "".join(heads) + "".join(items))


def answer(table: dict, data: str, seen: list | None = None,
           sender=None) -> str:
    """Serve an aggregate3 payload from a (to, calldata) -> hex table.

    Each subcall is recorded in `seen` exactly as the plain path records it, so
    assertions that count what was asked keep their meaning. A pair the table
    does not hold comes back as a failed call, which is what a revert is.
    """
    out = []
    for to, calldata in subcalls(data):
        if seen is not None:
            seen.append((to, calldata, sender))
        got = table.get((to, calldata))
        out.append((got is not None, got))
    return results(out)
