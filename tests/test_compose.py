"""The two-credential split also holds in the deployment file."""

from pathlib import Path

import yaml

from nta.config import TRADING_ENV

COMPOSE = yaml.safe_load((Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text())


def env_of(service: str) -> set[str]:
    return set((COMPOSE["services"][service].get("environment") or {}).keys())


def test_no_service_loads_a_whole_env_file() -> None:
    assert all("env_file" not in s for s in COMPOSE["services"].values())


def test_agent_gets_no_trading_credentials_and_risk_no_llm_key() -> None:
    assert not env_of("agent") & set(TRADING_ENV)
    assert "ANTHROPIC_API_KEY" in env_of("agent")
    assert not {k for k in env_of("risk") if k.startswith("ANTHROPIC_")}
    assert {"ALPACA_TRADING_KEY_ID", "ALPACA_TRADING_SECRET", "RISK_SIGNING_KEY"} <= env_of("risk")
    for svc in ("poller", "quotes", "migrate"):
        assert not env_of(svc) & (set(TRADING_ENV) | {"ANTHROPIC_API_KEY"}), svc


def test_plain_up_starts_only_the_database() -> None:
    unprofiled = [n for n, s in COMPOSE["services"].items() if "profiles" not in s and n != "db"]
    # services inherit the profile from the x-app anchor
    assert unprofiled == []
    assert "profiles" not in COMPOSE["services"]["db"]
