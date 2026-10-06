"""The parts behind running unattended for a client team (see test_hardening for the flows)."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import ConfigFactory
from delivery import cleanup, comments
from delivery.alerts import Alerts
from delivery.claude import ClaudeStatus, classify
from delivery.doctor import Report, check_signing
from delivery.git import ManagedRepo
from delivery.github import GhClient
from delivery.interactive import _api_outcome
from delivery.linked import linked_tickets
from delivery.models import RunRecord, RunState, Stage, utcnow
from delivery.ownership import RepoLocks
from delivery.ports import AuthError, IntegrationError, IssueLink
from delivery.resources import short_of_room
from harness import make_world

INIT = json.dumps({"type": "system", "subtype": "init", "plugins": [{"name": "delivery"}]})


def _result(text: str) -> str:
    return json.dumps(
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text}
    )


@pytest.mark.parametrize(
    "text",
    [
        'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
        "API Error: 500 Internal server error",
        "API Error: Connection error.",
        "Request timed out.",
    ],
)
def test_api_outages_are_told_apart_from_real_failures(text: str) -> None:
    assert classify(f"{INIT}\n{_result(text)}", "", 1, Path("/p")).status is ClaudeStatus.UNAVAILABLE
    assert _api_outcome(text).status is ClaudeStatus.UNAVAILABLE


def test_other_failures_and_limits_keep_their_meaning() -> None:
    assert classify(f"{INIT}\n{_result('result error')}", "", 1, Path("/p")).status is ClaudeStatus.ERROR
    usage = _result("Claude usage limit reached. Your limit resets at 5pm.")
    assert classify(f"{INIT}\n{usage}", "", 1, Path("/p")).status is ClaudeStatus.USAGE_LIMIT
    ok = json.dumps({"type": "result", "subtype": "success", "result": "500 lines", "structured_output": {}})
    assert classify(f"{INIT}\n{ok}", "", 0, Path("/p")).status is ClaudeStatus.OK
    # A run that died while Claude Code was retrying an overloaded API counts as an outage.
    retry = json.dumps({"type": "system", "subtype": "api_retry", "error": "overloaded_error"})
    assert classify(f"{INIT}\n{retry}", "", 1, Path("/p")).status is ClaudeStatus.UNAVAILABLE


def test_room_for_new_sessions(tmp_path: Path) -> None:
    root = tmp_path / "not-yet" / "worktrees"
    assert short_of_room(root, 0, True, pressure=lambda: 1) is None
    assert short_of_room(root, 0, True, pressure=lambda: 2) is None, "a warning is not critical"
    assert short_of_room(root, 0, True, pressure=lambda: 4) == "macOS reports critical memory pressure"
    assert short_of_room(root, 0, False, pressure=lambda: 4) is None
    full = short_of_room(root, 10**6, False)
    assert full is not None and "runtime.min_free_disk_gb = 1000000" in full and str(tmp_path) in full


def _fake_gh(tmp_path: Path, failures: int, stderr: str) -> Path:
    counter = tmp_path / "calls"
    script = tmp_path / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        f"if n < {failures}:\n"
        f"    sys.stderr.write({stderr!r})\n"
        "    sys.exit(1)\n"
        'print(\'{"login": "dev"}\')\n'
    )
    script.chmod(0o755)
    return script


async def test_rate_limited_github_requests_are_tried_again(tmp_path: Path) -> None:
    limited = "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n"
    gh = GhClient("o/r", str(_fake_gh(tmp_path, 2, limited)), pauses=(0.01, 0.01))
    assert await gh.viewer_login() == "dev"
    assert (tmp_path / "calls").read_text() == "3"


async def test_a_rate_limit_that_lasts_is_retryable_not_an_auth_failure(tmp_path: Path) -> None:
    limited = "gh: You have exceeded a secondary rate limit. (HTTP 403)\n"
    gh = GhClient("o/r", str(_fake_gh(tmp_path, 99, limited)), pauses=(0.01,))
    with pytest.raises(IntegrationError) as exc:
        await gh.viewer_login()
    assert exc.value.retryable and not isinstance(exc.value, AuthError)
    denied = tmp_path / "denied"
    denied.mkdir()
    gh = GhClient(
        "o/r", str(_fake_gh(denied, 99, "gh: Resource not accessible (HTTP 403)\n")), pauses=(0.01,)
    )
    with pytest.raises(AuthError):
        await gh.viewer_login()
    assert (denied / "calls").read_text() == "1", "a permission failure is never repeated"


def test_commit_signing_and_hooks_follow_the_config(tmp_path: Path) -> None:
    locks = RepoLocks(tmp_path / "locks")
    plain = ManagedRepo("u", "main", tmp_path, locks)._argv(("commit",), bare=False)
    assert "core.hooksPath=/dev/null" in plain and "commit.gpgsign=false" in plain
    signed = ManagedRepo("u", "main", tmp_path, locks, sign_commits=True, run_hooks=True)
    argv = signed._argv(("commit",), bare=False)
    assert "core.hooksPath=/dev/null" not in argv and "commit.gpgsign=true" in argv


def test_doctor_explains_a_branch_that_needs_signed_commits(make_config: ConfigFactory) -> None:
    report = Report()
    check_signing(make_config(), report, required=True)
    (check,) = report.checks
    assert check.level == "fail" and "sign_commits = true" in check.action
    report = Report()
    check_signing(make_config(), report, required=False)
    assert report.checks == []


async def test_linked_tickets_stay_inside_the_allowed_projects(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    await w.repo.ensure()
    w.new_ticket("PILOT-2", summary="Earlier story")
    w.jira.create("OTHER-7", "Another client's ticket", "confidential", None)
    w.new_ticket(
        "PILOT-3",
        links=[
            IssueLink("Relates", "outward", "relates to", "PILOT-2"),
            IssueLink("Relates", "outward", "relates to", "OTHER-7"),
        ],
    )
    issue = await w.jira.get_issue("PILOT-3")
    got = await linked_tickets(w.jira, w.repo, w.cfg.repository.url, issue, tmp_path / "linked")
    assert [t.key for t in got] == ["PILOT-2"]
    allowed = frozenset({"PILOT", "OTHER"})
    got = await linked_tickets(w.jira, w.repo, w.cfg.repository.url, issue, tmp_path / "linked2", allowed)
    assert [t.key for t in got] == ["PILOT-2", "OTHER-7"]


async def test_old_finished_runs_are_cleaned_by_themselves(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    await w.repo.ensure()
    store, root = w.deps.store, w.cfg.repository.worktree_root
    old = utcnow() - timedelta(days=40)

    def run(key: str, run_id: str, state: RunState, updated: object) -> None:
        journal = store.run_journal(key, run_id)
        journal.create(
            RunRecord(
                ticket_key=key,
                run_id=run_id,
                attempt=1,
                stage=Stage.DEVELOPMENT,
                developer_account_id="dev",
                worker_id="w",
                session_label=run_id,
                state=state,
                attempt_key=run_id,
                updated_at=updated,  # type: ignore[arg-type]
            )
        )
        (root / key / run_id / "feature").mkdir(parents=True)
        (root / key / run_id / "feature" / "f").write_text("x")

    run("PILOT-1", "PILOT-1-old", RunState.AWAITING_HUMAN, old)
    run("PILOT-2", "PILOT-2-recent", RunState.AWAITING_HUMAN, utcnow())
    run("PILOT-3", "PILOT-3-paused", RunState.INTERRUPTED, old)
    run("PILOT-4", "PILOT-4-busy", RunState.AWAITING_HUMAN, old)
    shown: list[str] = []
    removed = await cleanup.clean_expired(w.cfg, w.repo, 30, {"PILOT-4"}, shown.append)
    assert removed == 1
    assert not (root / "PILOT-1").exists() and not (store.runs_dir / "PILOT-1").exists()
    assert (root / "PILOT-2" / "PILOT-2-recent").exists() and (store.runs_dir / "PILOT-2").exists()
    assert (root / "PILOT-3" / "PILOT-3-paused").exists(), "unfinished work is never removed"
    assert (root / "PILOT-4" / "PILOT-4-busy").exists(), "a ticket being worked on is left alone"


def test_internal_error_comments_carry_no_error_text() -> None:
    text = comments.internal_error("refinement", "run-1", "laptop", "PILOT-1")
    assert "coordinator recover PILOT-1 --resume" in text and "**Error**" not in text


async def test_alerts_are_sent_once_an_hour(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DELIVERY_TEST_WEBHOOK", "https://hooks.example.test/x")
    cfg = make_config(overrides={"notifications": {"webhook_env": "DELIVERY_TEST_WEBHOOK", "desktop": True}})
    posts: list[str] = []
    shown: list[str] = []
    now = {"t": 0.0}

    async def post(url: str, text: str) -> None:
        posts.append(text)

    async def notify(title: str, text: str) -> None:
        shown.append(title)

    alerts = Alerts(cfg, post=post, notify=notify, clock=lambda: now["t"])
    assert await alerts.send("k", "title", "text") and len(posts) == 1 and len(shown) == 1
    assert not await alerts.send("k", "title", "text")
    now["t"] += 3601
    assert await alerts.send("k", "title", "text") and len(posts) == 2
    assert "test-laptop" in posts[0], "says which machine"
    assert alerts.on_tickets


async def test_a_failed_webhook_never_fails_the_caller(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv("DELIVERY_TEST_WEBHOOK", "https://hooks.example.test/x")
    cfg = make_config(overrides={"notifications": {"webhook_env": "DELIVERY_TEST_WEBHOOK"}})

    async def post(url: str, text: str) -> None:
        raise httpx.ConnectError("down")

    assert await Alerts(cfg, post=post).send("k", "t", "x")
