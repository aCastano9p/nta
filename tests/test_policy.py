from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from nta.policy import (
    DEFAULT_POLICY_PATH,
    SourceKind,
    load_policy,
    parse_policy,
    policy_hash,
    record_policy_version,
    sign,
    verify,
)
from nta.schema import PolicyVersion


def test_default_policy_holds_the_brief_limits() -> None:
    lp = load_policy()
    p = lp.policy
    assert p.risk_unit_pct == Decimal("0.25")
    assert (p.stop.atr_multiple, p.stop.min_pct) == (Decimal("2.0"), Decimal("1.0"))
    assert p.position_cap_pct == 10 and p.max_open_positions == 5
    assert p.cluster_cap_pct == 15 and p.sector_cap_pct == 25
    assert p.quote_max_age_s == 5 and p.spread_limit_bps == 20
    assert p.daily_loss_stop_pct == 1 and p.drawdown_pause_pct == 5
    assert p.latency_budget_min[SourceKind.filing] == 60
    assert p.horizon_trading_days[SourceKind.filing] == 2
    assert p.already_priced_k == Decimal("1.5")
    assert p.long_only is True
    assert p.quote_source == "iex" and p.spread_unverified
    assert lp.hash == policy_hash(DEFAULT_POLICY_PATH.read_text())


def test_hash_changes_with_any_byte() -> None:
    text = DEFAULT_POLICY_PATH.read_text()
    assert parse_policy(text).hash != parse_policy(text + "\n# comment\n").hash


@pytest.mark.parametrize("edit", [
    ("long_only: true", "long_only: false"),           # shorts cannot be switched on
    ("quote_source: iex", "quote_source: polygon"),
    ("max_open_positions: 5", "max_open_positions: 5\nleverage: 2"),  # unknown keys rejected
])
def test_invalid_policies_are_rejected(edit: tuple[str, str]) -> None:
    text = DEFAULT_POLICY_PATH.read_text().replace(*edit)
    with pytest.raises(ValidationError):
        parse_policy(text)


def test_sip_policy_verifies_spreads() -> None:
    p = parse_policy(DEFAULT_POLICY_PATH.read_text().replace("quote_source: iex", "quote_source: sip")).policy
    assert not p.spread_unverified


def test_sector_lookup() -> None:
    p = load_policy().policy
    assert p.sector("aapl") == "information_technology"
    assert p.sector("ZZZZ") == "unknown"


def test_signature_needs_the_key() -> None:
    h = "ab" * 32
    s = sign("k1", h)
    assert verify("k1", h, s)
    assert not verify("k2", h, s)
    assert not verify("k1", "cd" * 32, s)


def test_record_policy_version(session_factory) -> None:
    lp = load_policy()
    record_policy_version(session_factory, lp, process="risk", run_id=None, signing_key="secret")
    record_policy_version(session_factory, lp, process="agent", run_id=None)
    with session_factory() as s:
        rows = {r.process: r for r in s.scalars(select(PolicyVersion))}
    assert rows["risk"].text == lp.text and rows["risk"].policy_hash == lp.hash
    assert verify("secret", lp.hash, rows["risk"].signature)
    assert rows["agent"].signature is None
