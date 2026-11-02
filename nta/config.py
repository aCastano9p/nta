"""Settings from environment variables only. Secrets never come from files in the repo."""

import os
import re
from collections.abc import Mapping

# "<name> <email>", e.g. "Jane Doe jane@example.com"
_SEC_UA_RE = re.compile(r"^\S(.*\S)?\s+[^\s@]+@[^\s@]+\.[^\s@]+$")


class ConfigError(RuntimeError):
    pass


def optional_sec_user_agent(env: Mapping[str, str] = os.environ) -> str | None:
    value = env.get("SEC_USER_AGENT", "").strip()
    if not value:
        return None
    if not _SEC_UA_RE.match(value):
        raise ConfigError(
            f'SEC_USER_AGENT must look like "<name> <email>", got {value!r}'
        )
    return value


def sec_user_agent(env: Mapping[str, str] = os.environ) -> str:
    value = optional_sec_user_agent(env)
    if value is None:
        raise ConfigError('SEC_USER_AGENT is not set; SEC requires "<name> <email>".')
    return value


def alpaca_credentials(env: Mapping[str, str] = os.environ) -> tuple[str, str]:
    key, secret = env.get("ALPACA_KEY_ID", "").strip(), env.get("ALPACA_SECRET", "").strip()
    placeholders = {"", "replace-me"}
    if key in placeholders or secret in placeholders:
        raise ConfigError("ALPACA_KEY_ID and ALPACA_SECRET must be set (market data API keys).")
    return key, secret


def openfigi_api_key(env: Mapping[str, str] = os.environ) -> str | None:
    """Optional. Without it OpenFIGI allows 25 requests/min of 10 tickers; with it 250/min of 100."""
    value = env.get("OPENFIGI_API_KEY", "").strip()
    return value if value and value != "replace-me" else None


# ---------- phase 1: two processes, two credential sets ----------

# Account T (trading) and the signing key belong to nta risk only.
TRADING_ENV = ("ALPACA_TRADING_KEY_ID", "ALPACA_TRADING_SECRET", "RISK_SIGNING_KEY")


def _set(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip() not in ("", "replace-me")


def assert_agent_env(env: Mapping[str, str] = os.environ) -> None:
    """nta agent holds data credentials and the LLM key, never trading credentials."""
    present = [n for n in TRADING_ENV if _set(env, n)]
    if present:
        raise ConfigError(f"nta agent refuses to start: trading credentials in its environment ({', '.join(present)})")


def assert_risk_env(env: Mapping[str, str] = os.environ) -> None:
    """nta risk holds trading credentials and never an LLM credential of any kind."""
    present = sorted(n for n in env if n.startswith("ANTHROPIC_") and env[n].strip())
    if present:
        raise ConfigError(f"nta risk refuses to start: LLM credentials in its environment ({', '.join(present)})")


def anthropic_api_key(env: Mapping[str, str] = os.environ) -> str:
    key = env.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ConfigError("ANTHROPIC_API_KEY must be set for nta agent.")
    return key


def trading_credentials(env: Mapping[str, str] = os.environ) -> tuple[str, str]:
    key, secret = env.get("ALPACA_TRADING_KEY_ID", "").strip(), env.get("ALPACA_TRADING_SECRET", "").strip()
    if key in ("", "replace-me") or secret in ("", "replace-me"):
        raise ConfigError("ALPACA_TRADING_KEY_ID and ALPACA_TRADING_SECRET must be set (paper account T).")
    data_key = env.get("ALPACA_KEY_ID", "").strip()
    if data_key and data_key == key:
        raise ConfigError("account T and account D must be different paper accounts (same key id given).")
    return key, secret


def risk_signing_key(env: Mapping[str, str] = os.environ) -> str:
    key = env.get("RISK_SIGNING_KEY", "").strip()
    if len(key) < 32:
        raise ConfigError("RISK_SIGNING_KEY must be set (at least 32 characters, e.g. `openssl rand -hex 32`).")
    return key


# ---------- optional X ingestion ----------

def x_enabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get("X_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def x_bearer_token(env: Mapping[str, str] = os.environ) -> str:
    token = env.get("X_BEARER_TOKEN", "").strip()
    if not token or token == "replace-me":
        raise ConfigError("X_BEARER_TOKEN must be set when X_ENABLED=true.")
    return token


def x_daily_usd(env: Mapping[str, str] = os.environ) -> "Decimal":
    from decimal import Decimal
    return Decimal(env.get("X_DAILY_USD") or "3")


def x_poll_interval(env: Mapping[str, str] = os.environ) -> float:
    return min(10.0, max(5.0, float(env.get("X_POLL_INTERVAL") or 7)))
