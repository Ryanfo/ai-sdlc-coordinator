from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import APPROVER, DEV, STATUS_IDS
from delivery import cli
from delivery.config import load_config, template_text
from delivery.credentials import JiraCredentials
from delivery.doctor import Report, check_config
from delivery.jira import JiraClient
from delivery.ports import AuthError, BranchProtection, JiraUser, NotFound, RepoInfo
from delivery.setup import (
    SetupDeps,
    detect_checks,
    detect_preview,
    parse_repo,
    parse_site,
    run_setup,
    set_table,
    set_value,
    unset_value,
)
from delivery.workflow import Status
from fakes.jira import FakeJira

TOKEN = "ATATT3xFfGF0" + "a" * 48

# --------------------------------------------------------------------------- editing the TOML text


def test_set_value_replaces_keeps_comments_and_round_trips() -> None:
    text = set_value(template_text(), "jira", "project_key", "SDLC")
    assert tomllib.loads(text)["jira"]["project_key"] == "SDLC"
    assert "# Set to an opt-in label" in text
    assert text.count("project_key") == 1


def test_set_value_keeps_inline_comments() -> None:
    text = '[approvals]\ngithub_logins = []  # add the reviewer\nother = "a # b"  # real one\n'
    text = set_value(text, "approvals", "github_logins", ["sam"])
    text = set_value(text, "approvals", "other", "c")
    assert 'github_logins = ["sam"]  # add the reviewer\nother = "c"  # real one\n' in text
    text = set_value(template_text(), "claude.interactive", "window", "iTerm")
    assert 'window = "iTerm"       # or "iTerm", or "none"' in text
    assert tomllib.loads(text)["claude"]["interactive"] == {"window": "iTerm"}


def test_set_value_switches_on_commented_examples() -> None:
    text = set_value(template_text(), "claude", "model", "opus")
    assert tomllib.loads(text)["claude"]["model"] == "opus"
    assert '# model = "sonnet"' not in text
    text = set_value(text, "claude.interactive", "enabled", True)
    data = tomllib.loads(text)
    assert data["claude"]["interactive"] == {"enabled": True}
    assert data["claude"]["model"] == "opus"
    assert "# window = " in text  # the rest of the example stays a comment


def test_set_value_adds_missing_keys_and_tables() -> None:
    text = set_value("[jira]\nbase_url = 'x'\n\n[other]\n", "jira", "email", "a@b.co")
    assert tomllib.loads(text)["jira"] == {"base_url": "x", "email": "a@b.co"}
    assert text.index("email") < text.index("[other]")
    text = set_value(text, "jira.fields", "resume_stage", "customfield_1")
    assert tomllib.loads(text)["jira"]["fields"] == {"resume_stage": "customfield_1"}


def test_set_value_replaces_a_multi_line_array() -> None:
    text = '[checks.ci]\nrequired_names = [\n  "lint",  # first\n  "unit",\n]\nexpected_producer = "x"\n'
    out = tomllib.loads(set_value(text, "checks.ci", "required_names", ["build"]))
    assert out["checks"]["ci"] == {"required_names": ["build"], "expected_producer": "x"}


def test_set_table_and_unset_value() -> None:
    text = set_table(template_text(), "checks.commands", {"unit": ["npm", "test"]})
    assert tomllib.loads(text)["checks"]["commands"] == {"unit": ["npm", "test"]}
    assert "# Coordinator-run gates." in text
    text = set_table(text, "workflow.statuses", {"backlog": "1", "done": "2"})
    assert tomllib.loads(text)["workflow"]["statuses"] == {"backlog": "1", "done": "2"}
    text = unset_value(set_value(text, "claude", "model", "opus"), "claude", "model")
    assert "model" not in tomllib.loads(text)["claude"]


def test_values_are_escaped() -> None:
    text = set_value(template_text(), "identity", "worker_id", 'a"b\\c')
    assert tomllib.loads(text)["identity"]["worker_id"] == 'a"b\\c'


# --------------------------------------------------------------------------- reading answers


