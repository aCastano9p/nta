"""Graceful shutdown that is safe to trigger from a signal handler.

Signal handlers run on the main thread between bytecodes. Calling threading primitives
(Event.set, Lock.acquire) from one can deadlock if the main thread already holds the same
lock, e.g. inside Event.wait(). StopFlag only assigns an attribute, and wait() sleeps in
short slices, so nothing in the handler path takes a lock.
"""

import signal
import time

from nta.clock import monotonic


class StopFlag:
    def __init__(self, slice_s: float = 0.1) -> None:
        self._set = False
        self._slice_s = slice_s
        self.reason = "completed"
        self.signalled_at: float | None = None

    def set(self, reason: str | None = None) -> None:
        if reason:
            self.reason = reason
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:
        deadline = monotonic() + timeout
        while not self._set:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self._slice_s, remaining))
        return self._set


def install_signal_handlers(flag: StopFlag) -> dict[int, object]:
    """SIGTERM/SIGINT request a graceful stop (finish the current poll, then exit).
    A second Ctrl-C (SIGINT) more than 1 s after the first forces exit. Repeated SIGTERMs
    are ignored: `uv run` forwards SIGTERM to its child, so `pkill -f` on both delivers two.
    Returns the previous handlers so callers (tests) can restore them."""

    def handler(signum: int, _frame: object) -> None:
        now = monotonic()
        if flag.signalled_at is None:
            flag.signalled_at = now
            flag.set(f"signal {signal.Signals(signum).name}")
        elif signum == signal.SIGINT and now - flag.signalled_at > 1.0:
            raise KeyboardInterrupt

    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    for s in previous:
        signal.signal(s, handler)
    return previous
