"""Humans moving cards themselves: approvals still count, and dragged cards are not stranded."""

from __future__ import annotations

from pathlib import Path

from conftest import DEV
from delivery.models import GateKind, GateState
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import make_world, step

KEY = "PILOT-1"


def _spec_state(w) -> GateState:  # type: ignore[no-untyped-def]
    return next(g.state for g in w.record(KEY).gates if g.kind is GateKind.SPEC)


async def test_card_dragged_into_working_status_is_taken_over(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        # The developer drags the card to "Agent working" before the next poll.
        w.jira.human_move(KEY, Status.PLANNING, DEV)
        before = len(w.jira.changes_by_key[KEY])
        assert await step(sup) == [KEY]
        assert w.jira.status_of(KEY) is Status.PLAN_REVIEW
        moves = [(c.from_name, c.to_name) for c in w.jira.changes_by_key[KEY][before:]]
        assert moves == [("Planning", "Plan review")]  # the start move was not repeated
        assert any("Moved into Planning by hand" in c for c in w.comments(KEY))
        assert _spec_state(w) is GateState.APPROVED
        # Nothing further happens on later polls.
        assert await step(sup) == []


async def test_approval_counts_after_manual_moves_and_resume(tmp_path: Path) -> None:
    """The SDLC-7 sequence: approve, drag to Planning, Ask questions, Submit planning answers."""
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        for target in (Status.PLANNING, Status.NEEDS_CLARIFICATION, Status.READY_PLANNING):
            w.jira.human_move(KEY, target, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.BLOCKED
        assert "Choose Resume planning" in w.last_comment(KEY)
        w.jira.human_move(KEY, Status.READY_PLANNING, DEV)  # Resume planning
        await step(sup)
        assert w.jira.status_of(KEY) is Status.PLAN_REVIEW, w.last_comment(KEY)
        assert _spec_state(w) is GateState.APPROVED
