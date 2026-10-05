"""Running unattended for a client team: retries, edits, room, Claude Code updates and alerts."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from delivery.alerts import Alerts
from delivery.claude_version import VersionGuard, proven, write_stamp
from delivery.coordinator import RETRY
from delivery.models import RunRecord, RunState, utcnow
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, drain, make_world, step

KEY = "PILOT-1"
HOOK = "DELIVERY_TEST_WEBHOOK"


def _latest(w: World, key: str = KEY) -> RunRecord:
    run = w.deps.store.latest_run(key)
    assert run is not None and run.record is not None
    return run.record


def _make_due(w: World, key: str = KEY) -> None:
    """Move the run's retry time into the past, as if the pause had gone by."""
    run = w.deps.store.latest_run(key)
    assert run is not None and run.record is not None
    rec = run.record
    rec.outputs[RETRY] = {**rec.outputs[RETRY], "due": (utcnow() - timedelta(seconds=1)).isoformat()}
    run.journal.save(rec, "test_due")


def _briefs(w: World, procedure: str) -> list[str]:
    out = []
    for inv in sorted(w.invocations(), key=lambda i: i["started"]):
        if f"/delivery:{procedure}" in " ".join(inv["argv"]):
            prompt = inv["argv"][inv["argv"].index("-p") + 1]
            envelope = json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text())
            out.append(envelope["brief"]["description"])
    return out


