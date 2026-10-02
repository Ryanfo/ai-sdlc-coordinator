"""Jira credential lookup: environment variables first, then the macOS Keychain.

The config file never holds a secret. It may name the account email (not a secret) and a
Keychain service under which the API token is stored once, encrypted, by the developer:

    delivery credentials set --config ~/delivery.local.toml

The token is read when a Jira client is created, held only in that process's memory, and
never exported to child processes (checks, Git or Claude workers).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from delivery.config import Config

SECURITY = "/usr/bin/security"
# Atlassian API tokens are long base64url-style strings. Anything else is a bad paste.
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_=+/.-]{40,4096}$")
FIGMA_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_=+/.:-]{20,1024}$")

# Runs `security` and returns (exit code, stdout). Injectable for tests.
Runner = Callable[[list[str]], tuple[int, str]]


class CredentialsMissing(Exception):
    pass


@dataclass(frozen=True)
class JiraCredentials:
    email: str
    token: str
    source: str  # "environment" or "keychain:<service>" - never the value

    def __repr__(self) -> str:
        return f"JiraCredentials(email={self.email!r}, token=<redacted>, source={self.source!r})"


def _run(argv: list[str]) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    return r.returncode, r.stdout


def keychain_available() -> bool:
    return platform.system() == "Darwin" and (os.path.exists(SECURITY) or bool(shutil.which("security")))


def keychain_read(service: str, account: str, runner: Runner | None = None) -> str | None:
    code, out = (runner or _run)([SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"])
    token = out.strip()
    return token if code == 0 and token else None


def keychain_write(service: str, account: str, token: str, shape: re.Pattern[str] = TOKEN_SHAPE) -> None:
    """Store the token, replacing any earlier one, and confirm it reads back intact.

    The `security add-generic-password` password prompt silently truncates input at 128
    characters (Atlassian tokens are ~190), and passing the token as an argument would expose it
    in the process list. Instead the command goes to `security -i` on standard input.
    """
    if not shape.match(token):
        raise ValueError("that does not look like an API token")
    if any(c in '"\\' for c in service + account):
        raise ValueError("Keychain service and account cannot contain quotes or backslashes")
    command = (
        f'add-generic-password -U -s "{service}" -a "{account}" -l "delivery: Jira API token" -w "{token}"\n'
    )
    subprocess.run([SECURITY, "-i"], input=command, text=True, capture_output=True, check=False, timeout=30)
    if keychain_read(service, account) != token:
        raise OSError("the Keychain did not store the token intact")


def resolve_jira(cfg: Config, runner: Runner | None = None) -> JiraCredentials:
    j = cfg.jira
    email = os.environ.get(j.email_env, "") or j.email
    token = os.environ.get(j.token_env, "")
    if email and token:
        return JiraCredentials(email, token, "environment")
    if not email:
        raise CredentialsMissing(f"set jira.email in the config (or {j.email_env} in the environment)")
    if j.token_keychain_service:
        if runner is None and not keychain_available():
            raise CredentialsMissing(
                f"jira.token_keychain_service needs the macOS Keychain; on this OS set {j.token_env}"
            )
        found = keychain_read(j.token_keychain_service, email, runner)
        if found:
            return JiraCredentials(email, found, f"keychain:{j.token_keychain_service}")
        raise CredentialsMissing(
            f"no Keychain item for service {j.token_keychain_service!r} and account {email}; "
            "run `delivery credentials set --config <file>`"
        )
    raise CredentialsMissing(
        f"set {j.token_env} in the environment, or set jira.token_keychain_service and run "
        "`delivery credentials set --config <file>`"
    )


def resolve_figma(cfg: Config, runner: Runner | None = None) -> str | None:
    """The Figma token, or None when none is configured (Figma links are then skipped)."""
    f = cfg.figma
    token = os.environ.get(f.token_env, "")
    if token:
        return token
    if runner is None and not keychain_available():
        return None
    return keychain_read(f.token_keychain_service, f.token_account, runner)
