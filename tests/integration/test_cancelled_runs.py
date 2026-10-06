"""A ticket cancelled in Jira after its run stopped does not stay waiting in the journal or the office."""

from __future__ import annotations

from pathlib import Path

from conftest import DEV
from delivery.models import RunState
from delivery.office import OfficeFeed
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import make_world, step


def _state(w, key: str) -> RunState:  # type: ignore[no-untyped-def]
    latest = w.deps.store.latest_run(key)
    assert latest and latest.record
    return latest.record.state


async def test_runs_waiting_for_a_person_are_closed_when_the_ticket_is_cancelled(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"PILOT-2:refine-ticket": [{"outcome": "blocked", "blocker_reason": "unclear brief"}]})
    for key in ("PILOT-1", "PILOT-2", "PILOT-3"):
        w.new_ticket(key)
        w.submit(key)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert _state(w, "PILOT-1") is RunState.AWAITING_HUMAN
        assert _state(w, "PILOT-2") is RunState.BLOCKED
        feed = OfficeFeed(w.cfg.runtime.state_dir, w.cfg.identity_key)
        feed.poll()

        w.jira.human_move("PILOT-1", Status.CANCELLED, DEV)  # from Specification review
        w.jira.human_move("PILOT-2", Status.CANCELLED, DEV)  # from Blocked
        await step(sup)

        assert _state(w, "PILOT-1") is RunState.CANCELLED
        assert _state(w, "PILOT-2") is RunState.CANCELLED
        assert _state(w, "PILOT-3") is RunState.AWAITING_HUMAN  # still waiting: not cancelled
        rec = w.deps.store.latest_run("PILOT-1").record  # type: ignore[union-attr]
        assert rec and rec.reason == "cancelled in Jira" and rec.next_action == "None (cancelled)."
        beats = [(b["ticket"], b["kind"]) for b in feed.poll() if b["kind"] == "cancelled"]
        assert sorted(beats) == [("PILOT-1", "cancelled"), ("PILOT-2", "cancelled")]

        # Closed once: a later poll neither repeats it nor touches the run again.
        before = rec.updated_at
        await step(sup)
        again = w.deps.store.latest_run("PILOT-1").record  # type: ignore[union-attr]
        assert again and again.updated_at == before
        assert not [b for b in feed.poll() if b["kind"] == "cancelled"]
    assert not [c for c in w.comments("PILOT-1") if "cancel" in c.lower()]  # nothing is posted to Jira
