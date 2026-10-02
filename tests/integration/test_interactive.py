"""Interactive sessions in tmux: result hand-off, sessions left open, follow-up changes.

Real supervisor, Git and tmux; fake Jira, GitHub and an interactive fake Claude that runs the
coordinator's hooks and reads what is typed into its tmux pane.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from conftest import DEV
from delivery.models import GateKind, GateState
from delivery.open_sessions import SessionRegistry
from delivery.session_hook import read_events
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, make_world, step

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
KEY = "PILOT-1"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = make_world(tmp_path, interactive=True)
    yield w
    subprocess.run(["tmux", "-L", w.cfg.claude.interactive.socket, "kill-server"], capture_output=True)


def _tmux(w: World, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", w.cfg.claude.interactive.socket, *args], capture_output=True, text=True, check=False
    )


def _type(w: World, session: str, text: str) -> None:
    """A person typing into the session's window (tmux reads a trailing ';' as a separator)."""
    _tmux(w, "send-keys", "-t", f"={session}:", "-l", text)
    _tmux(w, "send-keys", "-t", f"={session}:", "Enter")


def _alive(w: World, session: str) -> bool:
    return _tmux(w, "has-session", "-t", f"={session}").returncode == 0


async def _until(cond: Callable[[], bool], timeout: float = 20) -> None:
    for _ in range(int(timeout * 10)):
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


def _open(w: World, procedure: str):  # type: ignore[no-untyped-def]
    found = [r for r in SessionRegistry(w.cfg.runtime.state_dir).all() if r.procedure == procedure]
    return found[0] if found else None


def _stops(rec) -> int:  # type: ignore[no-untyped-def]
    return sum(e.get("event") == "stop" for e in read_events(Path(rec.session_dir)))


def _show(w: World, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(w.origin), "show", ref], capture_output=True, text=True, check=False
    ).stdout


async def _to_development(w: World, sup: Supervisor) -> None:
    await step(sup)
    w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)


async def test_development_session_stays_open_and_follow_ups_are_published(world: World) -> None:
    w = world
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        assert sup.open is not None
        await _to_development(w, sup)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        c1 = w.record(KEY)
        dev = _open(w, "implement-ticket")
        assert dev is not None and _alive(w, dev.name)
        assert dev.base_sha == c1.candidate_sha and dev.candidate_number == 1
        assert Path(dev.worktree).is_dir(), "the open session keeps its worktree"
        # The session ran in tmux with exactly the coordinator's environment.
        inv = next(i for i in w.invocations() if i["procedure"] == "implement-ticket")
        assert inv["interactive"] and "TMPDIR" in inv["env_keys"] and "TMUX" not in inv["env_keys"]

        # The developer asks for a change while the ticket waits for verification.
        stops = _stops(dev)
        _type(w, dev.name, "EDIT src/followup.ts export const followup = 1")
        await _until(lambda: _stops(dev) > stops)
        await sup.open.tick()
        c2 = w.record(KEY)
        assert c2.candidate_number == 2 and c2.candidate_sha != c1.candidate_sha
        assert _show(w, f"feature/{KEY}:src/followup.ts") == "export const followup = 1\n"
        assert "EDIT src/followup.ts" in _show(w, f"feature/{KEY}")  # the request is in the commit
        assert "Follow-up change: candidate c2" in w.last_comment(KEY)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION  # no move needed

        # Verification runs on the new candidate; then another change sends it back.
        await step(sup)
        assert w.jira.status_of(KEY) is Status.CODE_REVIEW, w.last_comment(KEY)
        dev = _open(w, "implement-ticket")
        assert dev is not None
        stops = _stops(dev)
        _type(w, dev.name, "EDIT src/followup.ts export const followup = 2")
        await _until(lambda: _stops(dev) > stops)
        await sup.open.tick()
        c3 = w.record(KEY)
        assert c3.candidate_number == 3
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION
        assert "moved from Code review back to Ready for verification" in w.last_comment(KEY)
        code = [g for g in c3.gates if g.kind is GateKind.CODE]
        assert code and all(g.state is GateState.SUPERSEDED for g in code), "c2's code gate no longer counts"
        # The coordinator's own move back is accepted: verification runs again, on c3.
        await step(sup)
        assert w.jira.status_of(KEY) is Status.CODE_REVIEW, w.last_comment(KEY)
        assert w.token(KEY, "CODE").endswith("3")

        # Questions only: no changes, nothing published.
        stops = _stops(dev)
        _type(w, dev.name, "why did you name it followup?")
        await _until(lambda: _stops(dev) > stops)
        await sup.open.tick()
        assert w.record(KEY).candidate_number == 3

        # /exit closes it: the conversation is kept with the run's logs, the worktree goes.
        _type(w, dev.name, "/exit")
        await _until(lambda: not _alive(w, dev.name))
        await sup.open.tick()
        assert _open(w, "implement-ticket") is None
        assert not Path(dev.worktree).exists()
        after = Path(dev.journal_dir) / "logs" / "claude-implement-ticket-after.jsonl"
        assert "why did you name it followup?" in after.read_text()


