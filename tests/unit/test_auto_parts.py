"""Auto mode's pieces: the effective configuration, the command, and which tickets block which."""

from __future__ import annotations

from conftest import APPROVER, DEV, ConfigFactory
from delivery.autopilot import _blocking_keys
from delivery.cli import coordinator_args, parser
from delivery.ownership import IssueView
from delivery.ports import IssueLink, JiraIssue


def test_auto_mode_lets_the_developer_decide_and_needs_no_github_reviewer(make_config: ConfigFactory) -> None:
    cfg = make_config(
        overrides={
            "approvals": {"jira_account_ids": [APPROVER]},
            "claude.interactive": {"enabled": True},
        }
    ).for_auto()
    assert cfg.auto.enabled
    assert DEV in cfg.approvals.approvers() and APPROVER in cfg.approvals.approvers()
    assert not cfg.approvals.require_independent_github_review
    assert not cfg.claude.interactive.enabled  # nobody is there to type to a session


def test_auto_mode_keeps_anyone_may_decide(make_config: ConfigFactory) -> None:
    cfg = make_config(overrides={"approvals": {"jira_account_ids": []}}).for_auto()
    assert cfg.approvals.jira_account_ids == []


def test_coordinator_auto_is_its_own_command() -> None:
    commands = set(next(a for a in parser()._actions if hasattr(a, "choices") and a.choices).choices)
    assert coordinator_args(["auto"], commands) == ["auto"]
    assert coordinator_args(["auto", "--foreground"], commands) == ["auto", "--foreground"]


def _issue(key: str, *links: IssueLink) -> JiraIssue:
    return JiraIssue(IssueView(key, "PILOT", "Story", "1", "Blocked", None), links=links)


def test_blocking_tickets_come_from_links_and_the_blocker_text() -> None:
    issue = _issue(
        "PILOT-3",
        IssueLink("Blocks", "inward", "is blocked by", "PILOT-1"),
        IssueLink("Blocks", "outward", "blocks", "PILOT-9"),  # this one waits on PILOT-3, not the reverse
        IssueLink("Relates", "inward", "relates to", "PILOT-8"),
    )
    reason = "Depends on PILOT-2 (the index) and PILOT-1; see PILOT-3 and OTHER-4"
    assert _blocking_keys(issue, reason, "PILOT") == ["PILOT-1", "PILOT-2"]
    assert _blocking_keys(_issue("PILOT-3"), "the API key is missing", "PILOT") == []