def _alerting(w: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what would be posted to the webhook."""
    monkeypatch.setenv(HOOK, "https://hooks.example.test/x")
    posts: list[str] = []

    async def post(url: str, text: str) -> None:
        posts.append(text)

    w.deps.alerts = Alerts(w.cfg, post=post)
    return posts


async def test_an_api_outage_is_retried_instead_of_blocking_the_ticket(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"api_error": True}, {}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert w.jira.status_of(KEY) is Status.REFINING
        rec = _latest(w)
        assert rec.state is RunState.INTERRUPTED and not rec.held
        assert rec.outputs[RETRY]["kind"] == "claude" and rec.outputs[RETRY]["counts"] == {"claude": 1}
        report = await sup.poll_once()  # not due yet: nothing starts
        assert not sup.sessions and any(x["ticket"] == KEY for x in report.waiting)
        _make_due(w)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW
    assert not any("Blocked" in c for c in w.comments(KEY))


async def test_an_outage_that_lasts_blocks_after_the_retries(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"api_error": True}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        for _ in range(3):
            assert w.jira.status_of(KEY) is Status.REFINING
            _make_due(w)
            await step(sup)
    assert w.jira.status_of(KEY) is Status.BLOCKED
    assert "status.claude.com" in w.last_comment(KEY)


async def test_editing_the_ticket_mid_run_starts_again_with_the_new_text(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"sleep": 9}, {}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    edited = "Users need search. AC1: title and author search are case-insensitive."
    shown: list[str] = []
    async with Supervisor(w.deps, emit=shown.append) as sup:
        await sup.deps.repo.ensure()
        await sup.poll_once()
        await asyncio.sleep(1)
        w.jira.issues[KEY].description = edited
        await drain(sup)
        # Not blocked: nothing was published from the old text and it starts again by itself.
        assert w.jira.status_of(KEY) is Status.REFINING
        rec = _latest(w)
        assert rec.state is RunState.INTERRUPTED and rec.outputs[RETRY]["kind"] == "edited"
        _make_due(w)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW
    briefs = _briefs(w, "refine-ticket")
    assert len(briefs) == 2 and briefs[-1] == edited
    assert not any("Blocked" in c for c in w.comments(KEY))


async def test_new_sessions_wait_while_the_machine_is_short_of_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    w.submit(KEY)
    room: dict[str, str | None] = {"why": "only 1.0 GB is free on the disk holding /x"}
    monkeypatch.setattr("delivery.supervisor.short_of_room", lambda *a: room["why"])
    shown: list[str] = []
    async with Supervisor(w.deps, emit=shown.append) as sup:
        await sup.deps.repo.ensure()
        report = await sup.poll_once()
        assert report.started == [] and any("only 1.0 GB" in x["reason"] for x in report.waiting)
        assert w.jira.status_of(KEY) is Status.READY_REFINEMENT
        status = await sup.handle({"cmd": "status"})
        assert "only 1.0 GB" in status["new_sessions_wait"]
        room["why"] = None
        assert await step(sup) == [KEY]
    assert any("new sessions wait: only 1.0 GB" in s for s in shown)
    assert any("there is room again" in s for s in shown)


async def test_a_changed_claude_code_is_probed_before_new_sessions_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, extra={"notifications": {"webhook_env": HOOK}})
    posts = _alerting(w, monkeypatch)
    w.new_ticket(KEY)
    w.submit(KEY)
    version = {"now": "2.1.300 (Claude Code)"}
    release = asyncio.Event()
    # Passes on the first version; fails twice on the next (a single failure is run again).
    results = [(True, ""), (False, "secret read: NOT prevented"), (False, "secret read: NOT prevented")]

    async def current() -> str:
        return version["now"]

    async def probe() -> tuple[bool, str]:
        await release.wait()
        return results.pop(0)

    clock = {"t": 0.0}
    async with Supervisor(w.deps) as sup:
        await sup.deps.repo.ensure()
        guard = VersionGuard(
            w.cfg, sup.alerts, sup.emit, version=current, probe=probe, clock=lambda: clock["t"]
        )
        guard.enabled = True
        sup.version_guard = guard
        await guard.tick()
        assert sup.launch_hold and "2.1.300" in sup.launch_hold
        assert (await sup.poll_once()).started == []
        release.set()
        await asyncio.sleep(0.05)
        await guard.tick()
        assert sup.launch_hold is None and proven(w.cfg, "2.1.300 (Claude Code)")
        assert await step(sup) == [KEY]
        # The next update fails the probe: new sessions keep waiting, and it alerts.
        version["now"] = "2.1.301 (Claude Code)"
        clock["t"] += 1000
        await guard.tick()
        await asyncio.sleep(0.05)
        await guard.tick()  # first failure: run once more
        assert sup.launch_hold and "checking its sandbox" in sup.launch_hold
        await asyncio.sleep(0.05)
        await guard.tick()
        assert sup.launch_hold and "failed the sandbox probe" in sup.launch_hold
        # A pass recorded afterwards by `delivery doctor --claude-probe` ends the wait at once.
        write_stamp(w.cfg, "2.1.301 (Claude Code)")
        await guard.tick()
        assert sup.launch_hold is None
    assert any("2.1.301" in p and "NOT prevented" in p for p in posts)


async def test_operator_notices_stay_off_the_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = make_world(tmp_path, extra={"notifications": {"operational": "operator", "webhook_env": HOOK}})
    posts = _alerting(w, monkeypatch)
    w.scenario({"refine-ticket": [{"usage_limit": True}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        await asyncio.sleep(0.05)
        assert sup.record.claude_unavailable is not None
    assert not any("waiting for Claude" in c for c in w.comments(KEY))
    assert any("usage limit" in p and KEY in p for p in posts)


async def test_an_error_in_a_poll_is_alerted_and_the_coordinator_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, extra={"notifications": {"webhook_env": HOOK}})
    posts = _alerting(w, monkeypatch)
    monkeypatch.setattr("delivery.supervisor.BACKOFF_MIN_SECONDS", 0.01)
    shown: list[str] = []
    async with Supervisor(w.deps, emit=shown.append) as sup:
        calls = 0

        async def tick(once: bool) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            sup.stop_event.set()

        sup._tick = tick  # type: ignore[method-assign]
        await asyncio.wait_for(sup.run(), timeout=60)
    assert calls == 2
    assert any("internal error (RuntimeError: boom); carrying on" in s for s in shown)
    assert any("RuntimeError: boom" in p for p in posts)


async def test_an_unexpected_stop_is_reported_on_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, extra={"notifications": {"webhook_env": HOOK}})
    posts = _alerting(w, monkeypatch)
    async with Supervisor(w.deps):
        pass
    rec = w.deps.store.load_supervisor()
    assert rec is not None and rec.stopped_at is not None
    w.deps.store.save_supervisor(rec.model_copy(update={"stopped_at": None}))  # as if killed
    shown: list[str] = []
    async with Supervisor(w.deps, emit=shown.append):
        pass
    assert any("stopped unexpectedly" in s for s in shown)
    assert any("restarted after an unexpected stop" in p for p in posts)
    shown.clear()
    async with Supervisor(w.deps, emit=shown.append):  # a clean stop says nothing
        pass
    assert not any("stopped unexpectedly" in s for s in shown)