@pytest.mark.parametrize(
    ("answer", "site", "key"),
    [
        ("acme", "https://acme.atlassian.net", ""),
        ("acme.atlassian.net", "https://acme.atlassian.net", ""),
        (
            "https://acme.atlassian.net/jira/software/projects/SDLC/boards/1",
            "https://acme.atlassian.net",
            "SDLC",
        ),
        ("https://Acme.atlassian.net/browse/SDLC-12?focus=1", "https://acme.atlassian.net", "SDLC"),
        ("not a site", None, ""),
        ("", None, ""),
    ],
)
def test_parse_site(answer: str, site: str | None, key: str) -> None:
    assert parse_site(answer) == (site, key)


@pytest.mark.parametrize(
    ("answer", "slug"),
    [
        ("acme/shop", "acme/shop"),
        ("https://github.com/acme/shop", "acme/shop"),
        ("https://github.com/acme/shop.git", "acme/shop"),
        ("git@github.com:acme/shop.git", "acme/shop"),
        ("https://github.com/acme/shop/tree/main/src", "acme/shop"),
        ("shop", None),
        ("acme/sh op", None),
    ],
)
def test_parse_repo(answer: str, slug: str | None) -> None:
    assert parse_repo(answer) == slug


def test_detect_checks_from_package_json(tmp_path: Path) -> None:
    assert detect_checks(tmp_path) is None
    scripts = {
        "lint": "eslint .",
        "test": "vitest",
        "build": "vite build",
        "type-check": "tsc",
        "dev": "vite",
    }
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    setup, checks = detect_checks(tmp_path) or ([], {})
    assert setup == ["pnpm", "install", "--frozen-lockfile"]
    assert checks == {
        "lint": ["pnpm", "run", "lint"],
        "typecheck": ["pnpm", "run", "type-check"],
        "unit": ["pnpm", "run", "test"],
        "build": ["pnpm", "run", "build"],
    }
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified"'}})
    )
    assert detect_checks(tmp_path) == (["pnpm", "install", "--frozen-lockfile"], {})


# --------------------------------------------------------------------------- the wizard


class Script:
    """Answers each question in order, checking it is the question expected."""

    def __init__(self, answers: list[tuple[str, str | BaseException]]) -> None:
        self.answers = list(answers)
        self.said: list[str] = []

    def ask(self, question: str, default: str = "") -> str:
        assert self.answers, f"unexpected question: {question!r}"
        expected, answer = self.answers.pop(0)
        assert expected in question, f"expected a question about {expected!r}, got {question!r}"
        if isinstance(answer, BaseException):
            raise answer
        return answer or default

    def secret(self, question: str) -> str:
        return self.ask(question)

    def say(self, text: str = "") -> None:
        self.said.append(text)

    @property
    def output(self) -> str:
        return "\n".join(self.said)


@dataclass
class StubGitHub:
    slug: str
    users: set[str] = field(default_factory=lambda: {"dev-bot", "reviewer-1"})

    async def viewer_login(self) -> str:
        return "dev-bot"

    async def repo(self) -> RepoInfo:
        if self.slug != "acme/shop":
            raise NotFound("404", status=404)
        return RepoInfo("acme/shop", "private", "main", True, False)

    async def branch_protection(self, branch: str) -> BranchProtection | None:
        return BranchProtection(required_approving_reviews=1, required_checks=("unit", "lint"))

    async def api(self, path: str) -> Any:
        if path.removeprefix("users/") not in self.users:
            raise NotFound("404", status=404)
        return {}


class RefusingJira(FakeJira):
    async def myself(self) -> JiraUser:
        raise AuthError("Jira 401", status=401)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    return home


@pytest.fixture
def jira() -> FakeJira:
    fake = FakeJira(STATUS_IDS, me=DEV)
    fake.people = [JiraUser(APPROVER, "Priya Patel"), JiraUser("other-0001", "Sam Jones")]
    return fake


