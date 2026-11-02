"""Structured JSON logs: one line per event on stdout."""

import json
import sys
from typing import Any, TextIO

from nta.clock import wall_now

_stream: TextIO | None = None  # None means "current sys.stdout" (plays well with pytest capture)


def set_stream(stream: TextIO | None) -> None:
    global _stream
    _stream = stream


def log(event: str, **fields: Any) -> None:
    record = {"ts": wall_now().isoformat(), "event": event, **fields}
    out = _stream or sys.stdout
    out.write(json.dumps(record, default=str, sort_keys=False) + "\n")
    out.flush()
