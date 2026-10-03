"""``delivery setup``: answer a few questions and get a working config file.

Answers go into the commented template (or your existing config) one value at a time, so every
comment, and anything changed by hand, stays. Whatever can be looked up is looked up rather than
asked: your Jira account ID comes from your token, approvers are found by name, and the issue
types, base branch, required CI checks and workflow status IDs come from Jira and GitHub. Check
commands are read from the application's package.json.

Nothing here blocks: a missing status, tool or sign-in is reported with what to do about it, and
the config is still written so `delivery doctor` can take over.

With a team's shared project file (``--project``, or one found in this installation's
``projects/`` folder) the team's settings come from that file and only your own are asked: your
email and token, your clone, session windows and this laptop. `delivery project export` writes
such a file from a working config.

The wizard is synchronous, with its own event loop for the Jira and GitHub calls, so Ctrl-C at
a question stops it at once (``asyncio.run`` would only cancel the task at the next await).
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlparse

from pydantic import ValidationError

from delivery.config import (
    ACCOUNT_ID,
    MODEL_NAME,
    Config,
    _format_error,
    merge_tables,
    personal_keys_in,
    split_personal,
    template_text,
)
from delivery.credentials import (
    FIGMA_TOKEN_SHAPE,
    TOKEN_SHAPE,
    JiraCredentials,
    keychain_available,
    keychain_read,
    keychain_write,
)
from delivery.ports import (
    AuthError,
    BranchProtection,
    IntegrationError,
    JiraPort,
    JiraUser,
    NotFound,
    RepoInfo,
)
from delivery.workflow import Status

T = TypeVar("T")

TOKEN_PAGE = "https://id.atlassian.com/manage-profile/security/api-tokens"  # noqa: S105 - a web page
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
# Template values that are examples, never offered back as an answer.
PLACEHOLDERS = (
    "YOUR_",
    "APPROVER_ACCOUNT_ID",
    "your-site",
    "your-org",
    "your-app",
    "/absolute/path/",
    "you@example.com",
    "human-reviewer",
    "my-laptop",
)


INSTALL = Path(__file__).resolve().parents[2]
PROJECTS_DIR = INSTALL / "projects"

PERSONAL_TEMPLATE = """\
# delivery personal configuration
#
# The team's shared settings (Jira site and workflow, repository, approvers, checks, models)
# come from the project file below; this file holds only what is personal to you and this
# laptop. A value set here wins over the project file. Keep this file out of version control:
# it never contains tokens (setup stores those in your Keychain).

config_version = 1
project = {project}

[identity]
# Your Jira account ID; `delivery setup` fills it in from your Jira login.
developer_jira_account_id = "YOUR_ACCOUNT_ID"
# Names this laptop. Only one coordinator per Jira identity may run.
worker_id = "my-laptop"

[jira]
email = "you@example.com"

[repository]
# Your normal clone of the application. The coordinator never edits it.
checkout_path = "~/src/your-app"
# Managed worktrees, one per run. Must be outside the checkout.
worktree_root = "~/delivery-worktrees"

[runtime]
# Local recovery journal, logs and locks. Must be outside any Git checkout.
state_dir = "~/.local/state/delivery"

# Watch Claude work and type to it: each session runs in tmux and a terminal window opens on it.
# [claude.interactive]
# enabled = true
# window = "Terminal"       # or "iTerm", or "none" (then: coordinator attach <ticket>)
"""


# --------------------------------------------------------------------------- editing the TOML text
#
# tomllib only reads, and rewriting the file from data would lose its comments. These edit the
# text line by line instead: a table runs from its header to the next header.

_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?$")
_COMMENTED_HEADER = re.compile(r"^\s*#\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
_COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Za-z0-9_-]+)\s*=")


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a valid TOML basic string.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _section(lines: list[str], table: str) -> tuple[int, int] | None:
    start = next(
        (i for i, line in enumerate(lines) if (m := _HEADER.match(line)) and m.group(1) == table), None
    )
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _HEADER.match(lines[i])), len(lines))
    return start, end


def _open_section(lines: list[str], table: str) -> tuple[int, int]:
    """The table's line range, switching on its commented-out example or adding it at the end."""
    found = _section(lines, table)
    if found:
        return found
    for i, line in enumerate(lines):
        m = _COMMENTED_HEADER.match(line)
        if m and m.group(1) == table:
            lines[i] = f"[{table}]"
            found = _section(lines, table)
            assert found
            return found
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["", f"[{table}]"]
    return len(lines) - 1, len(lines)


_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'')


def _depth(text: str) -> int:
    text = _STRING.sub("", text).split("#", 1)[0]
    return text.count("[") - text.count("]")


def _inline_comment(line: str) -> str:
    """The comment after a one-line ``key = value``, with the spacing before it."""
    masked = _STRING.sub(lambda m: "x" * len(m.group()), line)
    at = masked.find("#", masked.find("=") + 1)
    return "" if at < 0 else line[len(line[:at].rstrip()) :]


def _value_end(lines: list[str], i: int) -> int:
    """Index after the last line of the value starting on line ``i`` (arrays can span lines)."""
    depth, j = _depth(lines[i].split("=", 1)[1]), i + 1
    while depth > 0 and j < len(lines):
        depth += _depth(lines[j])
        j += 1
    return j


def _append_at(lines: list[str], start: int, end: int) -> int:
    """Just after the table's last non-blank line."""
    i = end
    while i > start + 1 and not lines[i - 1].strip():
        i -= 1
    return i


