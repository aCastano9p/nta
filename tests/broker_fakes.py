"""In-memory broker with switchable failure modes."""

from dataclasses import replace
from decimal import Decimal

from nta.broker import Account, BrokerOrder, BrokerRejected, BrokerTimeout


class FakeBroker:
    def __init__(self, equity: str = "100000") -> None:
        self.orders: dict[str, BrokerOrder] = {}       # by client_order_id
        self.submissions: list[str] = []               # every accepted submission
        self.attempts = 0
        self.lookups = 0
        self.cancels: list[str] = []
        self.extra_positions: dict[str, Decimal] = {}  # held outside our orders (for mismatch tests)
        self.equity = Decimal(equity)
        self.last_equity = Decimal(equity)
        self.timeout_after_accept = False
        self.timeout_before_accept = False
        self.reject_next: tuple[int, str] | None = None
        self._n = 0

    # --- test controls ---
    def fill(self, cid: str, qty: str, price: str, status: str | None = None) -> None:
        o = self.orders[cid]
        new_qty = o.filled_qty + Decimal(qty)
        old_cost = (o.filled_avg_price or Decimal(0)) * o.filled_qty
        avg = (old_cost + Decimal(qty) * Decimal(price)) / new_qty
        st = status or ("filled" if new_qty >= o.qty else "partially_filled")
        self.orders[cid] = replace(o, filled_qty=new_qty, filled_avg_price=avg, status=st)

    def only(self) -> BrokerOrder:
        [o] = self.orders.values()
        return o

    # --- Broker protocol ---
    def submit_order(self, *, client_order_id, symbol, side, qty, limit_price, time_in_force="day"):
        self.attempts += 1
        if self.timeout_before_accept:
            self.timeout_before_accept = False
            raise BrokerTimeout("read timeout (not accepted)")
        if self.reject_next:
            status, body = self.reject_next
            self.reject_next = None
            raise BrokerRejected(status, body)
        if client_order_id in self.orders:
            raise BrokerRejected(422, '{"message":"client_order_id must be unique"}')
        self._n += 1
        o = BrokerOrder(id=f"B{self._n}", client_order_id=client_order_id, symbol=symbol, side=side,
                        qty=Decimal(qty), status="accepted", filled_qty=Decimal(0), filled_avg_price=None,
                        limit_price=Decimal(limit_price))
        self.orders[client_order_id] = o
        self.submissions.append(client_order_id)
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise BrokerTimeout("read timeout (accepted)")
        return o

    def get_by_client_order_id(self, cid):
        self.lookups += 1
        return self.orders.get(cid)

    def cancel(self, broker_order_id):
        self.cancels.append(broker_order_id)
        for cid, o in self.orders.items():
            if o.id == broker_order_id and not o.terminal:
                self.orders[cid] = replace(o, status="canceled")

    def open_orders(self):
        return [o for o in self.orders.values() if not o.terminal]

    def positions(self):
        pos: dict[str, Decimal] = dict(self.extra_positions)
        for o in self.orders.values():
            sign = 1 if o.side == "buy" else -1
            pos[o.symbol] = pos.get(o.symbol, Decimal(0)) + sign * o.filled_qty
        return {k: v for k, v in pos.items() if v != 0}

    def account(self):
        return Account(self.equity, self.last_equity, self.equity)
