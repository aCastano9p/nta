import os
import signal
import threading
import time

import pytest

from nta.shutdown import StopFlag, install_signal_handlers


def test_wait_times_out_when_not_set() -> None:
    flag = StopFlag(slice_s=0.01)
    t0 = time.monotonic()
    assert flag.wait(0.05) is False
    assert time.monotonic() - t0 >= 0.05


def test_sigterm_during_wait_stops_promptly() -> None:
    flag = StopFlag(slice_s=0.01)
    previous = install_signal_handlers(flag)
    try:
        timer = threading.Timer(0.05, os.kill, args=(os.getpid(), signal.SIGTERM))
        timer.start()
        t0 = time.monotonic()
        assert flag.wait(5.0) is True
        assert time.monotonic() - t0 < 1.0
        assert flag.reason == "signal SIGTERM"
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def test_duplicate_sigterm_is_ignored_second_ctrl_c_forces_exit() -> None:
    flag = StopFlag()
    previous = install_signal_handlers(flag)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)  # e.g. forwarded again by `uv run`
        time.sleep(0.05)
        assert flag.is_set() and flag.reason == "signal SIGTERM"
        os.kill(os.getpid(), signal.SIGINT)   # within 1 s of the first: ignored
        time.sleep(0.05)
        flag.signalled_at = time.monotonic() - 5
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.5)  # handler runs here
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
