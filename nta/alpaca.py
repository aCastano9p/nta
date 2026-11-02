"""Alpaca market data (free plan, IEX feed). Data API only.

There is deliberately no way to point this at a trading endpoint: the base URL must be on
data.alpaca.markets, and only GET /v2/stocks/snapshots is ever called.
"""

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx

from nta.clock import Clock
from nta.config import ConfigError
from nta.http import send_with_retry
from nta.ratelimit import TokenBucket

ALPACA_DATA_URL = "https://data.alpaca.markets"
ALLOWED_HOST = "data.alpaca.markets"
SOURCE = "alpaca_iex"
FEED = "iex"
MAX_SYMBOLS_PER_REQUEST = 100
MAX_PARALLEL_REQUESTS = 5  # 500 tickers in one round trip; the shared bucket still caps the rate


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    ts: datetime              # newest of the quote and trade timestamps
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None


def _price(v: object) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d > 0 else None  # IEX reports 0 when it has no quote on that side


def _ts(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)  # 3.11+: accepts Z and nanoseconds (truncated)
    except ValueError:
        return None
    return t if t.tzinfo else None


def parse_snapshots(payload: dict) -> dict[str, Snapshot]:
    """Parse GET /v2/stocks/snapshots. Symbols with neither a usable quote nor trade are
    left out."""
    body = payload.get("snapshots", payload) if isinstance(payload, dict) else {}
    out: dict[str, Snapshot] = {}
    for symbol, snap in body.items():
        if not isinstance(snap, dict):
            continue
        q = snap.get("latestQuote") or {}
        tr = snap.get("latestTrade") or {}
        bid, ask, last = _price(q.get("bp")), _price(q.get("ap")), _price(tr.get("p"))
        if bid is not None and ask is not None and ask < bid:
            bid = ask = None  # crossed quote: unusable
        times = [t for t in (_ts(q.get("t")) if (bid or ask) else None,
                             _ts(tr.get("t")) if last else None) if t]
        if not times:
            continue
        out[symbol] = Snapshot(symbol, max(times), bid, ask, last)
    return out


def alpaca_symbol(ticker: str) -> str:
    return ticker.replace("/", ".").replace("-", ".")


class AlpacaData:
    def __init__(self, http: httpx.Client, bucket: TokenBucket, clock: Clock,
                 key_id: str, secret: str, base_url: str = ALPACA_DATA_URL, feed: str = FEED) -> None:
        if feed not in ("iex", "sip"):
            raise ConfigError(f"unknown Alpaca feed {feed!r}")
        self.feed = feed
        self.source = f"alpaca_{feed}"
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
            raise ConfigError(f"Alpaca base URL must be https://{ALLOWED_HOST} (data API only)")
        self._http, self._bucket, self._clock = http, bucket, clock
        self._base = base_url.rstrip("/")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self._count_lock = threading.Lock()
        self.requests = 0

    def _fetch(self, chunk: list[str]) -> dict[str, Snapshot]:
        req = self._http.build_request(
            "GET", f"{self._base}/v2/stocks/snapshots",
            params={"symbols": ",".join(chunk), "feed": self.feed}, headers=self._headers)
        with self._count_lock:
            self.requests += 1
        resp = send_with_retry(self._http, req, bucket=self._bucket, clock=self._clock,
                               max_attempts=4, base_delay=0.25, max_delay=5.0)
        resp.raise_for_status()
        return parse_snapshots(resp.json())

    def snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        """Latest snapshot per symbol. Chunks of 100 symbols are fetched concurrently
        (httpx.Client and the token bucket are thread-safe); any chunk error is raised."""
        uniq = list(dict.fromkeys(symbols))
        chunks = [uniq[i:i + MAX_SYMBOLS_PER_REQUEST] for i in range(0, len(uniq), MAX_SYMBOLS_PER_REQUEST)]
        if len(chunks) <= 1:
            return self._fetch(chunks[0]) if chunks else {}
        out: dict[str, Snapshot] = {}
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REQUESTS, len(chunks)),
                                thread_name_prefix="alpaca") as pool:
            for part in pool.map(self._fetch, chunks):
                out.update(part)
        return out
