"""Recovery without blocking: waiting for Claude, internal errors, stopping and stale code."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from pathlib import Path

from delivery.coordinator import WAITING_FOR_CLAUDE
from delivery.models import RunState
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, drain, make_world, step

KEY = "PILOT-1"


def _envelopes(w: World, procedure: str) -> list[dict]:  # type: ignore[type-arg]
    out = []
    for inv in sorted(w.invocations(), key=lambda i: i["started"]):
        if f"/delivery:{procedure}" in " ".join(inv["argv"]):
            prompt = inv["argv"][inv["argv"].index("-p") + 1]
            out.append(json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text()))
    return out


async def _check_now(sup: Supervisor) -> None:
    sup._claude_check_at = 0.0
    await sup._check_claude()


async def test_a_usage_limit_waits_for_claude_then_continues(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario(
        {
            "PILOT-1:refine-ticket": [{"usage_limit": True}, {}],
            "refine-ticket": [{}],
            "probe": [{"usage_limit": True}, {}],
        }
    )
    w.new_ticket(KEY)
    w.submit(KEY)
    w.new_ticket("PILOT-2")
    shown: list[str] = []
    async with Supervisor(w.deps, emit=shown.append) as sup:
        await step(sup)
        # Not blocked: the ticket stays in Refining and says it waits for Claude.
        assert w.jira.status_of(KEY) is Status.REFINING
        assert "waiting for Claude" in w.last_comment(KEY)
        waiting = sup.record.claude_unavailable
        assert waiting is not None and waiting["kind"] == "usage_limit"
        assert any("WAITING FOR CLAUDE" in s for s in shown)
        run = w.deps.store.latest_run(KEY)
        assert run is not None and run.record is not None
        assert run.record.state is RunState.INTERRUPTED and not run.record.held
        # New work waits too.
        w.submit("PILOT-2")
        await _check_now(sup)  # the probe still hits the limit
        assert sup.record.claude_unavailable is not None and not sup.sessions
        assert w.jira.status_of("PILOT-2") is Status.READY_REFINEMENT
        await _check_now(sup)  # Claude works again: the waiting run continues by itself
        assert sup.record.claude_unavailable is None
        assert any("CLAUDE WORKS AGAIN" in s for s in shown)
        await drain(sup)
        assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW
        run = w.deps.store.latest_run(KEY)
        assert run is not None and run.record is not None and WAITING_FOR_CLAUDE not in run.record.outputs
        await step(sup)  # and new work starts again
        assert w.jira.status_of("PILOT-2") is Status.SPECIFICATION_REVIEW
    assert not any(c for c in w.comments(KEY) if "Blocked" in c)


async def test_development_waiting_for_claude_continues_in_its_own_worktree(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario(
        {
            "implement-ticket": [
                {"edit": {"src/style.css": "body { margin: 0 }\n"}, "usage_limit": True},
                {"edit": {"src/a.test.ts": "finished\n"}},
            ],
            "probe": [{}],
        }
    )
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.DEVELOPING
        await _check_now(sup)
        await drain(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
    # Two sessions of the same run (they share one envelope file, so this is the second's):
    # it was told about the changes the first left in the worktree.
    envelopes = _envelopes(w, "implement-ticket")
    assert len(envelopes) == 2
    resumed = envelopes[-1]
    assert resumed["prior_work"]["run_id"] == resumed["run_id"]
    assert resumed["prior_work"]["files"] == ["src/style.css"]
    show = ["git", "--git-dir", str(w.origin), "show"]
    style = subprocess.run([*show, f"feature/{KEY}:src/style.css"], capture_output=True, text=True)
    assert style.stdout == "body { margin: 0 }\n"


async def test_waiting_runs_continue_when_the_coordinator_starts_again(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"usage_limit": True}, {}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert sup.record.claude_unavailable is not None
    async with Supervisor(w.deps) as sup:
        assert sup.record.claude_unavailable is None, "a new start checks again"
        await sup.reconcile()
        await drain(sup)
    assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW


async def test_an_internal_error_is_explained_in_jira(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:

        async def broken(rc):  # type: ignore[no-untyped-def]
            raise RuntimeError("something unexpected")

        sup.executor.execute = broken  # type: ignore[method-assign]
        await step(sup)
    comment = w.last_comment(KEY)
    assert "coordinator error" in comment
    assert "something unexpected" not in comment, "error text stays in the coordinator log"
    assert f"coordinator recover {KEY} --resume" in comment
    run = w.deps.store.latest_run(KEY)
    assert run is not None and run.record is not None
    assert run.record.held and "coordinator recover" in (run.record.next_action or "")
    events = [e for e in run.journal.events.read() if e["type"] == "internal_error"]
    assert "RuntimeError: something unexpected" in events[0]["data"]["traceback"]


async def test_stop_command_sighup_and_changed_code(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    shown: list[str] = []
    async with Supervisor(w.deps, emit=shown.append) as sup:
        assert sup.record.code_mtime
        reply = await sup.handle({"cmd": "shutdown"})
        assert reply == {"ok": True, "running_sessions": 0} and sup.stop_event.is_set()

    async with Supervisor(w.deps, emit=shown.append) as sup:
        loop = asyncio.get_running_loop()
        sup.install_signal_handlers()
        try:
            os.kill(os.getpid(), signal.SIGHUP)  # the terminal window was closed
            await asyncio.wait_for(sup.stop_event.wait(), timeout=5)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.remove_signal_handler(sig)
        sup.record = sup.record.model_copy(update={"code_mtime": 1.0})
        sup._check_code()
        sup._check_code()
    assert sum("CODE CHANGED" in s for s in shown) == 1
