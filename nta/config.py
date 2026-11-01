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
