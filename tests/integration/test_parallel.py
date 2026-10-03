"""Parallel sessions: many concurrent, isolated runs under one supervisor (amendment §1-3, §9)."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

from conftest import DEV, OTHER_DEV
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, drain, make_world, step

SRC = Path(__file__).resolve().parents[2] / "src" / "delivery"


async def test_many_tickets_start_concurrently_without_a_cap(tmp_path: Path) -> None:
    n = 12
    w: World = make_world(tmp_path)
    # Every refinement waits until all n sessions are alive at the same time.
    w.scenario({"refine-ticket": [{"barrier": n, "barrier_timeout": 30}]})
    keys = [f"PILOT-{i}" for i in range(1, n + 1)]
    for k in keys:
        w.new_ticket(k)
        w.submit(k)
    async with Supervisor(w.deps) as sup:
        started = await step(sup)
    assert sorted(started) == sorted(keys)
    assert all(w.jira.status_of(k) is Status.SPECIFICATION_REVIEW for k in keys)
    # Each session had its own worktree and its own process.
    inv = w.invocations()
    assert len({i["cwd"] for i in inv}) == n
    assert len({i["pid"] for i in inv}) == n


async def test_slow_ticket_does_not_hold_up_new_dispatch(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"PILOT-1:refine-ticket": [{"sleep": 4}], "refine-ticket": [{}]})
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await sup.deps.repo.ensure()
        await sup.poll_once()
        assert set(sup.sessions) == {"PILOT-1"}
        # While PILOT-1 is still running, new eligible work starts immediately.
        w.new_ticket("PILOT-2")
        w.submit("PILOT-2")
        await sup.poll_once()
        assert "PILOT-2" in sup.sessions
        await sup.sessions["PILOT-2"].task
        assert w.jira.status_of("PILOT-2") is Status.SPECIFICATION_REVIEW
        assert w.jira.status_of("PILOT-1") is Status.REFINING  # still working
        await drain(sup)
    assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW


async def test_blocked_failed_and_waiting_tickets_do_not_hold_others(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario(
        {
            "PILOT-1:refine-ticket": [{"usage_limit": True}],
            "PILOT-2:refine-ticket": [{"malformed": True}],
            "PILOT-3:refine-ticket": [{"outcome": "blocked", "blocker_reason": "brief contradicts itself"}],
            "refine-ticket": [{}],
        }
    )
    for k in ("PILOT-1", "PILOT-2", "PILOT-3", "PILOT-4"):
        w.new_ticket(k)
        w.submit(k)
    w.new_ticket("PILOT-5", description="short")  # waits for a usable brief
    w.submit("PILOT-5")
    async with Supervisor(w.deps) as sup:
        await step(sup)
    assert w.jira.status_of("PILOT-4") is Status.SPECIFICATION_REVIEW
    for k in ("PILOT-2", "PILOT-3"):
        assert w.jira.status_of(k) is Status.BLOCKED
    # A usage limit is not the ticket's fault: it waits for Claude instead of being blocked.
    assert w.jira.status_of("PILOT-1") is Status.REFINING
    assert "waiting for Claude" in w.last_comment("PILOT-1")
    assert "usage limit" in w.last_comment("PILOT-1") and "No paid API fallback" in w.last_comment("PILOT-1")
    assert w.jira.status_of("PILOT-5") is Status.READY_REFINEMENT
    assert "Waiting before refinement" in w.last_comment("PILOT-5")


async def test_repeated_polls_never_launch_duplicate_writers(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"sleep": 2}]})
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await sup.deps.repo.ensure()
        reports = [await sup.poll_once() for _ in range(5)]
        assert sum(len(r.started) for r in reports) == 1
        await drain(sup)
        # After the run, the same ready-entry is never re-attempted either.
        w.jira.issues["PILOT-1"].status = Status.READY_REFINEMENT  # simulate a stuck status
        again = await sup.poll_once()
        assert again.started == []
    assert len([i for i in w.invocations() if i["procedure"] == "refine-ticket"]) == 1


async def test_other_assignee_ignored_and_own_ticket_picked_regardless_of_mover(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1", assignee=OTHER_DEV)
    w.submit("PILOT-1", author=OTHER_DEV)
    w.new_ticket("PILOT-2", assignee=DEV)
    w.submit("PILOT-2", author="approver-0001")  # someone else moved my ticket
    async with Supervisor(w.deps) as sup:
        assert await step(sup) == ["PILOT-2"]
    assert w.jira.status_of("PILOT-1") is Status.READY_REFINEMENT


async def test_stop_one_ticket_leaves_other_sessions_running(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"PILOT-1:refine-ticket": [{"sleep": 30}], "PILOT-2:refine-ticket": [{"sleep": 2}]})
    for k in ("PILOT-1", "PILOT-2"):
        w.new_ticket(k)
        w.submit(k)
    async with Supervisor(w.deps) as sup:
        await sup.deps.repo.ensure()
        await sup.poll_once()
        await asyncio.sleep(1)
        res = await sup.handle({"cmd": "stop", "ticket": "PILOT-1"})
        assert res["ok"] and res["other_sessions"] == ["PILOT-2"]
        assert "PILOT-2" in sup.sessions
        await drain(sup)
        status = await sup.handle({"cmd": "status"})
        assert status["sessions"] == []
    assert w.jira.status_of("PILOT-2") is Status.SPECIFICATION_REVIEW
    assert w.jira.status_of("PILOT-1") is Status.REFINING  # checkpointed, nothing published
    latest = w.deps.store.latest_run("PILOT-1")
    assert latest and latest.record and latest.record.state.value == "interrupted" and latest.record.held


async def test_cancel_in_jira_terminates_only_that_child_and_suppresses_publication(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"PILOT-1:refine-ticket": [{"sleep": 30}], "PILOT-2:refine-ticket": [{"sleep": 7}]})
    for k in ("PILOT-1", "PILOT-2"):
        w.new_ticket(k)
        w.submit(k)
    async with Supervisor(w.deps) as sup:
        await sup.deps.repo.ensure()
        await sup.poll_once()
        await asyncio.sleep(1)
        w.jira.human_move("PILOT-1", Status.CANCELLED, DEV)
        await drain(sup, timeout=40)
    assert w.jira.status_of("PILOT-1") is Status.CANCELLED
    assert not [c for c in w.comments("PILOT-1") if "Specification" in c]
    assert w.jira.status_of("PILOT-2") is Status.SPECIFICATION_REVIEW


async def test_dispatch_pause_stops_new_launches_only(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"sleep": 2}]})
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await sup.deps.repo.ensure()
        await sup.poll_once()
        assert (await sup.handle({"cmd": "pause"}))["dispatch_paused"] is True
        w.new_ticket("PILOT-2")
        w.submit("PILOT-2")
        run = asyncio.create_task(sup.run(once=True))
        await run
        await drain(sup)
        assert w.jira.status_of("PILOT-2") is Status.READY_REFINEMENT
        assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW
        await sup.handle({"cmd": "resume"})
        assert await step(sup) == ["PILOT-2"]


async def test_isolated_ports_and_worktrees_for_concurrent_development(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"implement-ticket": [{"barrier": 3, "barrier_timeout": 30}]})
    from delivery.gates import gate_token
    from delivery.models import PROPERTY_KEY, GateKind, GateRecord, GateState, SharedExecutionRecord, utcnow

    keys = ["PILOT-1", "PILOT-2", "PILOT-3"]
    async with Supervisor(w.deps) as sup:
        for k in keys:
            w.new_ticket(k)
            w.submit(k)
        await step(sup)
        for k in keys:
            w.decide(k, f"APPROVE SPEC {w.token(k, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        for k in keys:
            w.decide(k, f"APPROVE PLAN {w.token(k, 'PLAN')}", Status.READY_DEVELOPMENT)
        assert sorted(await step(sup)) == keys
    for k in keys:
        assert w.jira.status_of(k) is Status.READY_VERIFICATION, w.last_comment(k)
    envs = [
        json.loads(p.read_text())
        for p in Path(w.cfg.runtime.state_dir).rglob("envelope-implement-ticket.json")
    ]
    ports = [e["ports"]["app"] for e in envs]
    assert len(set(ports)) == 3
    branches = {w.record(k).pr_number for k in keys}
    assert len(branches) == 3
    _ = (GateKind, GateRecord, GateState, SharedExecutionRecord, PROPERTY_KEY, utcnow, gate_token)


def test_no_numeric_session_ceiling_in_scheduler_code() -> None:
    """Static guard: no semaphore, pool size or session-count setting in dispatch code."""
    forbidden_calls = {"Semaphore", "BoundedSemaphore", "ThreadPoolExecutor", "ProcessPoolExecutor"}
    forbidden_names = {"max_parallel_runs", "max_sessions", "max_workers", "concurrency_limit"}
    for path in SRC.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_calls:
                raise AssertionError(f"{path.name} uses {node.attr}")
            if isinstance(node, ast.Name) and (node.id in forbidden_calls or node.id in forbidden_names):
                raise AssertionError(f"{path.name} uses {node.id}")
            if isinstance(node, ast.arg) and node.arg in forbidden_names:
                raise AssertionError(f"{path.name} has parameter {node.arg}")
