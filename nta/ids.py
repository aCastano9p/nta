"""ULID generation (stdlib only): 48-bit ms timestamp + 80 random bits, Crockford base32.

Monotonic within the process: IDs generated in the same millisecond increment the random
part, so id order equals generation order. Code that replays rows "in insertion order"
(e.g. allowlist membership) relies on this.
"""

import os
import threading

from nta.clock import epoch_ms

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RAND_MAX = (1 << 80) - 1

_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(now_ms: int | None = None) -> str:
    global _last_ms, _last_rand
    ms = epoch_ms() if now_ms is None else now_ms
    with _lock:
        if ms <= _last_ms:
            ms = _last_ms
            rand = _last_rand + 1
            if rand > _RAND_MAX:  # astronomically unlikely; borrow the next millisecond
                ms, rand = ms + 1, int.from_bytes(os.urandom(10), "big")
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last_ms, _last_rand = ms, rand
    return _encode((ms << 80) | rand)
