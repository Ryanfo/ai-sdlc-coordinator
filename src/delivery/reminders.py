"""Reminders for the developer's tickets that have waited long for a person.

Work stalls quietly when a review or a decision sits unnoticed. After ``[reminders]
after_hours`` in a status that waits for a person (a review or decision, answers, a blocker, the
merge), the coordinator comments on the ticket saying how long it has waited and what is needed
next; Jira notifies the ticket's watchers, and listed approvers are mentioned. It repeats every
``repeat_hours``, at most ``max_reminders`` times per wait, and with ``weekdays_only`` sends
nothing at weekends. With ``webhook_env`` the same reminder goes to a Slack (or compatible)
incoming webhook too. A new move of the ticket starts a new wait.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from delivery import console
from delivery.config import Config
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.models import utcnow
from delivery.ownership import waiting_jql
from delivery.ports import IntegrationError, JiraIssue, JiraPort
from delivery.publication import PublicationError, PublicationUncertain, Publisher
from delivery.team import entry, waited, waiting_on
from delivery.workflow import STATUS_NAMES, Status

log = logging.getLogger("delivery")
CHECK_SECONDS = 900


def reminder(status: Status, hours: float, since: datetime, step: str, mention: list[str]) -> str:
    who = (" ".join(f"<@{a}>" for a in mention) + " ") if mention else ""
    return "\n".join(
        [
            f"## Reminder: waiting {waited(hours)} in {STATUS_NAMES[status]}",
            f"{who}This ticket has been in **{STATUS_NAMES[status]}** since "
            f"{since:%d %b %H:%M} UTC. Next: {step}. The latest comment that asks for it says "
            "which action to choose.",
        ]
    )


class Reminders:
    def __init__(
        self,
        cfg: Config,
        jira: JiraPort,
        emit: Callable[[str], None],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.cfg = cfg
        self.jira = jira
        self.emit = emit
        self.clock = clock
        self.dir = cfg.runtime.state_dir / "reminders"
        self._last: datetime | None = None

    def _sent(self, key: str) -> dict[str, int]:
        try:
            return dict(json.loads((self.dir / f"{key}.json").read_text()))
        except (OSError, ValueError):
            return {}

    async def tick(self, *, force: bool = False) -> None:
        rc = self.cfg.reminders
        now = self.clock()
        if rc.after_hours == 0:
            return
        if not force and self._last is not None and now - self._last < timedelta(seconds=CHECK_SECONDS):
            return
        self._last = now
        if rc.weekdays_only and now.astimezone().weekday() >= 5:
            return
        try:
            issues = await self.jira.search(waiting_jql(self.cfg))
        except IntegrationError as exc:
            log.info("reminders skipped: %s", exc)
            return
        for issue in issues:
            try:
                await self.check(issue, now)
            except (IntegrationError, PublicationError, PublicationUncertain) as exc:
                log.warning("reminder for %s not sent: %s", issue.key, exc)

    async def check(self, issue: JiraIssue, now: datetime) -> bool:
        rc = self.cfg.reminders
        status = self.cfg.status_by_id().get(issue.view.status_id)
        moved = await entry(self.jira, issue)
        if status is None or moved is None:
            return False
        hours = (now - moved.created).total_seconds() / 3600
        sent = self._sent(issue.key)
        n = sent.get(moved.history_id, 0)
        if n >= rc.max_reminders or hours < rc.after_hours + n * rc.repeat_hours:
            return False
        _, step = waiting_on(status, issue.assignee_name)
        decision = status not in (Status.NEEDS_CLARIFICATION, Status.BLOCKED, Status.READY_RELEASE)
        mention = list(self.cfg.approvals.jira_account_ids) if decision else []
        text = reminder(status, hours, moved.created, step, mention)
        journal = RunJournal(ensure_private_dir(self.dir / issue.key))
        pub = Publisher(self.cfg, self.jira, None, None, journal, f"reminder-{issue.key}")
        await pub.comment(issue.key, "reminder", text, f"{moved.history_id}-{n + 1}")
        atomic_write_json(self.dir / f"{issue.key}.json", {moved.history_id: n + 1})
        await self._webhook(issue, status, hours, step)
        self.emit(console.line(f"{issue.key}: reminded that it has waited {waited(hours)} ({step})"))
        return True

    async def _webhook(self, issue: JiraIssue, status: Status, hours: float, step: str) -> None:
        env = self.cfg.reminders.webhook_env or self.cfg.notifications.webhook_env
        url = os.environ.get(env) if env else None
        if not url:
            return
        link = f"{self.cfg.jira.base_url}/browse/{issue.key}"
        text = (
            f"{issue.key} ({issue.view.summary}) has waited {waited(hours)} in {STATUS_NAMES[status]}. "
            f"Next: {step}. {link}"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json={"text": text})
                r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("reminder webhook failed for %s: %s", issue.key, exc)


__all__ = ["CHECK_SECONDS", "Reminders", "reminder"]
