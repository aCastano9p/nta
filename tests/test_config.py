import pytest

from nta.config import ConfigError, optional_sec_user_agent, sec_user_agent


def test_sec_user_agent_required() -> None:
    with pytest.raises(ConfigError, match="not set"):
        sec_user_agent({})


@pytest.mark.parametrize("bad", ["jane@example.com", "Jane Doe", "Jane jane@example"])
def test_sec_user_agent_format(bad: str) -> None:
    with pytest.raises(ConfigError, match="<name> <email>"):
        sec_user_agent({"SEC_USER_AGENT": bad})


def test_sec_user_agent_ok() -> None:
    assert sec_user_agent({"SEC_USER_AGENT": "Jane Doe jane@example.com"}) == "Jane Doe jane@example.com"
    assert optional_sec_user_agent({}) is None
