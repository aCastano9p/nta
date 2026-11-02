"""Trading policy: policy/policy.yaml, read once at startup.

Nothing in this package writes the policy file. nta risk signs the file's sha256 with
RISK_SIGNING_KEY and records it in policy_versions; nta agent records an unsigned row for
what it saw. Every decision carries the hash, and nta risk rejects decisions made under a
policy other than the one it signed.
"""

import hashlib
import hmac
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from nta.logs import log
from nta.schema import PolicyVersion

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = ROOT / "policy" / "policy.yaml"


class SourceKind(StrEnum):
    filing = "filing"
    post = "post"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StopRule(_Strict):
    atr_multiple: Decimal = Field(gt=0)
    atr_periods: int = Field(ge=2)
    min_pct: Decimal = Field(gt=0)


class Blackouts(_Strict):
    minutes_after_open: int = Field(ge=0)
    minutes_before_close: int = Field(ge=0)
    windows: list[dict[str, datetime]] = []


class Clustering(_Strict):
    repeat_window_h: int = Field(gt=0)
    minhash_threshold: float = Field(gt=0, le=1)


class Corroboration(_Strict):
    min_origins: int = Field(ge=1)
    min_weight: Decimal = Field(ge=0)


class Policy(_Strict):
    long_only: Literal[True]  # shorts are out of scope; the file cannot turn them on
    quote_source: Literal["iex", "sip"]
    risk_unit_pct: Decimal = Field(gt=0, le=2)
    stop: StopRule
    take_profit_r: Decimal = Field(gt=0)
    position_cap_pct: Decimal = Field(gt=0, le=100)
    max_open_positions: int = Field(ge=1)
    cluster_cap_pct: Decimal = Field(gt=0, le=100)
    sector_cap_pct: Decimal = Field(gt=0, le=100)
    quote_max_age_s: float = Field(gt=0)
    spread_limit_bps: Decimal = Field(gt=0)
    daily_loss_stop_pct: Decimal = Field(gt=0)
    drawdown_pause_pct: Decimal = Field(gt=0)
    already_priced_k: Decimal = Field(gt=0)
    default_daily_vol_pct: Decimal = Field(gt=0)
    min_p_up: Decimal = Field(ge=0, le=1)
    latency_budget_min: dict[SourceKind, int]
    horizon_trading_days: dict[SourceKind, int]
    entry_limit_bps: Decimal = Field(ge=0)
    exit_limit_bps: Decimal = Field(ge=0)
    entry_ttl_s: float = Field(gt=0)
    assumed_spread_bps_unverified: Decimal = Field(ge=0)
    slippage_bps: Decimal = Field(ge=0)
    fee_bps: Decimal = Field(ge=0)
    blackouts: Blackouts
    clustering: Clustering
    corroboration: Corroboration
    sectors: dict[str, str] = {}

    @property
    def spread_unverified(self) -> bool:
        return self.quote_source == "iex"

    def sector(self, ticker: str | None) -> str:
        return self.sectors.get((ticker or "").upper(), "unknown")


class LoadedPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    policy: Policy
    text: str
    hash: str  # sha256 hex of the file bytes = policy_version


def policy_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse_policy(text: str) -> LoadedPolicy:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("policy file must be a YAML mapping")
    return LoadedPolicy(policy=Policy.model_validate(data), text=text, hash=policy_hash(text))


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> LoadedPolicy:
    return parse_policy(Path(path).read_text())


def sign(key: str, policy_hash_hex: str) -> str:
    return hmac.new(key.encode(), f"policy:{policy_hash_hex}".encode(), hashlib.sha256).hexdigest()


def verify(key: str, policy_hash_hex: str, signature: str) -> bool:
    return hmac.compare_digest(sign(key, policy_hash_hex), signature)


def record_policy_version(sf: sessionmaker[Session], loaded: LoadedPolicy, *, process: str,
                          run_id: str | None, signing_key: str | None = None) -> str:
    """Write a policy_versions row for the file as seen at startup. Returns the hash."""
    signature = sign(signing_key, loaded.hash) if signing_key else None
    with sf() as s, s.begin():
        s.add(PolicyVersion(policy_hash=loaded.hash, text=loaded.text, signature=signature,
                            process=process, run_id=run_id))
    log("policy_loaded", process=process, policy_version=loaded.hash, signed=signature is not None,
        quote_source=loaded.policy.quote_source)
    return loaded.hash