async def test_stop_hook_keeps_claude_working_until_the_result_is_valid(world: World) -> None:
    w = world
    w.scenario({"refine-ticket": [{"forget_result": 2}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW, w.last_comment(KEY)
    rec = _open(w, "refine-ticket")
    assert rec is not None
    results = [e.get("result") for e in read_events(Path(rec.session_dir)) if e.get("event") == "stop"]
    assert results == ["blocked", "blocked", "valid"]
    log = (Path(rec.journal_dir) / "logs" / "claude-refine-ticket.txt").read_text()
    assert "Finished: success" in log


async def test_a_session_that_never_hands_over_a_result_blocks_the_stage(world: World) -> None:
    w = world
    w.scenario({"refine-ticket": [{"forget_result": 9, "never_result": True}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
    assert w.jira.status_of(KEY) is Status.BLOCKED
    assert "without a valid result" in w.last_comment(KEY)
    assert SessionRegistry(w.cfg.runtime.state_dir).all() == []
    assert _tmux(w, "list-sessions").stdout.strip() == "", "a failed session is not left running"


@pytest.mark.parametrize(
    ("behaviour", "expected"),
    [
        ({"usage_limit": True}, "usage limit"),
        ({"no_plugin": True}, "delivery plugin did not load"),
    ],
)
async def test_provider_and_plugin_failures_are_recognised(
    world: World, behaviour: dict[str, bool], expected: str
) -> None:
    w = world
    w.scenario({"refine-ticket": [behaviour]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
    assert w.jira.status_of(KEY) is Status.BLOCKED
    assert expected in w.last_comment(KEY)


async def test_a_new_development_run_closes_the_open_session_and_keeps_its_changes(world: World) -> None:
    w = world
    w.scenario(
        {
            "implement-ticket": [
                {"edit": {"src/a.ts": "draft\n"}, "outcome": "blocked", "blocker_reason": "need a decision"},
                {"edit": {"src/b.ts": "done\n"}},
            ]
        }
    )
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await _to_development(w, sup)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.BLOCKED
        dev = _open(w, "implement-ticket")
        assert dev is not None and dev.base_sha is None
        # The developer fixes things up in the session that stayed open, then resumes.
        _type(w, dev.name, "EDIT src/fix.ts fixed")
        await _until(lambda: (Path(dev.worktree) / "src" / "fix.ts").exists())
        await _until(lambda: _stops(dev) >= 2)
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        new = _open(w, "implement-ticket")
        assert new is not None and new.run_id != dev.run_id, "the old session was closed first"
        assert not Path(dev.worktree).exists()
    # The candidate has the blocked run's draft, the fix typed in its session and the new work.
    assert _show(w, f"feature/{KEY}:src/a.ts") == "draft\n"
    assert _show(w, f"feature/{KEY}:src/fix.ts") == "fixed\n"
    assert _show(w, f"feature/{KEY}:src/b.ts") == "done\n"
    events = (Path(dev.journal_dir) / "events.jsonl").read_text()
    assert "wip_refreshed" in events and json.loads(events.splitlines()[0])
