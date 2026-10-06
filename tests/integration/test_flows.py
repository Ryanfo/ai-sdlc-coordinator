"""Other kinds of work: proposed tickets (slices and follow-ups), spikes and the fast track."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import APPROVER, OTHER_DEV
from delivery.models import GateKind, GateState
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, make_world, step

KEY = "PILOT-1"
SPIKES = {"jira": {"supported_issue_types": ["Story", "Bug", "Spike"]}}
SLICES = [
    {
        "id": "S1",
        "summary": "Search by title",
        "description": "Users find tasks by title.\nAC1: typing part of a title lists matching tasks.",
    },
    {
        "id": "S2",
        "summary": "Export results",
        "description": "Users export the results.\nAC1: a CSV downloads.",
    },
]


def _calls(w: World, procedure: str) -> int:
    return sum(f"/delivery:{procedure}" in " ".join(i["argv"]) for i in w.invocations())


def _show(w: World, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(w.origin), "show", ref], capture_output=True, text=True, check=False
    ).stdout


async def test_proposed_tickets_are_created_only_when_asked(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"proposed_tickets": SLICES}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        gate = w.last_comment(KEY)
        token = w.token(KEY, "SPEC")
        assert "To create proposed tickets" in gate
        assert "S1 Search by title" in gate and f"CREATE TICKETS {token}" in gate
        assert '"summary": "Export results"' in _show(
            w, f"delivery/{KEY}:docs/delivery/{KEY}/specification/v001.proposed.json"
        )
        before = set(w.jira.issues)
        w.jira.human_comment(KEY, OTHER_DEV, f"CREATE TICKETS {token}\nS1")  # not an approver: ignored
        w.jira.human_comment(KEY, APPROVER, f"CREATE TICKETS {token}\nS2: call it Download\nS9")
        await sup.poll_once()
        (new,) = set(w.jira.issues) - before
        created = w.jira.issues[new]
        assert created.summary == "Export results" and created.status is Status.BACKLOG
        assert created.assignee is None and created.issue_type == "Story"
        assert "Note from the person who asked for this ticket: call it Download" in created.description
        assert f"Proposed in {KEY}" in created.description
        assert [link.other_key for link in created.links] == [KEY]
        assert new in [link.other_key for link in w.jira.issues[KEY].links]
        reply = w.last_comment(KEY)
        assert (
            f"Created in Backlog, unassigned and linked to this ticket: {new} (S2: Export results)" in reply
        )
        assert "Not proposed" in reply and "S9" in reply
        # Asked once, done once.
        w.jira.human_comment(KEY, APPROVER, "thanks")
        await sup.poll_once()
        assert set(w.jira.issues) - before == {new}
        assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW


async def test_a_spike_is_investigated_and_closed_when_its_findings_are_accepted(tmp_path: Path) -> None:
    w = make_world(tmp_path, extra=SPIKES)
    w.scenario({"investigate-ticket": [{"proposed_tickets": SLICES[:1]}]})
    w.new_ticket(KEY, issue_type="Spike", summary="Which search index should we use?")
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.PLAN_REVIEW, w.last_comment(KEY)
        findings = w.last_comment(KEY)
        token = w.token(KEY, "PLAN")
        assert f"Findings v001 ready for review: {token}" in findings
        assert "closes as Done" in findings and f"CREATE TICKETS {token}" in findings
        assert "Use the existing index." in _show(w, f"delivery/{KEY}:docs/delivery/{KEY}/findings/v001.md")
        assert _calls(w, "plan-ticket") == 0 and _calls(w, "investigate-ticket") == 1
        w.decide(KEY, f"APPROVE PLAN {token}", Status.READY_DEVELOPMENT)
        assert await step(sup) == [KEY]
        assert w.jira.status_of(KEY) is Status.DONE
        assert "Spike complete: findings v001 accepted" in w.last_comment(KEY)
        assert _calls(w, "implement-ticket") == 0
        # Its follow-ups can still be created after it is done.
        before = set(w.jira.issues)
        w.jira.human_comment(KEY, APPROVER, f"CREATE TICKETS {token}\nS1")
        await sup.poll_once()
        (new,) = set(w.jira.issues) - before
        assert w.jira.issues[new].summary == "Search by title"


async def test_a_spike_without_the_complete_spike_transition_says_to_close_it_by_hand(tmp_path: Path) -> None:
    w = make_world(tmp_path, extra=SPIKES)
    w.jira.drop_routes.add((Status.READY_DEVELOPMENT, Status.DONE))
    w.new_ticket(KEY, issue_type="Spike")
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_DEVELOPMENT
        assert "Move it to Done by hand" in w.last_comment(KEY)
        before = len(w.comments(KEY))
        await step(sup)
        assert len(w.comments(KEY)) == before and _calls(w, "implement-ticket") == 0


async def test_the_fast_track_approves_the_plan_with_the_specification(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY, labels=("fast-track",))
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        gate = w.last_comment(KEY)
        assert "Fast track: plan v001 (" in gate and "was written with it" in gate
        assert "Written with the specification." in _show(
            w, f"delivery/{KEY}:docs/delivery/{KEY}/plan/v001.md"
        )
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)  # planning publishes the approved plan: no Claude, no plan review
        assert w.jira.status_of(KEY) is Status.READY_DEVELOPMENT, w.last_comment(KEY)
        assert "approved with the specification" in w.last_comment(KEY)
        plan = next(g for g in w.record(KEY).gates if g.kind is GateKind.PLAN)
        assert plan.state is GateState.APPROVED and plan.evidence and plan.evidence.comment_author == APPROVER
        assert w.record(KEY).footprint_ref
        assert _calls(w, "plan-ticket") == 0
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        started = next(c for c in reversed(w.comments(KEY)) if "Development started" in c)
        assert "(fast track)" in started


async def test_the_fast_track_falls_back_to_plan_review(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.jira.drop_routes.add((Status.PLANNING, Status.READY_DEVELOPMENT))
    w.new_ticket(KEY, labels=("fast-track",))
    w.new_ticket("PILOT-2", labels=("fast-track",))
    w.scenario({"PILOT-2:refine-ticket": [{"no_plan": True}]})
    w.submit(KEY)
    w.submit("PILOT-2")
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert "Fast track not used: no plan was written" in w.last_comment("PILOT-2")
        for key in (KEY, "PILOT-2"):
            w.decide(key, f"APPROVE SPEC {w.token(key, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        # No Use approved plan transition: the plan written with the spec goes to Plan review.
        assert w.jira.status_of(KEY) is Status.PLAN_REVIEW
        assert "needs its own approval" in w.last_comment(KEY)
        assert _calls(w, "plan-ticket") == 1  # only PILOT-2, which wrote no plan, was planned
        assert w.jira.status_of("PILOT-2") is Status.PLAN_REVIEW
        # Plan changes asked for: planned by Claude as usual, not the same plan again.
        w.decide(KEY, f"CHANGE PLAN {w.token(KEY, 'PLAN')}\nF1: smaller steps", Status.READY_PLANNING)
        await step(sup)
        assert _calls(w, "plan-ticket") == 2 and w.token(KEY, "PLAN") == f"{KEY}-PLAN-v2"
