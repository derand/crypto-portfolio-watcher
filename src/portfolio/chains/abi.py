"""The small part of ABI encoding this project actually needs.

Not web3.contract: every call here is one view method with a known shape, and
building a contract object per protocol per chain costs more than decoding four
return types by hand. The four are what the catalog asks for - an address, a
list of addresses, a list of (symbol, address) pairs, and an integer.

Everything returns None rather than raising on a shape it does not recognise.
A discovery sweep touches a dozen protocols on five chains; one contract that
answers something unexpected must not end the run, and a silent None is caught
by the caller that knows which protocol it asked.
"""

from eth_utils import keccak

WORD = 32


def selector(signature: str) -> str:
    """"getAllATokens()" -> "0x1937d1e5". Calldata for a method with no args."""
    return "0x" + keccak(text=signature)[:4].hex()


def encode_address(signature: str, address: str) -> str:
    """Calldata for a one-address method, e.g. balanceOf(address)."""
    return selector(signature) + f"{int(address, 16):064x}"


AGGREGATE3 = "aggregate3((address,bool,bytes)[])"
"""Multicall3's batching entry point, and the only method of it used here.

`aggregate` reverts the whole batch if any call fails and `tryAggregate` takes
one flag for all of them; `aggregate3` carries the flag per call, which is the
only variant that can keep `eth_call_many`'s promise - answers line up with the
calls, and a reverted one is None rather than the end of the batch.
"""


def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """Calldata asking Multicall3 for a list of (to, calldata), all at once.

    `allowFailure` is set on every call, so one contract without the method
    asked of it costs its own answer and nothing else.

    The struct carries `bytes`, which makes it dynamic, so the array is encoded
    as a count followed by one offset per element rather than as a flat run of
    words. Those offsets are relative to the start of the array's data - the
    word after the count - and not to the start of the payload, which is the
    mistake this function exists to make exactly once.
    """
    structs = []
    for to, data in calls:
        payload = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        structs.append(f"{int(to, 16):064x}"
                       + f"{1:064x}"              # allowFailure
                       + f"{WORD * 3:064x}"       # where the bytes start
                       + f"{len(payload):064x}"
                       + payload.hex()
                       + "00" * (-len(payload) % WORD))
    heads, at = [], WORD * len(structs)
    for struct in structs:
        heads.append(f"{at:064x}")
        at += len(struct) // 2
    return (selector(AGGREGATE3) + f"{WORD:064x}" + f"{len(structs):064x}"
            + "".join(heads) + "".join(structs))


def decode_aggregate3(data, count: int) -> list[str | None] | None:
    """The per-call answers, in the order they were asked.

    A call Multicall3 reports as failed comes back None, which is what a revert
    already means to every caller. `None` in place of the whole list means the
    answer was not the shape `aggregate3` returns at all - the wrong contract,
    or a network where it was never deployed - and is deliberately told apart
    from a list of Nones: the caller can retry the calls one batch at a time,
    where reading "every position answered nothing" as data would report a
    portfolio as closed.
    """
    b = _body(data)
    if b is None:
        return None
    try:
        head = _word(b, 0)
        if _counted(b, head) != count:
            return None
        base = head + WORD
        out: list[str | None] = []
        for i in range(count):
            item = base + _word(b, base + i * WORD)
            ok = _word(b, item)
            at = item + _word(b, item + WORD)
            length = _word(b, at)
            raw = b[at + WORD:at + WORD + length]
            if len(raw) != length:
                return None
            out.append("0x" + raw.hex() if ok else None)
        return out
    except (IndexError, ValueError):
        return None


def _body(data) -> bytes | None:
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 3:
        return None
    try:
        return bytes.fromhex(data[2:])
    except ValueError:
        return None


def _word(b: bytes, at: int) -> int:
    """One 32-byte word, or IndexError.

    Refusing is the point. Slicing a bytes object past its end is not an error
    in Python and int.from_bytes(b"") is 0, so reading out of range used to
    answer zero - which turned every payload of an unexpected shape into
    plausible data instead of a refusal: an offset pointing nowhere read as
    zero, a length word past the end read as "empty", and a truncated answer
    decoded to a clean, wrong result. Every caller below already turns this
    into None, which is what the module promises.
    """
    if at < 0 or at + WORD > len(b):
        raise IndexError(f"word at {at} lies outside a {len(b)}-byte payload")
    return int.from_bytes(b[at:at + WORD], "big")