def fake_clone(argv: list[str]) -> int:
    """`gh repo clone acme/shop <path>`: a clone with a package.json."""
    assert argv[:3] == ["gh", "repo", "clone"]
    path = Path(argv[4])
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", f"https://github.com/{argv[3]}.git"], check=True
    )
    scripts = {"lint": "eslint .", "test": "vitest", "dev": "vite --port ${PORT:-5173}"}
    (path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (path / "package-lock.json").write_text("{}")
    return 0


def deps(io: Script, jira: FakeJira, keychain: dict[tuple[str, str], str], ran: list[list[str]]) -> SetupDeps:
    def run(argv: list[str]) -> int:
        ran.append(argv)
        return fake_clone(argv) if argv[:2] == ["gh", "repo"] else 0

    def write(service: str, account: str, token: str, shape: Any) -> None:
        keychain[(service, account)] = token

    tools = {"gh": "/usr/bin/gh"}
    return SetupDeps(
        io,
        jira=lambda cfg, creds: jira,
        github=lambda slug: StubGitHub(slug),
        run=run,
        capture=lambda argv: (0, ""),
        which=tools.get,
        keychain=True,
        keychain_read=lambda service, account: keychain.get((service, account)),
        keychain_write=write,
        macos=True,
    )


FIRST_RUN: list[tuple[str, str | BaseException]] = [
    ("Jira site", "https://example.atlassian.net/jira/software/projects/PILOT/boards/1"),
    ("email", "dev@example.com"),
    ("Paste the token", TOKEN),
    ("project key", ""),  # PILOT, from the pasted link
    ("Issue types", "story, bug"),
    ("Approvers", "priya"),
    ("GitHub repository", "acme/nope"),
    ("GitHub repository", "https://github.com/acme/shop"),
    ("Branch", ""),
    ("clone of it", ""),  # ~/src/shop
    ("Clone acme/shop there now", ""),
    ("usernames", "dev-bot"),
    ("usernames", "reviewer-1, ghost"),
    ("usernames", "reviewer-1"),
    ("Use these", ""),
    ("Claude model", "opus"),
    ("Open a window", ""),
    ("Figma", "n"),
    ("Save to", ""),
]


def test_setup_writes_a_working_config(home: Path, jira: FakeJira) -> None:
    io, keychain, ran = Script(FIRST_RUN), {}, []
    result = run_setup(home / "delivery.local.toml", deps(io, jira, keychain, ran))
    assert result is not None, io.output
    assert not io.answers

    path = home / "delivery.local.toml"
    cfg = load_config(path)
    assert cfg.jira.base_url == "https://example.atlassian.net"
    assert cfg.jira.email == "dev@example.com"
    assert cfg.jira.project_key == "PILOT"
    assert cfg.jira.supported_issue_types == ["Story", "Bug"]
    assert cfg.identity.developer_jira_account_id == DEV  # from the token, never asked
    assert cfg.approvals.jira_account_ids == [APPROVER]
    assert cfg.approvals.github_logins == ["reviewer-1"]
    assert cfg.repository.url == "https://github.com/acme/shop.git"
    assert cfg.repository.base_branch == "main"
    assert cfg.repository.checkout_path == home / "src" / "shop"
    assert cfg.checks.setup == ["npm", "ci"]
    assert cfg.checks.commands == {"lint": ["npm", "run", "lint"], "unit": ["npm", "run", "test"]}
    assert cfg.checks.ci.required_names == ["unit", "lint"]  # from branch protection
    assert cfg.claude.model == "opus"
    assert not cfg.claude.interactive.enabled
    assert cfg.workflow.missing_statuses() == []
    assert cfg.workflow.statuses[Status.BACKLOG] == STATUS_IDS[Status.BACKLOG]
    assert cfg.jira.fields.resume_stage == "customfield_10050"
    assert (cfg.claude.plugin_path / ".claude-plugin" / "plugin.json").exists()

    assert ["gh", "repo", "clone", "acme/shop", str(home / "src" / "shop")] in ran
    assert keychain == {("delivery-jira", "dev@example.com"): TOKEN}
    text = path.read_text()
    assert TOKEN not in text
    assert "# delivery local configuration" in text  # the template's comments stay
    assert path.stat().st_mode & 0o777 == 0o600
    report = Report()
    check_config(cfg, report)
    assert {c.name: c.level for c in report.checks}["placeholders"] == "ok"
    assert any("Install Claude Code" in n for n in result.notes)
    assert "GitHub does not let you approve" in io.output
    assert "There is no GitHub user ghost" in io.output


def test_session_windows_offer_the_app_preview(home: Path, jira: FakeJira) -> None:
    at = next(n for n, (q, _) in enumerate(FIRST_RUN) if q == "Open a window")
    answers = [
        *FIRST_RUN[:at],
        ("Open a window", "y"),
        ("run the app (npm run dev)", ""),
        *FIRST_RUN[at + 1 :],
    ]
    io = Script(answers)
    result = run_setup(home / "delivery.local.toml", deps(io, jira, {}, []))
    assert result is not None, io.output
    cfg = load_config(home / "delivery.local.toml")
    assert cfg.claude.interactive.enabled
    assert cfg.preview.command == ["npm", "run", "dev"]
    assert "Install tmux" in " ".join(result.notes)  # no tmux in the stub tools
    assert "listen on" not in io.output  # the dev script already uses $PORT


def test_detect_preview(tmp_path: Path) -> None:
    assert detect_preview(tmp_path) is None
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": "vite"}}))
    assert detect_preview(tmp_path) == (["npm", "run", "start"], False)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "next dev", "start": "x"}}))
    (tmp_path / "yarn.lock").write_text("")
    assert detect_preview(tmp_path) == (["yarn", "run", "dev"], True)