def set_value(text: str, table: str, key: str, value: object) -> str:
    """Set ``key`` in ``[table]``: replace it, switch on its commented-out example, or add it."""
    lines = text.splitlines()
    start, end = _open_section(lines, table)
    new = f"{key} = {toml_value(value)}"
    for pattern in (_KEY, _COMMENTED_KEY):
        for i in range(start + 1, end):
            m = pattern.match(lines[i])
            if m and m.group(1) == key:
                stop = _value_end(lines, i) if pattern is _KEY else i + 1
                comment = _inline_comment(lines[i]) if stop == i + 1 else ""
                lines[i:stop] = [new + comment]
                return "\n".join(lines) + "\n"
    lines.insert(_append_at(lines, start, end), new)
    return "\n".join(lines) + "\n"


def unset_value(text: str, table: str, key: str) -> str:
    """Comment out ``key`` in ``[table]`` so its default applies."""
    lines = text.splitlines()
    found = _section(lines, table)
    if found:
        for i in range(found[0] + 1, found[1]):
            m = _KEY.match(lines[i])
            if m and m.group(1) == key:
                stop = _value_end(lines, i)
                lines[i:stop] = ["# " + line for line in lines[i:stop]]
                break
    return "\n".join(lines) + "\n"


def set_table(text: str, table: str, entries: dict[str, Any]) -> str:
    """Replace every key in ``[table]`` with ``entries``, keeping the table's comments."""
    lines = text.splitlines()
    start, end = _open_section(lines, table)
    i = start + 1
    while i < end:
        if _KEY.match(lines[i]):
            stop = _value_end(lines, i)
            del lines[i:stop]
            end -= stop - i
        else:
            i += 1
    at = _append_at(lines, start, end)
    lines[at:at] = [f"{k} = {toml_value(v)}" for k, v in entries.items()]
    return "\n".join(lines) + "\n"


def set_top(text: str, key: str, value: object) -> str:
    """Set a top-level ``key`` (before the first table)."""
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if _HEADER.match(line)), len(lines))
    new = f"{key} = {toml_value(value)}"
    for i in range(first):
        m = _KEY.match(lines[i])
        if m and m.group(1) == key:
            lines[i : _value_end(lines, i)] = [new]
            return "\n".join(lines) + "\n"
    at = first
    while at > 0 and not lines[at - 1].strip():
        at -= 1
    lines.insert(at, new)
    return "\n".join(lines) + "\n"


def _bare(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def dumps(data: dict[str, Any], header: str = "") -> str:
    """A new TOML document: top-level values first, then each table as ``[a.b]``."""
    lines = [f"# {ln}".rstrip() for ln in header.splitlines()]
    if lines:
        lines.append("")
    lines += [f"{_bare(k)} = {toml_value(v)}" for k, v in data.items() if not isinstance(v, dict)]

    def table(prefix: str, body: dict[str, Any]) -> None:
        values = {k: v for k, v in body.items() if not isinstance(v, dict)}
        if values or not any(isinstance(v, dict) for v in body.values()):
            lines.extend(["", f"[{prefix}]", *(f"{_bare(k)} = {toml_value(v)}" for k, v in values.items())])
        for k, v in body.items():
            if isinstance(v, dict):
                table(f"{prefix}.{_bare(k)}", v)

    for k, v in data.items():
        if isinstance(v, dict):
            table(_bare(k), v)
    text = "\n".join(lines).strip("\n") + "\n"
    tomllib.loads(text)
    return text


def project_candidates() -> list[Path]:
    """Shared project files kept in this installation (``projects/*.toml``)."""
    return sorted(PROJECTS_DIR.glob("*.toml")) if PROJECTS_DIR.is_dir() else []


def _project_of(path: Path, text: str) -> Path | None:
    with contextlib.suppress(tomllib.TOMLDecodeError):
        ref = tomllib.loads(text).get("project")
        if isinstance(ref, str) and ref:
            p = Path(ref).expanduser()
            return p if p.is_absolute() else path.parent / p
    return None


def export_project(config_path: Path, out: Path, force: bool = False) -> Path:
    """Write the team settings of a working config as a shared project file."""
    raw = tomllib.loads(config_path.read_text())
    project = _project_of(config_path, config_path.read_text())
    raw.pop("project", None)
    if project:
        raw = merge_tables(tomllib.loads(project.read_text()), raw)
    team, _personal = split_personal(raw)
    team.pop("config_version", None)
    if out.exists() and not force:
        raise FileExistsError(out)
    header = (
        "delivery shared project settings\n\n"
        "The same for everyone on this project. Each developer's own config names this file\n"
        '(project = "<path>") and adds only personal settings; `delivery setup --project` writes it.\n'
        "No secrets, nothing personal and nothing specific to one laptop."
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps(team, header))
    return out


# --------------------------------------------------------------------------- reading answers


def parse_site(answer: str) -> tuple[str | None, str]:
    """The Jira site URL from an address, a site name or any pasted Jira link, plus a project
    key if the link names one (``.../projects/SDLC/boards/1``, ``.../browse/SDLC-12``)."""
    a = answer.strip()
    if not a:
        return None, ""
    u = urlparse(a if "://" in a else "https://" + a)
    host = (u.hostname or "").lower()
    if not re.match(r"^[a-z0-9][a-z0-9.-]*$", host):
        return None, ""
    if "." not in host:
        host += ".atlassian.net"
    m = re.search(r"/(?:projects|browse)/([A-Z][A-Z0-9_]{1,9})(?:[/-]|$)", u.path)
    return f"https://{host}", m.group(1) if m else ""


def parse_repo(answer: str) -> str | None:
    """``owner/name`` from ``owner/name``, a GitHub link or a clone URL (HTTPS or SSH)."""
    a = answer.strip()
    if "github.com" in a:
        a = a.split("github.com", 1)[1].lstrip("/:")
    parts = [p for p in a.split("/") if p][:2]
    if len(parts) != 2:
        return None
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not re.match(r"^[A-Za-z0-9-]+$", owner) or not re.match(r"^[A-Za-z0-9._-]+$", name):
        return None
    return f"{owner}/{name}"


def split_list(answer: str) -> list[str]:
    return [part.strip() for part in answer.split(",") if part.strip()]


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and any(p in value for p in PLACEHOLDERS)


def tilde(path: Path, home: Path | None = None) -> str:
    home = home or Path.home()
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def machine_name() -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname().split(".")[0]).strip("-.")
    return (name or "laptop")[:64]


