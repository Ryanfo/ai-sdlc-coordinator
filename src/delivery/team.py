"""What every in-flight ticket in the project is waiting on, for everyone on the team.

Each developer's coordinator runs only their own tickets, but decisions can come from anyone, so
the team needs one view across all of them: ``delivery team``. It reads Jira only (any assignee)
and groups tickets by what moves them on next: a person's decision, answers or a blocker, a merge,
or the coordinator and Claude. Within each group the longest wait comes first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from pydantic import ValidationError

from delivery.config import Config
from delivery.models import PROPERTY_KEY, SharedExecutionRecord, utcnow
from delivery.ownership import coordination_jql
from delivery.ports import IntegrationError, JiraIssue, JiraPort, StatusChange
from delivery.workflow import ACTIVE_STATUSES, READY_STATUSES, STATUS_NAMES, Status

# What a person does next, by status.
DECISIONS: dict[Status, str] = {
    Status.SPECIFICATION_REVIEW: "approve or change the specification",
    Status.PLAN_REVIEW: "approve or change the plan",
    Status.CODE_REVIEW: "review the PR on GitHub, then approve or request code changes",
    Status.ACCEPTANCE_REVIEW: "try it, then accept or request changes",
    Status.CHANGES_REQUESTED: "choose Submit implementation changes or Revise scope",
}
GROUPS = (
    ("decision", "Waiting for a decision (anyone who may approve)"),
    ("answers", "Waiting for answers or a blocker to be resolved"),
    ("merge", "Approved for release: waiting for the PR to be merged"),
    ("coordinator", "With the coordinator: queued or Claude working"),
)


@dataclass
class Row:
    key: str
    summary: str
    status: str
    assignee: str
    group: str
    next: str
    since: str | None
    waiting_hours: float | None
    pr: int | None = None
    candidate: int | None = None


def waiting_on(status: Status | None, assignee: str, pr: int | None = None) -> tuple[str, str]:
    """(group, next step) for a ticket in ``status``."""
    if status in DECISIONS:
        return "decision", DECISIONS[status]
    if status is Status.NEEDS_CLARIFICATION:
        return "answers", "answer the questions, then Submit answers"
    if status is Status.BLOCKED:
        return "answers", "resolve the blocker, then Resume"
    if status is Status.READY_RELEASE:
        return "merge", f"merge PR #{pr}" if pr else "merge the pull request"
    if status in ACTIVE_STATUSES:
        return "coordinator", "Claude is working"
    if status in READY_STATUSES:
        return "coordinator", f"queued for {assignee or 'its assignee'}'s coordinator"
    return "coordinator", "-"


async def _record(jira: JiraPort, key: str) -> SharedExecutionRecord | None:
    try:
        raw = await jira.get_property(key, PROPERTY_KEY)
        return SharedExecutionRecord.model_validate(raw) if raw else None
    except (IntegrationError, ValidationError):
        return None


async def entry(jira: JiraPort, issue: JiraIssue) -> StatusChange | None:
    """The move that brought the ticket into its current status (Jira's changelog)."""
    changes = [c for c in await jira.status_changes(issue.key) if c.to_id == issue.view.status_id]
    return max(changes, key=lambda c: (c.created, c.history_id), default=None)


async def board(cfg: Config, jira: JiraPort, now: datetime | None = None) -> list[Row]:
    now = now or utcnow()
    by_id = cfg.status_by_id()
    rows: list[Row] = []
    for issue in await jira.search(coordination_jql(cfg)):
        status = by_id.get(issue.view.status_id)
        rec = await _record(jira, issue.key)
        moved = await entry(jira, issue)
        since = moved.created if moved else None
        assignee = issue.assignee_name or issue.view.assignee_account_id or "unassigned"
        group, step = waiting_on(status, assignee, rec.pr_number if rec else None)
        rows.append(
            Row(
                issue.key,
                issue.view.summary,
                STATUS_NAMES[status] if status else issue.view.status_name,
                assignee,
                group,
                step,
                since.isoformat() if since else None,
                round((now - since).total_seconds() / 3600, 1) if since else None,
                rec.pr_number if rec else None,
                rec.candidate_number if rec and rec.candidate_number else None,
            )
        )
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    rows.sort(key=lambda r: (order[r.group], -(r.waiting_hours or 0), r.key))
    return rows


def waited(hours: float | None) -> str:
    if hours is None:
        return "?"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def render(rows: list[Row], base_url: str) -> str:
    if not rows:
        return "Nothing in flight in this project."
    out: list[str] = []
    for group, title in GROUPS:
        mine = [r for r in rows if r.group == group]
        if not mine:
            continue
        out += ["", f"{title} ({len(mine)}):"]
        for r in mine:
            out.append(
                f"  {r.key:<11} {waited(r.waiting_hours):>4}  {r.status:<24} {r.assignee[:18]:<18} "
                f"{r.summary[:40]}"
            )
            out.append(f"  {'':<11} {'':>4}  next: {r.next}")
    out += ["", f"Open a ticket: {base_url}/browse/<KEY>"]
    return "\n".join(out).lstrip("\n")


def as_json(rows: list[Row]) -> list[dict[str, object]]:
    return [asdict(r) for r in rows]


__all__ = ["DECISIONS", "Row", "board", "entry", "render", "waited", "waiting_on"]
