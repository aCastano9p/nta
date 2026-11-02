"""Broker access for nta risk only: Alpaca *paper* trading API, plus order signing.

- The base URL must be https://paper-api.alpaca.markets; live trading cannot be configured.
- POST /v2/orders is sent once and never retried automatically. A timeout raises
  BrokerTimeout and the executor looks the order up by client_order_id before anything else.
- client_order_id is a pure function of (decision_id, leg).
- The HMAC covers (decision_id, side, qty, limit); the executor refuses unsigned orders.
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlparse

import httpx

from nta.clock import Clock
from nta.config import ConfigError
from nta.http import send_with_retry
from nta.ratelimit import TokenBucket

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
PAPER_HOST = "paper-api.alpaca.markets"
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"})


def client_order_id(decision_id: str, leg: int) -> str:
    return f"nta-{decision_id}-{leg}"


def limit_str(limit: Decimal) -> str:
    return f"{Decimal(limit):.4f}"


def order_message(decision_id: str, side: str, qty: int, limit: Decimal) -> bytes:
    return f"order:{decision_id}|{side}|{int(qty)}|{limit_str(limit)}".encode()


def sign_order(key: str, decision_id: str, side: str, qty: int, limit: Decimal) -> str:
    return hmac.new(key.encode(), order_message(decision_id, side, qty, limit), hashlib.sha256).hexdigest()


def verify_order(key: str, decision_id: str, side: str, qty: int, limit: Decimal, signature: str) -> bool:
    return hmac.compare_digest(sign_order(key, decision_id, side, qty, limit), signature or "")


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    status: str
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    limit_price: Decimal | None
    updated_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class Account:
    equity: Decimal
    last_equity: Decimal
    cash: Decimal


class BrokerTimeout(RuntimeError):
    """The broker may or may not have accepted the request."""


class BrokerRejected(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"broker rejected order: HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


class Broker(Protocol):
    def submit_order(self, *, client_order_id: str, symbol: str, side: str, qty: int, limit_price: Decimal,
                     time_in_force: str) -> BrokerOrder: ...
    def get_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def cancel(self, broker_order_id: str) -> None: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def positions(self) -> dict[str, Decimal]: ...
    def account(self) -> Account: ...


def _dec(v: object) -> Decimal | None:
    if v in (None, ""):
        return None
    return Decimal(str(v))


def _dt(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def parse_order(o: dict) -> BrokerOrder:
    return BrokerOrder(id=str(o["id"]), client_order_id=str(o.get("client_order_id") or ""), symbol=str(o["symbol"]),
                       side=str(o["side"]), qty=_dec(o.get("qty")) or Decimal(0), status=str(o["status"]),
                       filled_qty=_dec(o.get("filled_qty")) or Decimal(0),
                       filled_avg_price=_dec(o.get("filled_avg_price")), limit_price=_dec(o.get("limit_price")),
                       updated_at=_dt(o.get("updated_at")))


class AlpacaPaperBroker:
    def __init__(self, http: httpx.Client, bucket: TokenBucket, clock: Clock, key_id: str, secret: str,
                 base_url: str = PAPER_TRADING_URL) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != PAPER_HOST:
            raise ConfigError(f"trading base URL must be https://{PAPER_HOST} (paper only)")
        self._http, self._bucket, self._clock = http, bucket, clock
        self._base = base_url.rstrip("/")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        req = self._http.build_request("GET", self._base + path, params=params, headers=self._headers)
        return send_with_retry(self._http, req, bucket=self._bucket, clock=self._clock, max_attempts=4,
                               base_delay=0.25, max_delay=5.0)

    def submit_order(self, *, client_order_id: str, symbol: str, side: str, qty: int, limit_price: Decimal,
                     time_in_force: str = "day") -> BrokerOrder:
        body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": "limit",
                "time_in_force": time_in_force, "limit_price": str(Decimal(limit_price).quantize(Decimal("0.01"))),
                "client_order_id": client_order_id}
        self._bucket.acquire()
        try:  # exactly one attempt: a retry here could double an order
            resp = self._http.post(self._base + "/v2/orders", json=body, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise BrokerTimeout(str(exc)) from exc
        except httpx.TransportError as exc:  # unknown whether it arrived
            raise BrokerTimeout(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 500:
            raise BrokerTimeout(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BrokerRejected(resp.status_code, resp.text)
        return parse_order(resp.json())

    def get_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        resp = self._get("/v2/orders:by_client_order_id", {"client_order_id": client_order_id})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return parse_order(resp.json())

    def cancel(self, broker_order_id: str) -> None:
        self._bucket.acquire()
        resp = self._http.delete(f"{self._base}/v2/orders/{broker_order_id}", headers=self._headers)
        if resp.status_code not in (200, 204, 404, 422):  # 422: already filled/cancelled
            resp.raise_for_status()

    def open_orders(self) -> list[BrokerOrder]:
        resp = self._get("/v2/orders", {"status": "open", "limit": 500})
        resp.raise_for_status()
        return [parse_order(o) for o in resp.json()]

    def positions(self) -> dict[str, Decimal]:
        resp = self._get("/v2/positions")
        resp.raise_for_status()
        return {str(p["symbol"]): Decimal(str(p["qty"])) for p in resp.json()}

    def account(self) -> Account:
        resp = self._get("/v2/account")
        resp.raise_for_status()
        a = resp.json()
        return Account(Decimal(str(a["equity"])), Decimal(str(a.get("last_equity") or a["equity"])),
                       Decimal(str(a.get("cash") or 0)))