def bundled_plugin_path() -> Path | None:
    """``plugins/delivery`` of the checkout this code runs from (an editable install)."""
    plugin = Path(__file__).resolve().parents[2] / "plugins" / "delivery"
    return plugin if (plugin / ".claude-plugin" / "plugin.json").exists() else None


# Install command per lockfile, most specific first.
_INSTALL = (
    ("pnpm-lock.yaml", "pnpm", ["pnpm", "install", "--frozen-lockfile"]),
    ("yarn.lock", "yarn", ["yarn", "install", "--frozen-lockfile"]),
    ("bun.lock", "bun", ["bun", "install", "--frozen-lockfile"]),
    ("bun.lockb", "bun", ["bun", "install", "--frozen-lockfile"]),
    ("package-lock.json", "npm", ["npm", "ci"]),
)
# Check name -> package.json scripts that provide it, most specific first.
_SCRIPTS = {
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "types", "tsc"),
    "unit": ("test:unit", "unit", "test"),
    "build": ("build",),
    "e2e": ("test:e2e", "e2e"),
}


def _package(checkout: Path) -> tuple[str, list[str], dict[str, Any]] | None:
    """(package manager, install command, scripts) of a JavaScript project."""
    try:
        scripts = json.loads((checkout / "package.json").read_text()).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return None
    for lockfile, tool, install in _INSTALL:
        if (checkout / lockfile).exists():
            return tool, install, scripts
    return "npm", ["npm", "install"], scripts


def detect_checks(checkout: Path) -> tuple[list[str], dict[str, list[str]]] | None:
    """The setup command and check commands of a JavaScript project, from its package.json."""
    found = _package(checkout)
    if not found:
        return None
    tool, setup, scripts = found
    checks: dict[str, list[str]] = {}
    for check, candidates in _SCRIPTS.items():
        # npm init's placeholder test script always fails.
        script = next(
            (s for s in candidates if s in scripts and "no test specified" not in str(scripts[s])), None
        )
        if script:
            checks[check] = [tool, "run", script]
    return setup, checks


# Dev servers that listen on $PORT by themselves.
_READS_PORT = ("PORT", "next", "nuxt", "react-scripts")


def detect_preview(checkout: Path) -> tuple[list[str], bool] | None:
    """The command that runs the app for a preview, and whether it listens on $PORT."""
    found = _package(checkout)
    script = next((s for s in ("dev", "start") if s in found[2]), None) if found else None
    if not found or not script:
        return None
    return [found[0], "run", script], any(p in str(found[2][script]) for p in _READS_PORT)


