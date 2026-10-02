from __future__ import annotations

import base64

import httpx
import pytest

from conftest import ConfigFactory
from delivery.config import ConfigError
from delivery.credentials import SECURITY, CredentialsMissing, resolve_jira
from delivery.jira import JiraClient, JiraCredentialsMissing

TOKEN = "ATATT3xFfGF0" + "keychain" * 22 + "=0A1B2C3D"  # realistic ~190-character token
KEYCHAIN = {"jira": {"email": "dev@example.com", "token_keychain_service": "delivery-jira"}}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)


class Keychain:
    def __init__(self, items: dict[tuple[str, str], str]) -> None:
        self.items = items
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        service, account = argv[argv.index("-s") + 1], argv[argv.index("-a") + 1]
        value = self.items.get((service, account))
        return (0, value + "\n") if value else (44, "")


def test_environment_wins_over_keychain(make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_API_TOKEN", "from-env")
    kc = Keychain({("delivery-jira", "dev@example.com"): TOKEN})
    creds = resolve_jira(make_config(overrides=KEYCHAIN), kc)
    assert (creds.token, creds.source) == ("from-env", "environment")
    assert kc.calls == []


def test_token_comes_from_keychain_and_is_never_shown(make_config: ConfigFactory) -> None:
    kc = Keychain({("delivery-jira", "dev@example.com"): TOKEN})
    creds = resolve_jira(make_config(overrides=KEYCHAIN), kc)
    assert creds.token == TOKEN
    assert creds.source == "keychain:delivery-jira"
    assert TOKEN not in repr(creds)
    assert kc.calls == [
        [SECURITY, "find-generic-password", "-s", "delivery-jira", "-a", "dev@example.com", "-w"]
    ]


def test_missing_keychain_item_explains_how_to_store_it(make_config: ConfigFactory) -> None:
    with pytest.raises(CredentialsMissing, match="delivery credentials set"):
        resolve_jira(make_config(overrides=KEYCHAIN), Keychain({}))


def test_no_token_source_configured(make_config: ConfigFactory) -> None:
    cfg = make_config(overrides={"jira": {"email": "dev@example.com"}})
    with pytest.raises(CredentialsMissing, match="JIRA_API_TOKEN"):
        resolve_jira(cfg, Keychain({}))


@pytest.mark.parametrize(
    ("key", "value"),
    [("email", "not-an-email"), ("token_keychain_service", "has spaces"), ("token_keychain_service", "a/b")],
)
def test_config_rejects_bad_credential_references(make_config: ConfigFactory, key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        make_config(overrides={"jira": {key: value}})


async def test_client_authenticates_with_the_keychain_token(make_config: ConfigFactory) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"accountId": "dev-account-0001", "displayName": "Dev"})

    kc = Keychain({("delivery-jira", "dev@example.com"): TOKEN})
    client = JiraClient(make_config(overrides=KEYCHAIN), httpx.MockTransport(handler), keychain=kc)
    try:
        await client.myself()
    finally:
        await client.close()
    assert seen == ["Basic " + base64.b64encode(f"dev@example.com:{TOKEN}".encode()).decode()]
    assert client.credential_source == "keychain:delivery-jira"


def test_client_reports_missing_keychain_item(make_config: ConfigFactory) -> None:
    with pytest.raises(JiraCredentialsMissing, match="no Keychain item"):
        JiraClient(make_config(overrides=KEYCHAIN), keychain=Keychain({}))


def test_keychain_write_refuses_a_bad_paste() -> None:
    from delivery.credentials import keychain_write

    with pytest.raises(ValueError, match="does not look like"):
        keychain_write("delivery-jira", "dev@example.com", "short token")


@pytest.mark.parametrize(("jira_accepts", "stored"), [(False, False), (True, True)])
def test_set_stores_only_a_token_jira_accepts(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, jira_accepts: bool, stored: bool
) -> None:
    import getpass

    from delivery import cli, credentials

    cfg = make_config(overrides=KEYCHAIN)
    writes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": TOKEN + "\n")
    monkeypatch.setattr(credentials, "keychain_available", lambda: True)
    monkeypatch.setattr(credentials, "keychain_write", lambda *a: writes.append(a[:3]))

    async def whoami(cfg, creds=None):  # type: ignore[no-untyped-def]
        assert creds.token == TOKEN
        return "Dev (dev-account-0001)" if jira_accepts else None

    monkeypatch.setattr(cli, "_jira_whoami", whoami)
    code = cli.main(["credentials", "set", "--config", str(cfg.source_path)])
    assert (code == 0) is jira_accepts
    assert writes == ([("delivery-jira", "dev@example.com", TOKEN)] if stored else [])