def test_setup_again_keeps_answers_and_hand_edits(home: Path, jira: FakeJira) -> None:
    keychain: dict[tuple[str, str], str] = {}
    assert run_setup(home / "delivery.local.toml", deps(Script(FIRST_RUN), jira, keychain, []))
    path = home / "delivery.local.toml"
    first = load_config(path)
    edited = path.read_text().replace("poll_seconds = 60", "poll_seconds = 30")
    path.write_text(edited)

    questions = [
        "Jira site", "email", "project key", "Issue types", "Approvers", "GitHub repository", "Branch",
        "clone of it", "usernames", "Use these", "Claude model", "Open a window", "Figma", "Save to",
    ]  # fmt: skip
    io = Script([(q, "") for q in questions])
    assert run_setup(path, deps(io, jira, keychain, [])) is not None, io.output
    assert "with the token in your Keychain" in io.output  # not asked for again
    again = load_config(path)
    assert again.runtime.poll_seconds == 30
    assert again.model_copy(update={"runtime": first.runtime}).digest() == first.digest()
    assert (home / "delivery.local.toml.bak").read_text() == edited


def test_ctrl_c_saves_nothing(home: Path, jira: FakeJira) -> None:
    io = Script([("Jira site", "example"), ("email", "dev@example.com"), ("Paste", KeyboardInterrupt())])
    assert run_setup(home / "delivery.local.toml", deps(io, jira, {}, [])) is None
    assert not (home / "delivery.local.toml").exists()
    assert "Nothing was saved" in io.output


def test_refused_token_asks_again(home: Path, jira: FakeJira) -> None:
    refusing = RefusingJira(STATUS_IDS, me=DEV)
    io = Script(
        [
            ("Jira site", "example"),
            ("email", "dev@example.com"),
            ("Paste the token", "x" * 10),  # not a token at all
            ("Jira site", ""),
            ("email", ""),
            ("Paste the token", "B" * 60),  # refused by Jira
            ("Jira site", ""),
            ("email", ""),
            ("Paste the token", KeyboardInterrupt()),
        ]
    )
    d = deps(io, jira, {}, [])
    d.jira = lambda cfg, creds: refusing if creds.token.startswith("B") else jira
    assert run_setup(home / "delivery.local.toml", d) is None
    assert "does not look like an API token" in io.output
    assert "Jira refused the token just pasted for dev@example.com (401)" in io.output


def test_setup_needs_a_terminal(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli.main(["setup", "--config", str(tmp_path / "c.toml")]) == cli.EXIT_CONFIG
    assert "run it in a terminal" in capsys.readouterr().err


# --------------------------------------------------------------------------- Jira lookups


async def test_jira_finds_people_and_projects(make_config: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/user/search":
            assert request.url.params["query"] == "priya"
            return httpx.Response(
                200,
                json=[
                    {"accountId": "557058:abc", "displayName": "Priya Patel", "accountType": "atlassian"},
                    {"accountId": "557058:bot", "displayName": "Priya bot", "accountType": "app"},
                    {"accountId": "557058:old", "displayName": "Priya Old", "active": False},
                ],
            )
        assert request.url.path == "/rest/api/3/project/search"
        return httpx.Response(200, json={"values": [{"key": "SDLC", "name": "Delivery"}]})

    client = JiraClient(
        make_config(),
        transport=httpx.MockTransport(handler),
        credentials=JiraCredentials("dev@example.com", TOKEN, "test"),
    )
    assert [u.display_name for u in await client.find_users("priya")] == ["Priya Patel"]
    assert await client.projects() == [("SDLC", "Delivery")]
    await client.close()