def _counted(b: bytes, at: int) -> int:
    """The length word of a dynamic array, sized against what could hold it.

    An array claiming more entries than the payload has bytes is not an array;
    without this the loops below spun on a plausible-looking offset and a huge
    length word, appending zero addresses until memory ran out. That answered
    neither None nor an exception - the two things this module is allowed to do
    - and it was reachable from `catalog-check`, where a stale entry points at a
    contract that answers some other ABI entirely.
    """
    count = _word(b, at)
    if count * WORD > len(b):
        raise IndexError(f"{count} entries cannot fit in {len(b)} bytes")
    return count


def decode_uint(data) -> int | None:
    """First word as an integer.

    Deliberately the *first* word, not the whole payload: several Compound v2
    forks return `(uint error, uint value)` where the original returns one
    uint, and reading the concatenation as a single number gives an answer 10^60
    times too large - which looks like data rather than like a bug.
    """
    b = _body(data)
    if b is None or len(b) < WORD:
        return None
    return _word(b, 0)


def decode_address(data) -> str | None:
    b = _body(data)
    if b is None or len(b) < WORD:
        return None
    value = _word(b, 0)
    if value == 0:
        return None                      # a zero address is "not configured"
    return f"0x{value:040x}"


def decode_address_at(data, index: int) -> str | None:
    """One address out of a fixed-size tuple of them.

    Aave's getReserveTokensAddresses answers (aToken, stableDebtToken,
    variableDebtToken) as three plain words; the third is the only one that
    matters, and slicing it out beats decoding a struct.
    """
    b = _body(data)
    if b is None or len(b) < WORD * (index + 1):
        return None
    value = _word(b, index * WORD)
    return f"0x{value:040x}" if value else None


def decode_address_array(data) -> list[str] | None:
    """A dynamic address[], as Comptroller.getAllMarkets() returns."""
    b = _body(data)
    if b is None or len(b) < WORD:
        return None
    try:
        head = _word(b, 0)
        count = _counted(b, head)
        out = []
        for i in range(count):
            out.append(f"0x{_word(b, head + WORD + i * WORD):040x}")
        return out
    except (IndexError, ValueError):
        return None


def decode_string(data) -> str | None:
    """A single dynamic string, as symbol() returns on most tokens.

    Tokens that answer with a bytes32 instead (a handful of pre-2018 ERC-20s)
    decode to None here; the caller falls back to the address, which is honest
    about not knowing rather than printing mojibake.
    """
    b = _body(data)
    if b is None or len(b) < 2 * WORD:
        return None
    try:
        at = _word(b, 0)
        length = _word(b, at)
        raw = b[at + WORD:at + WORD + length]
        return raw.decode() if len(raw) == length else None
    except (IndexError, UnicodeDecodeError, ValueError):
        return None


def decode_symbol_address_array(data) -> list[tuple[str, str]] | None:
    """A dynamic array of struct { string symbol; address tokenAddress }.

    Aave's data provider answers getAllATokens() in this shape, which is the
    whole reason discovery needs no per-token catalog: one call names every
    receipt token a market has, symbol included, and stays right across the
    listings a market adds after this code was written.
    """
    b = _body(data)
    if b is None or len(b) < WORD:
        return None
    try:
        head = _word(b, 0)
        count = _counted(b, head)
        base = head + WORD
        out = []
        for i in range(count):
            item = base + _word(b, base + i * WORD)
            symbol_at = item + _word(b, item)
            length = _word(b, symbol_at)
            raw = b[symbol_at + WORD:symbol_at + WORD + length]
            if len(raw) != length:
                return None
            out.append((raw.decode(), f"0x{_word(b, item + WORD):040x}"))
        return out
    except (IndexError, UnicodeDecodeError, ValueError):
        return None
