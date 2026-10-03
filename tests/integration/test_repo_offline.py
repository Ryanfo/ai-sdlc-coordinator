"""The application repository is unreachable when the supervisor starts (GitHub or network down)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from delivery import supervisor
from delivery.ports import UncertainResult
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import drain, make_world

UNREACHABLE = (
    "git fetch failed: fatal: unable to access 'https://github.com/example/app.git/':\n"
    "Could not resolve host: github.com"
)


async def _until(cond: Callable[[], bool], timeout: float = 30) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.05)


async def test_unreachable_repository_at_start_retries_then_starts_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "BACKOFF_MIN_SECONDS", 0.05)
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    real = w.repo.ensure
    attempts: list[tuple[int, list[str], Status]] = []  # what had happened by each attempt

    async def flaky() -> None:
        searches = [op for op, _ in w.jira.calls if op == "search"]
        attempts.append((len(sup.sessions), searches, w.jira.status_of("PILOT-1")))
        if len(attempts) <= 2:
            raise UncertainResult(UNREACHABLE)
        await real()

    monkeypatch.setattr(w.repo, "ensure", flaky)
    lines: list[str] = []
    async with Supervisor(w.deps, emit=lines.append) as sup:
        run = asyncio.create_task(sup.run())
        await _until(lambda: bool(sup.sessions) or w.jira.status_of("PILOT-1") is not Status.READY_REFINEMENT)
        assert not run.done()
        await drain(sup)
        sup.stop_event.set()
        await asyncio.wait_for(run, timeout=10)
    # Nothing was polled or dispatched until the third attempt reached the repository.
    assert attempts == [(0, [], Status.READY_REFINEMENT)] * 3
    retries = [line for line in lines if "could not reach the application repository" in line]
    assert len(retries) == 2
    assert all("\n" not in r and "Could not resolve host: github.com); retrying in" in r for r in retries)
    assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW


@pytest.mark.parametrize("hang", [False, True], ids=["between-retries", "mid-fetch"])
async def test_stop_while_waiting_for_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hang: bool
) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    entered = asyncio.Event()
    cancelled = False

    async def unreachable() -> None:
        nonlocal cancelled
        entered.set()
        if not hang:
            raise UncertainResult(UNREACHABLE)
        try:
            await asyncio.Event().wait()  # a fetch stalled on the network
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(w.repo, "ensure", unreachable)
    lines: list[str] = []
    async with Supervisor(w.deps, emit=lines.append) as sup:
        run = asyncio.create_task(sup.run())
        await asyncio.wait_for(entered.wait(), timeout=10)
        await asyncio.sleep(0.1)
        assert not run.done()
        refused = await sup.handle({"cmd": "poll"})
        assert refused["ok"] is False and "application repository" in refused["error"]
        assert (await sup.handle({"cmd": "status"}))["ok"] is True
        sup.stop_event.set()  # what Ctrl-C and SIGTERM do
        await asyncio.wait_for(run, timeout=5)  # well inside the 15s backoff or the stalled fetch
        assert not sup.sessions
    assert cancelled is hang
    assert any("retrying in 15s" in line for line in lines) is not hang
    assert not [op for op, _ in w.jira.calls if op == "search"]
    assert w.jira.status_of("PILOT-1") is Status.READY_REFINEMENT
