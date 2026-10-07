"""Failure injection and multi-session recovery (handoff §12, amendment §7)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from delivery.ownership import LockHeld
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import external_commit
from harness import World, drain, make_world, step


async def _to_development(w: World, sup: Supervisor, key: str) -> None:
    await step(sup)
    w.move(key, Status.READY_PLANNING)
    await step(sup)
    w.move(key, Status.READY_DEVELOPMENT)


async def test_lost_comment_response_reconciles_without_duplicate(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    w.jira.lose_next.add("add_comment")  # the "started" comment is created but the response is lost
    async with Supervisor(w.deps) as sup:
        await step(sup)
        latest = w.deps.store.latest_run("PILOT-1")
        assert latest and latest.record
        if latest.record.state.value == "publishing" or latest.journal.pending_ops():
            await sup.reconcile()
            await drain(sup)
    starts = [c for c in w.comments("PILOT-1") if "Refinement started" in c]
    assert len(starts) == 1
    assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW


async def test_lost_transition_response_is_reconciled_from_jira(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    w.jira.lose_next.add("do_transition")  # start transition applied, response lost
    async with Supervisor(w.deps) as sup:
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW
    moves = [c for c in w.jira.changes_by_key["PILOT-1"]]
    assert len(moves) == 3  # submit, start, complete: no duplicate transition


async def test_lost_pr_creation_response_attaches_existing_pr(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await _to_development(w, sup, "PILOT-1")
        w.github.lose_next_create = True
        await step(sup)
        latest = w.deps.store.latest_run("PILOT-1")
        assert latest and latest.record
        if latest.record.state.value != "awaiting_human":
            await sup.reconcile()
            await drain(sup)
    assert len(w.github.prs) == 1
    assert w.jira.status_of("PILOT-1") is Status.READY_VERIFICATION


async def test_shutdown_mid_session_resumes_every_run_independently(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"sleep": 30}, {}]})
    for k in ("PILOT-1", "PILOT-2"):
        w.new_ticket(k)
        w.submit(k)
    sup = Supervisor(w.deps)
    await sup.__aenter__()
    await sup.deps.repo.ensure()
    await sup.poll_once()
    await asyncio.sleep(1.5)
    await sup.__aexit__(None, None, None)  # graceful stop: children terminated, checkpoints written
    for k in ("PILOT-1", "PILOT-2"):
        latest = w.deps.store.latest_run(k)
        assert latest and latest.record and latest.record.state.value == "interrupted"
        assert not latest.record.held
        assert w.jira.status_of(k) is Status.REFINING
    # A fresh supervisor reconciles both runs concurrently with fresh sessions.
    async with Supervisor(w.deps) as sup2:
        actions = await sup2.reconcile()
        assert len([a for a in actions if "resuming" in a]) == 2
        await drain(sup2)
    for k in ("PILOT-1", "PILOT-2"):
        assert w.jira.status_of(k) is Status.SPECIFICATION_REVIEW
        assert len([c for c in w.comments(k) if "Refinement started" in c]) == 1


async def test_corrupt_record_blocks_only_its_ticket(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"PILOT-1:refine-ticket": [{"sleep": 30}], "refine-ticket": [{}]})
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    sup = Supervisor(w.deps)
    await sup.__aenter__()
    await sup.deps.repo.ensure()
    await sup.poll_once()
    await asyncio.sleep(1)
    await sup.__aexit__(None, None, None)
    latest = w.deps.store.latest_run("PILOT-1")
    assert latest
    with latest.journal.events.path.open("ab") as fh:
        fh.write(b'{"type": "op_intent"')  # torn write
    w.new_ticket("PILOT-2")
    w.submit("PILOT-2")
    async with Supervisor(w.deps) as sup2:
        actions = await sup2.reconcile()
        assert any("PILOT-1: blocked locally" in a for a in actions)
        assert await step(sup2) == ["PILOT-2"]
    assert w.jira.status_of("PILOT-2") is Status.SPECIFICATION_REVIEW
    assert w.jira.status_of("PILOT-1") is Status.REFINING


async def test_jira_offline_leaves_state_untouched_and_backs_off(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    w.jira.offline = True
    async with Supervisor(w.deps) as sup:
        report = await sup.poll_once()
        assert report.error and report.started == []
        assert sup.backoff_seconds > 0
        w.jira.offline = False
        sup.backoff_until = 0
        assert await step(sup) == ["PILOT-1"]


async def test_diverged_remote_blocks_without_force_push(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await step(sup)
        # Someone else pushes to the ticket's delivery branch between runs.
        external_commit(tmp_path, w.origin, "delivery/PILOT-1", "docs/other.md", "x\n", "intruder")
        w.decide("PILOT-1", Status.READY_REFINEMENT, "F1: more")
        await step(sup)
    latest = w.deps.store.latest_run("PILOT-1")
    assert latest and latest.record
    # The run starts from the updated remote branch, so this is a clean fast-forward.
    assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW
    names = await w.repo.ls_tree("origin/delivery/PILOT-1", "docs/")
    assert "docs/other.md" in names  # nothing was overwritten


async def test_second_supervisor_for_same_identity_refused(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps):
        with pytest.raises(LockHeld):
            await Supervisor(w.deps).__aenter__()


async def test_handover_of_one_ticket_while_another_runs(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"PILOT-1:refine-ticket": [{"sleep": 30}], "PILOT-2:refine-ticket": [{"sleep": 3}]})
    for k in ("PILOT-1", "PILOT-2"):
        w.new_ticket(k)
        w.submit(k)
    async with Supervisor(w.deps) as sup:
        await sup.deps.repo.ensure()
        await sup.poll_once()
        await asyncio.sleep(1)
        res = await sup.handle({"cmd": "handover", "ticket": "PILOT-1"})
        assert res["ok"] and res["ready_for_reassignment"]
        assert "PILOT-2" in sup.sessions
        await drain(sup)
    assert w.jira.status_of("PILOT-1") is Status.BLOCKED
    assert w.jira.issues["PILOT-1"].fields["customfield_10050"] == {"value": "refinement"}
    assert any("Handover checkpoint" in c for c in w.comments("PILOT-1"))
    assert w.jira.status_of("PILOT-2") is Status.SPECIFICATION_REVIEW
