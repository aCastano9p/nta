"""Pure trade simulation on recorded prices: used by the estimate ledger (hypothetical trades
for abstentions) and by replay's simulated book.

A long entered at `entry` exits at the first recorded price at or below the stop (filled at
the stop, or at that price if it gapped through), at or above the take-profit (filled at
the take-profit), or at the last price at or before expiry. Prices are point snapshots, so
a move between two snapshots is not seen: the result is an estimate.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

SPARSE_GAP = timedelta(minutes=30)


@dataclass(frozen=True)
class SimResult:
    exit_price: Decimal | None
    exit_time: datetime | None
    exit_reason: str | None      # stop | take_profit | expiry | no_data
    mae_bps: Decimal | None      # worst move against the position before exit
    mfe_bps: Decimal | None      # best move in favour
    uncertain: bool


def _bps(p: Decimal, entry: Decimal) -> Decimal:
    return ((p / entry - 1) * 10000).quantize(Decimal("0.01"))


def simulate_long(*, entry: Decimal, stop: Decimal, take_profit: Decimal, start: datetime, expiry: datetime,
                  prices: list[tuple[datetime, Decimal]]) -> SimResult:
    path = sorted((t, p) for t, p in prices if start < t <= expiry and p > 0)
    if not path:
        return SimResult(None, None, "no_data", None, None, True)
    gaps = [b[0] - a[0] for a, b in zip([(start, entry)] + path, path)]
    uncertain = any(g > SPARSE_GAP for g in gaps) or (expiry - path[-1][0]) > SPARSE_GAP
    lo = hi = entry
    for t, p in path:
        lo, hi = min(lo, p), max(hi, p)
        if p <= stop:  # a gap through the stop fills at the observed price (conservative)
            return SimResult(min(stop, p), t, "stop", _bps(lo, entry), _bps(hi, entry), uncertain)
        if p >= take_profit:
            return SimResult(take_profit, t, "take_profit", _bps(lo, entry), _bps(hi, entry), uncertain)
    t, p = path[-1]
    return SimResult(p, t, "expiry", _bps(lo, entry), _bps(hi, entry), uncertain)


def net_bps(entry: Decimal, exit_: Decimal, cost_bps: Decimal) -> Decimal:
    return (_bps(exit_, entry) - cost_bps).quantize(Decimal("0.01"))
