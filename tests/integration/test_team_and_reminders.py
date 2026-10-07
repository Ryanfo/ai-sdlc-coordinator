"""The team view across developers, and reminders for tickets that wait long for a person."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from conftest import APPROVER, OTHER_DEV
from delivery.adf import adf_to_text, markdown_to_adf
from delivery.config import RemindersConfig
from delivery.reminders import Reminders
from delivery.supervisor import Supervisor
from delivery.team import board, render
from delivery.workflow import Status
from harness import World, make_world, step


async def _spec_review(w: World, sup: Supervisor, key: str) -> None:
    w.new_ticket(key)
    w.submit(key)
    await step(sup)
    assert w.jira.status_of(key) is Status.SPECIFICATION_REVIEW


async def test_the_team_view_groups_every_ticket_by_what_it_waits_on(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        await _spec_review(w, sup, "PILOT-1")
    w.new_ticket("PILOT-2", assignee=OTHER_DEV)  # another developer's, queued for their coordinator
    w.jira.human_move("PILOT-2", Status.READY_REFINEMENT, OTHER_DEV)
    w.new_ticket("PILOT-3")  # Backlog: not in flight
    rows = await board(w.cfg, w.jira, now=w.jira.clock + timedelta(hours=30))
    assert [(r.key, r.group) for r in rows] == [("PILOT-1", "decision"), ("PILOT-2", "coordinator")]
    first = rows[0]
    assert first.status == "Specification review" and first.next == "approve or change the specification"
    assert first.waiting_hours is not None and 29 < first.waiting_hours <= 30
    text = render(rows, w.cfg.jira.base_url)
    assert text.startswith("Waiting for a decision (anyone who may approve) (1):")
    assert "PILOT-1      30h  Specification review" in text
    assert f"queued for user:{OTHER_DEV}'s coordinator" in text


async def test_reminders_after_a_long_wait_mention_approvers_and_stop(tmp_path: Path) -> None:
    w = make_world(
        tmp_path,
        extra={
            "reminders": {"after_hours": 24, "repeat_hours": 24, "max_reminders": 2, "weekdays_only": False}
        },
    )
    async with Supervisor(w.deps) as sup:
        await _spec_review(w, sup, "PILOT-1")
        entered = w.jira.clock
        hours = [0.0]
        sup.reminders.clock = lambda: entered + timedelta(hours=hours[0])
        before = len(w.comments("PILOT-1"))
        for h, expect in ((23, 0), (25, 1), (30, 1), (49, 2), (100, 2)):
            hours[0] = h
            await sup.reminders.tick(force=True)
            assert len(w.comments("PILOT-1")) - before == expect, h
        text = w.comments("PILOT-1")[before]
        assert text.startswith("Reminder: waiting 25h in Specification review")
        assert "Next: approve or change the specification" in text
        body = w.jira.comments_by_key["PILOT-1"][before].body_adf
        assert {"type": "mention", "attrs": {"id": APPROVER, "text": f"@{APPROVER}"}} in body["content"][1][
            "content"
        ]
        # A new move starts a new wait.
        w.decide("PILOT-1", Status.READY_REFINEMENT, "F1: more detail")
        await step(sup)
        entered = w.jira.clock
        sup.reminders.clock = lambda: entered + timedelta(hours=25)
        await sup.reminders.tick(force=True)
        assert w.last_comment("PILOT-1").startswith("Reminder: waiting 25h")


async def test_no_reminders_at_weekends_or_when_turned_off(tmp_path: Path) -> None:
    w = make_world(tmp_path, extra={"reminders": {"after_hours": 1}})
    async with Supervisor(w.deps) as sup:
        await _spec_review(w, sup, "PILOT-1")
        before = len(w.comments("PILOT-1"))
        saturday = w.jira.clock + timedelta(days=(5 - w.jira.clock.weekday()) % 7, hours=2)
        r = Reminders(w.cfg, w.jira, lambda s: None, clock=lambda: saturday)
        await r.tick(force=True)
        assert len(w.comments("PILOT-1")) == before
        # after_hours = 0 turns them off.
        off = w.cfg.model_copy(update={"reminders": RemindersConfig(after_hours=0, weekdays_only=False)})
        later = w.jira.clock + timedelta(days=9)
        await Reminders(off, w.jira, lambda s: None, clock=lambda: later).tick(force=True)
        assert len(w.comments("PILOT-1")) == before
    assert RemindersConfig().after_hours == 24  # on by default for real use


def test_mentions_survive_the_round_trip() -> None:
    doc = markdown_to_adf("Hello <@approver-0001>, please look.")
    para = doc["content"][0]["content"]
    assert para[1] == {"type": "mention", "attrs": {"id": "approver-0001", "text": "@approver-0001"}}
    assert adf_to_text(doc) == "Hello @approver-0001, please look."
    # Short or odd strings are left as text.
    assert markdown_to_adf("a <@x> b")["content"][0]["content"] == [{"type": "text", "text": "a <@x> b"}]