def origin_of(checkout: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(checkout), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip()


# Where people usually keep clones, searched for an existing clone of the application.
_CODE_DIRS = ("Projects", "projects", "src", "code", "dev", "Developer", "repos", "git", "GitHub", "")


def find_clone(slug: str, home: Path) -> Path | None:
    name = slug.split("/")[1]
    for folder in _CODE_DIRS:
        path = home / folder / name
        if (path / ".git").exists() and (parse_repo(origin_of(path)) or "").lower() == slug.lower():
            return path
    return None


# --------------------------------------------------------------------------- talking to people and systems


class Prompter(Protocol):
    def ask(self, question: str, default: str = "") -> str: ...
    def secret(self, question: str) -> str: ...
    def say(self, text: str = "") -> None: ...


class TerminalPrompter:
    def ask(self, question: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        return input(f"{question}{shown}: ").strip() or default

    def secret(self, question: str) -> str:
        return getpass.getpass(f"{question}: ").strip()

    def say(self, text: str = "") -> None:
        print(text, flush=True)


def confirm(io: Prompter, question: str, default: bool = True) -> bool:
    while True:
        answer = io.ask(f"{question} ({'Y/n' if default else 'y/N'})").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


class SetupJira(JiraPort, Protocol):
    async def find_users(self, query: str) -> list[JiraUser]: ...
    async def projects(self) -> list[tuple[str, str]]: ...
    async def issue_type_statuses(self, project_key: str) -> dict[str, set[str]]: ...


class SetupGitHub(Protocol):
    async def viewer_login(self) -> str: ...
    async def repo(self) -> RepoInfo: ...
    async def branch_protection(self, branch: str) -> BranchProtection | None: ...
    async def api(self, path: str) -> Any: ...


def _jira_client(cfg: Config, credentials: JiraCredentials) -> SetupJira:
    from delivery.jira import JiraClient

    return JiraClient(cfg, credentials=credentials)


def _gh_client(slug: str) -> SetupGitHub:
    from delivery.github import GhClient

    return GhClient(slug)


def _interactive(argv: list[str]) -> int:
    """Run a command the person answers themselves (a sign-in, a clone, an install)."""
    try:
        return subprocess.run(argv, check=False).returncode
    except OSError:
        return 127


def _quiet(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return r.returncode, r.stdout


async def _claude_auth(executable: str) -> tuple[bool, list[str]]:
    from delivery.claude import auth_report

    report = await auth_report(executable, Path.home())
    return report.ok, list(report.problems)


async def _figma_user(token: str) -> str | None:
    from delivery.figma import FigmaClient

    client = FigmaClient(token)
    try:
        me = await client.me()
    except IntegrationError:
        return None
    finally:
        await client.close()
    return str(me.get("handle") or me.get("email") or "your Figma account")


@dataclass
class SetupDeps:
    io: Prompter
    jira: Callable[[Config, JiraCredentials], SetupJira] = _jira_client
    github: Callable[[str], SetupGitHub] = _gh_client
    run: Callable[[list[str]], int] = _interactive
    capture: Callable[[list[str]], tuple[int, str]] = _quiet
    which: Callable[[str], str | None] = shutil.which
    keychain: bool = field(default_factory=keychain_available)
    keychain_read: Callable[[str, str], str | None] = keychain_read
    keychain_write: Callable[[str, str, str, re.Pattern[str]], None] = keychain_write
    claude_auth: Callable[[str], Awaitable[tuple[bool, list[str]]]] = _claude_auth
    figma_user: Callable[[str], Awaitable[str | None]] = _figma_user
    home: Path = field(default_factory=Path.home)
    macos: bool = field(default_factory=lambda: platform.system() == "Darwin")


@dataclass
class SetupResult:
    path: Path
    notes: list[str]  # what is left for the person to do


# --------------------------------------------------------------------------- the questions


class Wizard:
    def __init__(self, path: Path, deps: SetupDeps, project: Path | None = None) -> None:
        self.path = path
        self.d = deps
        self.io = deps.io
        self.existing = path.exists()
        self.original = path.read_text() if self.existing else ""
        named = _project_of(path, self.original) if self.original else None
        # The team's shared project file: its settings are not asked again.
        self.project = project or named
        self.shared: dict[str, Any] = {}
        if self.project and self.project.is_file():
            with contextlib.suppress(tomllib.TOMLDecodeError, OSError):
                self.shared = tomllib.loads(self.project.read_text())
        if self.original:
            self.text = self.original
            if self.project and named is None:
                self.text = set_top(self.text, "project", str(self.project))
        elif self.project:
            self.text = PERSONAL_TEMPLATE.format(project=toml_value(str(self.project)))
        else:
            self.text = template_text()
        self.loop = asyncio.new_event_loop()
        self.jira: SetupJira | None = None
        self.me = JiraUser("", "")
        self.project_hint = ""
        self.gh_login = ""
        self.saved = False
        self.notes: list[str] = []

    # ---- plumbing
    def wait(self, call: Awaitable[T]) -> T:
        return self.loop.run_until_complete(call)

    def _merged(self, text: str) -> dict[str, Any]:
        """What the config means: the project file's settings, then this file's on top."""
        data = tomllib.loads(text)
        data.pop("project", None)
        return merge_tables(self.shared, data) if self.shared else data

    def team(self, dotted: str) -> bool:
        """Whether the team's project file sets this (so it is not asked)."""
        data: Any = self.shared
        for part in dotted.split("."):
            if not isinstance(data, dict) or part not in data:
                return False
            data = data[part]
        return True

    def get(self, dotted: str) -> Any:
        data: Any = self._merged(self.text)
        for part in dotted.split("."):
            if not isinstance(data, dict) or part not in data:
                return None
            data = data[part]
        return data

    def current(self, dotted: str) -> str:
        """The existing answer, worth offering as the default (never a template example)."""
        value = self.get(dotted) if self.existing else None
        return value if isinstance(value, str) and not is_placeholder(value) else ""

    def set(self, table: str, key: str, value: object) -> None:
        self.text = set_value(self.text, table, key, value)

    def _validate(self, text: str) -> list[str]:
        try:
            Config.model_validate(self._merged(text), context={"base_dir": str(self.path.parent)})
        except ValidationError as exc:
            return [_format_error(dict(e)) for e in exc.errors()]
        except tomllib.TOMLDecodeError as exc:
            return [f"invalid TOML: {exc}"]
        return []

    def config(self) -> Config:
        return Config.model_validate(self._merged(self.text), context={"base_dir": str(self.path.parent)})

    def problem(self, table: str, key: str, value: object) -> str | None:
        """Why this answer would make the config invalid, or None."""
        problems = self._validate(set_value(self.text, table, key, value))
        return "; ".join(problems) or None

    def ask_until(self, question: str, default: str, check: Callable[[str], str | None]) -> str:
        while True:
            answer = self.io.ask(question, default)
            problem = check(answer)
            if not problem:
                return answer
            self.io.say(f"  {problem}")

    def note(self, todo: str) -> None:
        if todo not in self.notes:
            self.notes.append(todo)

    # ---- the run
    def run(self) -> SetupResult | None:
        try:
            return self._run()
        finally:
            if self.jira:
                with contextlib.suppress(Exception):
                    self.wait(self.jira.close())
            self.loop.close()

    def _run(self) -> SetupResult | None:
        io = self.io
        if self.project:
            if not self.shared:
                io.say(f"Cannot read the project file {tilde(self.project)}.")
                return None
            personal = personal_keys_in(self.shared)
            if personal:
                io.say(f"{tilde(self.project)} holds personal settings ({', '.join(personal)});")
                io.say("a shared project file must not. Take them out, then run setup again.")
                return None
        problems = self._validate(self.text)
        if problems:
            io.say(f"{tilde(self.path)} has problems setup cannot work around:")
            for p in problems:
                io.say(f"  - {p}")
            io.say("Fix them in the file, or move it aside to start again.")
            return None
        io.say(f"Delivery setup. A few questions, then {tilde(self.path)} is written for you.")
        if self.project:
            io.say(f"The team's settings come from {tilde(self.project)}; only yours are asked.")
        io.say("Press Enter to accept the answer in [brackets]. Ctrl-C stops; nothing is saved till the end.")
        try:
            self.jira_signin()
            self.jira_project()
            self.approvers()
            self.github()
            self.checks()
            self.claude()
            self.figma()
            self.this_machine()
            if not self.save():
                return None
            self.map_workflow()
        except (KeyboardInterrupt, EOFError):
            io.say("")
            io.say(
                "Setup stopped. "
                + (f"{tilde(self.path)} has your answers." if self.saved else "Nothing was saved.")
            )
            return None
        return SetupResult(self.path, self.notes)

    # ---- Jira
    def jira_signin(self) -> None:
        io = self.io
        io.say("\nJira")
        site_default, email_default = self.current("jira.base_url"), self.current("jira.email")
        team_site = str(self.get("jira.base_url")) if self.team("jira.base_url") else ""
        if team_site:
            io.say(f"  Site {team_site} (the team's)")
        while True:
            site: str | None
            if team_site:
                site, hint = team_site, ""
            else:
                answer = io.ask("Jira site (its address, or paste any link from it)", site_default)
                site, hint = parse_site(answer)
            if not site:
                io.say("  That is not a Jira address; it looks like https://your-company.atlassian.net")
                continue
            email = self.ask_until(
                "Your Atlassian account email",
                email_default,
                lambda a: None if EMAIL.match(a) else "That is not an email address.",
            )
            if not team_site:
                self.set("jira", "base_url", site)
            self.set("jira", "email", email)
            if self.d.keychain and not self.get("jira.token_keychain_service"):
                self.set("jira", "token_keychain_service", "delivery-jira")
            if self.jira_token(email):
                self.project_hint = hint
                return
            site_default, email_default = site, email
            io.say("  Check the site and email, then try again (Ctrl-C stops).")

    def jira_token(self, email: str) -> bool:
        io = self.io
        token_env = str(self.get("jira.token_env") or "JIRA_API_TOKEN")
        service = self.get("jira.token_keychain_service")
        stored: list[tuple[str, str]] = []
        if os.environ.get(token_env):
            stored.append((os.environ[token_env], f"the token in ${token_env}"))
        if self.d.keychain and service and (token := self.d.keychain_read(service, email)):
            stored.append((token, "the token in your Keychain"))
        for token, where in stored:
            if self.jira_connect(email, token, where):
                io.say(f"  Signed in to Jira as {self.me.display_name}, with {where}.")
                return True
        io.say(f"  Create a Jira API token at {TOKEN_PAGE}")
        io.say("  (Create API token, any name, then Copy.)")
        token = io.secret("  Paste the token (nothing is shown), then Enter")
        if not TOKEN_SHAPE.match(token):
            io.say(f"  That does not look like an API token ({len(token)} characters).")
            return False
        if not self.jira_connect(email, token, "the token just pasted"):
            return False
        io.say(f"  Signed in to Jira as {self.me.display_name}.")
        if self.d.keychain and service:
            try:
                self.d.keychain_write(service, email, token, TOKEN_SHAPE)
            except (OSError, ValueError) as exc:
                io.say(f"  Storing it in your Keychain failed: {exc}")
                self.note("Store your Jira token: delivery credentials set")
            else:
                io.say("  Stored it in your login Keychain; it never goes in the config file.")
        else:
            io.say("  There is no Keychain here. In the shell that runs the coordinator, run")
            io.say(f"    read -rs {token_env} && export {token_env}")
            io.say("  and paste the token there.")
            self.note(f"Export the Jira token first: read -rs {token_env} && export {token_env}")
        return True

    def jira_connect(self, email: str, token: str, where: str) -> bool:
        client = self.d.jira(self.config(), JiraCredentials(email, token, where))
        try:
            me = self.wait(client.myself())
        except AuthError as exc:
            self.io.say(
                f"  Jira refused {where} for {email} ({exc.status}): check the email, and that the "
                "token was copied in full and has not been revoked."
            )
            self.wait(client.close())
            return False
        except IntegrationError as exc:
            self.io.say(f"  Could not reach Jira: {exc}")
            self.wait(client.close())
            return False
        if self.jira:
            self.wait(self.jira.close())
        self.jira, self.me = client, me
        self.set("identity", "developer_jira_account_id", me.account_id)
        return True

    def jira_project(self) -> None:
        io, jira = self.io, self.jira
        assert jira
        if self.team("jira.project_key"):
            kinds = ", ".join(self.get("jira.supported_issue_types") or [])
            io.say(f"  Project {self.get('jira.project_key')}, issue types {kinds} (the team's)")
            return
        default = self.project_hint or self.current("jira.project_key")
        if not default:
            try:
                projects = self.wait(jira.projects())
            except IntegrationError:
                projects = []
            if len(projects) == 1:
                default = projects[0][0]
            elif projects:
                shown = ", ".join(f"{k} ({n})" for k, n in projects[:10])
                io.say(f"  Projects you can see: {shown}{', …' if len(projects) > 10 else ''}")
        while True:
            key = io.ask("Jira project key", default).upper()
            if not PROJECT_KEY.match(key):
                io.say("  A project key is a short code in capitals, like SDLC.")
                continue
            try:
                statuses = self.wait(jira.project_statuses(key))
                types = self.wait(jira.issue_type_statuses(key))
            except NotFound:
                io.say(f"  There is no project {key} that you can see.")
                continue
            except IntegrationError as exc:
                io.say(f"  Could not read project {key}: {exc}")
                continue
            break
        self.set("jira", "project_key", key)

        names = {s.id: s.name.strip().lower() for s in statuses}
        with_flow = [t for t, ids in types.items() if "ready for refinement" in {names.get(i) for i in ids}]
        if with_flow:
            io.say(f"  Issue types with the delivery workflow: {', '.join(with_flow)}")
        else:
            io.say(
                f"  No issue type in {key} has the delivery statuses yet; your Jira admin sets them up "
                "with docs/jira-workflow-setup.md."
            )
        existing = self.get("jira.supported_issue_types") if self.existing else None
        default_types = existing or with_flow or [t for t in ("Story", "Bug") if t in types] or list(types)
        by_lower = {t.lower(): t for t in types}
        while True:
            picked = split_list(io.ask("Issue types it picks up", ", ".join(default_types)))
            if picked and all(p.lower() in by_lower for p in picked):
                break
            io.say(f"  Choose from: {', '.join(types)}")
        self.set("jira", "supported_issue_types", [by_lower[p.lower()] for p in picked])

    def approvers(self) -> None:
        io = self.io
        if self.team("approvals.jira_account_ids"):
            ids = self.get("approvals.jira_account_ids") or []
            names = ", ".join(self.person_name(a) for a in ids) or "anyone"
            io.say(f"  Approvers: {names} (the team's)")
            return
        io.say(
            "  Approvers sign off specifications, plans, acceptance and releases in Jira. 'anyone' lets "
            "anyone who can comment on and move the ticket decide, so nothing waits for one person."
        )
        ids = self.get("approvals.jira_account_ids") if self.existing else None
        current = [a for a in ids or [] if not is_placeholder(a)]
        others = [a for a in current if a != self.me.account_id]
        if others:
            io.say("  Now: " + ", ".join(self.person_name(a) for a in current))
        default = ", ".join("me" if a == self.me.account_id else a for a in current) or "anyone"
        while True:
            entries = split_list(
                io.ask("Approvers (names, emails, 'me' or 'anyone', comma-separated)", default)
            )
            if [e.lower() for e in entries] == ["anyone"]:
                io.say("  Approvers: anyone")
                self.set("approvals", "jira_account_ids", [])
                return
            people = [self.find_person(e) for e in entries]
            found = [p for p in people if p]
            if entries and len(found) == len(entries):
                break
        accounts = list(dict.fromkeys(p.account_id for p in found))
        io.say("  Approvers: " + ", ".join(dict.fromkeys(p.display_name for p in found)))
        if self.me.account_id in accounts:
            io.say("  You approve your own work: fine while trying it out alone (doctor will warn).")
        self.set("approvals", "jira_account_ids", accounts)

    def person_name(self, account_id: str) -> str:
        assert self.jira
        try:
            user = self.wait(self.jira.user(account_id))
        except IntegrationError:
            user = None
        return user.display_name if user and user.display_name else account_id

    def find_person(self, entry: str) -> JiraUser | None:
        io, jira = self.io, self.jira
        assert jira
        if entry.lower() in ("me", "myself"):
            return self.me
        if ACCOUNT_ID.match(entry):
            try:
                user = self.wait(jira.user(entry))
            except IntegrationError:
                user = None
            if user:
                return user
        try:
            matches = self.wait(jira.find_users(entry))[:8]
        except IntegrationError as exc:
            io.say(f"  Could not search Jira for {entry!r}: {exc}")
            return None
        if not matches:
            io.say(f"  Nobody in Jira matches {entry!r}.")
            return None
        if len(matches) == 1:
            return matches[0]
        io.say(f"  Several people match {entry!r}:")
        for n, user in enumerate(matches, 1):
            io.say(f"    {n}. {user.display_name}")
        pick = io.ask("  Which one (number)")
        return matches[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(matches) else None

    # ---- GitHub
    def github(self) -> None:
        io, d = self.io, self.d
        io.say("\nGitHub")
        if not d.which("gh"):
            io.say("  GitHub CLI (gh) is not installed, so nothing can be checked on GitHub.")
            self.note("Install GitHub CLI and sign in (brew install gh, then gh auth login)")
        else:
            if d.capture(["gh", "auth", "status"])[0] != 0 and confirm(
                io, "  You are not signed in to GitHub CLI. Sign in now (gh auth login)?"
            ):
                d.run(["gh", "auth", "login"])
            try:
                self.gh_login = self.wait(d.github("").viewer_login())
                io.say(f"  Signed in to GitHub as {self.gh_login}.")
            except IntegrationError:
                io.say("  GitHub CLI is not signed in, so nothing can be checked on GitHub.")
                self.note("Sign in to GitHub CLI: gh auth login")
        if self.team("repository.url"):
            team_slug = parse_repo(str(self.get("repository.url"))) or ""
            base = self.get("repository.base_branch") or "main"
            io.say(f"  Repository {team_slug}, PRs into {base} (the team's)")
            self.clone(team_slug, self.existing)
            if not self.team("approvals.github_logins"):
                self.reviewers()
            return
        current = parse_repo(self.current("repository.url")) or ""
        info: RepoInfo | None = None
        while True:
            slug = parse_repo(io.ask("The application's GitHub repository (owner/name or link)", current))
            if not slug:
                io.say("  Give it as owner/name, for example acme/shop-web.")
                continue
            if self.gh_login:
                try:
                    info = self.wait(d.github(slug).repo())
                except NotFound:
                    io.say(f"  {slug} does not exist, or {self.gh_login} cannot see it.")
                    continue
                except IntegrationError as exc:
                    io.say(f"  Could not check {slug}: {exc}")
            break
        self.set("repository", "url", f"https://github.com/{slug}.git")
        if info and not info.can_push:
            io.say(f"  {self.gh_login} cannot push to {slug}; ask its owner for write access.")
            self.note(f"Get write access to {slug}")
        same = slug.lower() == current.lower()
        default_base = (self.current("repository.base_branch") if same else "") or (
            info.default_branch if info else "main"
        )
        base = self.ask_until(
            "Branch that PRs go into", default_base, lambda a: self.problem("repository", "base_branch", a)
        )
        self.set("repository", "base_branch", base)
        self.clone(slug, same)
        self.reviewers()
        self.required_checks(slug, base)

    def clone(self, slug: str, same_repo: bool) -> None:
        io, home = self.io, self.d.home
        found = find_clone(slug, home)
        projects = "Projects" if (home / "Projects").is_dir() else "src"
        default = (self.current("repository.checkout_path") if same_repo else "") or (
            tilde(found, home) if found else f"~/{projects}/{slug.split('/')[1]}"
        )
        while True:
            answer = io.ask("Your clone of it on this laptop (the coordinator never edits it)", default)
            path = Path(answer).expanduser().absolute()
            problem = self.problem("repository", "checkout_path", str(path))
            if problem:
                io.say(f"  {problem}")
                continue
            if (path / ".git").exists():
                origin = origin_of(path)
                if (parse_repo(origin) or "").lower() != slug.lower():
                    io.say(f"  {answer} is a clone of {origin or 'something else'}, not {slug}.")
                    continue
            elif path.exists() and any(path.iterdir()):
                io.say(f"  {answer} already has files in it and is not a Git clone.")
                continue
            elif self.gh_login and confirm(io, f"  Nothing at {answer} yet. Clone {slug} there now?"):
                if self.d.run(["gh", "repo", "clone", slug, str(path)]) != 0:
                    io.say("  The clone failed (see above).")
                    continue
            else:
                self.note(f"Clone the application: gh repo clone {slug} {answer}")
            break
        self.set("repository", "checkout_path", tilde(path, home))

    def reviewers(self) -> None:
        io = self.io
        logins = self.get("approvals.github_logins") if self.existing else None
        current = [g for g in logins or [] if not is_placeholder(g)]
        mine = f" (not you, {self.gh_login})" if self.gh_login else ""
        while True:
            answer = io.ask(
                f"GitHub usernames whose PR approval counts{mine}, or anyone", ", ".join(current) or "anyone"
            )
            picked = [] if answer.lower() == "anyone" else split_list(answer)
            if self.gh_login.lower() in (p.lower() for p in picked):
                io.say("  GitHub does not let you approve PRs you opened; name someone else.")
                continue
            unknown = [p for p in picked if not self.github_user_exists(p)]
            if unknown:
                io.say(f"  There is no GitHub user {', '.join(unknown)}.")
                continue
            break
        if self.get("approvals.require_independent_github_review") is False:
            io.say("  (This config does not require a GitHub approval: require_independent_github_review.)")
        elif not picked:
            io.say("  Anyone on GitHub other than you can approve the code.")
        self.set("approvals", "github_logins", picked)

    def github_user_exists(self, login: str) -> bool:
        if not self.gh_login:
            return True  # cannot check
        try:
            self.wait(self.d.github("").api(f"users/{login}"))
        except NotFound:
            return False
        except IntegrationError:
            return True
        return True

    def required_checks(self, slug: str, base: str) -> None:
        if not self.gh_login:
            return
        try:
            protection = self.wait(self.d.github(slug).branch_protection(base))
        except IntegrationError:
            return
        if protection is None:
            self.io.say(f"  {base} is not protected yet; doctor says what to turn on.")
            return
        if protection.source.startswith("unreadable"):
            return
        checks = list(protection.required_checks)
        if checks:
            self.io.say(f"  CI checks {base} requires: {', '.join(checks)}")
        else:
            self.io.say(f"  {base} requires no CI checks, so code review cannot confirm CI passed.")
        self.set("checks.ci", "required_names", checks)

    # ---- the application's checks
    def checks(self) -> None:
        io = self.io
        if self.team("checks.commands"):
            return
        io.say("\nChecks")
        checkout = Path(str(self.get("repository.checkout_path"))).expanduser()
        found = detect_checks(checkout)
        if not found or not found[1]:
            io.say(f"  No package.json scripts in {tilde(checkout)} to take the checks from.")
            self.note(f"List the application's checks under [checks] in {tilde(self.path)}")
            return
        setup, commands = found
        io.say("  From package.json, the coordinator would run these in every fresh worktree:")
        io.say(f"    {'setup':<10} {' '.join(setup)}")
        for name, argv in commands.items():
            io.say(f"    {name:<10} {' '.join(argv)}")
        if confirm(io, "  Use these?"):
            self.set("checks", "setup", setup)
            self.text = set_table(self.text, "checks.commands", commands)
        else:
            self.note(f"List the application's checks under [checks] in {tilde(self.path)}")

    # ---- Claude
    def claude(self) -> None:
        io, d = self.io, self.d
        io.say("\nClaude Code")
        exe = str(self.get("claude.executable") or "claude")
        if not d.which(exe):
            io.say(f"  Claude Code is not installed. Install it with: {CLAUDE_INSTALL}")
            self.note(f"Install Claude Code ({CLAUDE_INSTALL}), then sign in: claude auth login")
        else:
            ok, problems = self.wait(d.claude_auth(exe))
            question = "  Claude Code is not signed in. Sign in with your subscription now?"
            if any("not logged in" in p for p in problems) and confirm(io, question):
                d.run([exe, "auth", "login"])
                ok, problems = self.wait(d.claude_auth(exe))
            if ok:
                io.say("  Signed in with your Claude subscription.")
            else:
                for p in problems:
                    io.say(f"  {p}")
                self.note("Sign in to Claude Code with your subscription: claude auth login")
        if self.project:
            pass  # the team's project file chooses the models
        else:
            model = self.ask_until(
                "Claude model for every stage: opus, sonnet, or default for your plan's",
                str(self.get("claude.model") or "default"),
                lambda a: None if MODEL_NAME.match(a) else "That is not a model name.",
            )
            if model == "default":
                self.text = unset_value(self.text, "claude", "model")
            else:
                self.set("claude", "model", model)
        if not d.macos:
            return
        watching = bool(self.get("claude.interactive.enabled"))
        io.say("  Each Claude session can open in a Terminal window that you can watch and type into.")
        if confirm(io, "  Open a window for each session?", watching):
            needs_tmux = not d.which("tmux") and d.which("brew")
            if needs_tmux and confirm(io, "  That needs tmux. Install it now (brew install tmux)?"):
                d.run(["brew", "install", "tmux"])
            if not d.which("tmux"):
                self.note("Install tmux for session windows: brew install tmux")
            self.set("claude.interactive", "enabled", True)
            self.preview()
        elif watching:
            self.set("claude.interactive", "enabled", False)

    def preview(self) -> None:
        """Offer to run the app from each finished development session (needs the windows)."""
        io = self.io
        if self.team("preview.command"):
            return
        current = self.get("preview.command") or []
        found = detect_preview(Path(str(self.get("repository.checkout_path"))).expanduser())
        command = current or (found[0] if found else [])
        if not command:
            return
        shown = " ".join(command)
        if confirm(io, f"  When development finishes, run the app ({shown}) and open it in your browser?"):
            self.set("preview", "command", command)
            if found and command == found[0] and not found[1]:
                io.say("  Each preview gets its own port in $PORT; make the app's dev script listen on it,")
                io.say("  for example: vite --port ${PORT:-5173}")
                self.note("Make the app's dev script listen on $PORT for previews")
        elif current:
            self.set("preview", "command", [])

    # ---- Figma
    def figma(self) -> None:
        io, d = self.io, self.d
        if not d.keychain or self.get("figma.enabled") is False:
            return
        service = str(self.get("figma.token_keychain_service") or "delivery-figma")
        account = str(self.get("figma.token_account") or "figma")
        stored = d.keychain_read(service, account)
        if stored and (who := self.wait(d.figma_user(stored))):
            io.say(f"\nFigma: using the token in your Keychain ({who}).")
            return
        io.say("\nFigma (optional)")
        if not confirm(io, "  Do tickets link Figma designs? Add a token so Claude can see them?", False):
            return
        io.say("  In Figma: Settings > Security > Personal access tokens > Generate new token, with only")
        io.say("  File content: Read-only and Current user: Read.")
        token = io.secret("  Paste the token (nothing is shown), then Enter")
        who = self.wait(d.figma_user(token)) if FIGMA_TOKEN_SHAPE.match(token) else None
        if not who:
            io.say("  Figma did not accept that token. Add one later with: delivery credentials set figma")
            self.note("Add a Figma token: delivery credentials set figma")
            return
        try:
            d.keychain_write(service, account, token, FIGMA_TOKEN_SHAPE)
        except (OSError, ValueError) as exc:
            io.say(f"  Storing it in your Keychain failed: {exc}")
            return
        io.say(f"  Figma accepted it ({who}); stored in your login Keychain.")

    # ---- this laptop
    def this_machine(self) -> None:
        if is_placeholder(self.get("identity.worker_id")):
            self.set("identity", "worker_id", machine_name())
        plugin = bundled_plugin_path()
        if plugin:
            self.set("claude", "plugin_path", str(plugin))

    # ---- saving
    def save(self) -> bool:
        io = self.io
        problems = self._validate(self.text)
        if problems:
            io.say("\nThese answers do not make a valid config, so nothing was saved:")
            for p in problems:
                io.say(f"  - {p}")
            return False
        if not confirm(io, f"\nSave to {tilde(self.path)}?"):
            io.say("Nothing saved.")
            return False
        self.write()
        backup = f" (the previous version is in {self.path.name}.bak)" if self.original else ""
        io.say(f"Saved {tilde(self.path)}{backup}.")
        return True

    def write(self) -> None:
        if self.original and not self.saved:
            backup = self.path.with_name(self.path.name + ".bak")
            backup.write_text(self.original)
            backup.chmod(0o600)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.text)
        self.path.chmod(0o600)
        self.saved = True

    def map_workflow(self) -> None:
        from delivery.doctor import inspect_workflow

        io, jira = self.io, self.jira
        assert jira
        io.say("\nJira workflow")
        cfg = self.config()
        if self.project and not cfg.workflow.missing_statuses():
            io.say(f"  Mapped in the team's project file ({len(cfg.workflow.statuses)} statuses).")
            return
        try:
            wf = self.wait(inspect_workflow(cfg, jira))
        except IntegrationError as exc:
            io.say(f"  Could not read the workflow: {exc}")
            self.note("Map the workflow: delivery workflow inspect")
            return
        mapped: dict[str, Any] = {s.value: wf.resolved[s.value] for s in Status if s.value in wf.resolved}
        self.text = set_table(self.text, "workflow.statuses", mapped)
        if wf.resume_field:
            self.set("jira.fields", "resume_stage", wf.resume_field)
        self.write()
        project = cfg.jira.project_key
        io.say(f"  Found {len(mapped)} of {len(Status)} delivery statuses in {project}.")
        if wf.missing:
            io.say(f"  Missing: {', '.join(wf.missing)}")
        for name, ids in wf.ambiguous.items():
            io.say(f"  Several statuses are called {name} ({', '.join(ids)}); rename all but one.")
        if wf.missing or wf.ambiguous:
            self.note(
                f"Ask your Jira admin to finish {project}'s workflow (docs/jira-workflow-setup.md), "
                "then run delivery setup again"
            )
        if wf.problems:
            io.say(f"  {len(wf.problems)} statuses have missing or extra transitions.")
            self.note("See which Jira transitions need fixing: delivery workflow inspect")


def run_setup(path: Path, deps: SetupDeps, project: Path | None = None) -> SetupResult | None:
    """Ask the questions and write ``path``. None when stopped or not saved.

    A new config offers the team project files kept in this installation's ``projects/``.
    """
    path = path.expanduser().absolute()
    if project is None and not path.exists():
        found = project_candidates()
        try:
            if len(found) == 1 and confirm(
                deps.io, f"Your team's settings are in {tilde(found[0])}. Use them (only your own are asked)?"
            ):
                project = found[0]
            elif len(found) > 1:
                deps.io.say("Team project files: " + ", ".join(p.stem for p in found))
                pick = deps.io.ask("Which project (Enter for none, to answer everything yourself)", "")
                project = next((p for p in found if p.stem.lower() == pick.strip().lower()), None)
        except (KeyboardInterrupt, EOFError):
            deps.io.say("\nSetup stopped. Nothing was saved.")
            return None
    return Wizard(path, deps, project).run()
