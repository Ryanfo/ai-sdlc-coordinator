"""Interactive sessions in tmux: result hand-off, sessions left open, follow-up changes, the app.

Real supervisor, Git and tmux; fake Jira, GitHub and an interactive fake Claude that runs the
coordinator's hooks and reads what is typed into its tmux pane.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from conftest import DEV
from delivery.cli import main
from delivery.models import GateKind, GateState
from delivery.open_sessions import DOCUMENTS, OpenRecord, SessionRegistry
from delivery.session_hook import read_events
from delivery.supervisor import Supervisor
from delivery.workflow import Stage, Status
from harness import REVIEWER, World, make_world, step

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


async def _ticks(sup: Supervisor, cond: Callable[[], bool], timeout: float = 30) -> None:
    """Run the open-session watcher until ``cond`` holds (as the supervisor does every few seconds)."""
    assert sup.open is not None
    for _ in range(int(timeout * 5)):
        await sup.open.tick()
        if cond():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not met in time")


def _get(url: str) -> str | None:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2) as r:
            return str(r.read().decode())
    except OSError:
        return None


def _prompts(w: World, procedure: str) -> list[str]:
    """The prompts each session of ``procedure`` started with, oldest first."""
    found = sorted((i for i in w.invocations() if i["procedure"] == procedure), key=lambda i: i["started"])
    return [str(i["argv"][0]) for i in found]


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


async def _to_review(w: World, sup: Supervisor, stage: Stage) -> None:
    """Take the ticket to the review status of a stage that publishes a document."""
    await step(sup)
    if stage is Stage.REFINEMENT:
        return
    w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    if stage is Stage.PLANNING:
        return
    w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    pr = w.record(KEY).pr_number
    assert pr is not None
    w.github.approve(pr, REVIEWER)
    w.decide(KEY, f"APPROVE CODE {w.token(KEY, 'CODE')}", Status.ACCEPTANCE_REVIEW)
    w.decide(KEY, f"ACCEPT DELIVERY {w.token(KEY, 'ACCEPT')}", Status.READY_RELEASE_PREPARATION)
    await step(sup)


@pytest.mark.parametrize(
    ("stage", "kind", "folder", "approve", "approved_to", "then"),
    [
        (
            Stage.REFINEMENT,
            "SPEC",
            "specification",
            "APPROVE SPEC",
            Status.READY_PLANNING,
            Status.PLAN_REVIEW,
        ),
        (Stage.PLANNING, "PLAN", "plan", "APPROVE PLAN", Status.READY_DEVELOPMENT, Status.READY_VERIFICATION),
        (
            Stage.RELEASE_PREPARATION,
            "RELEASE",
            "releases",
            "APPROVE RELEASE",
            Status.READY_RELEASE,
            Status.READY_RELEASE,
        ),
    ],
)
async def test_a_document_changed_in_its_open_session_is_published_as_the_next_revision(
    world: World, stage: Stage, kind: str, folder: str, approve: str, approved_to: Status, then: Status
) -> None:
    w = world
    doc = DOCUMENTS[stage]
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        assert sup.open is not None
        await _to_review(w, sup, stage)
        assert w.jira.status_of(KEY) is doc.review, w.last_comment(KEY)
        rec = _open(w, doc.procedure)
        assert rec is not None and rec.revision == 1
        first = w.token(KEY, kind)

        # The developer asks for a change in the session that wrote it. A plan's change can
        # also change its footprint, which Claude keeps in the result file.
        if stage is Stage.PLANNING:
            result = Path(rec.out_dir) / "result.json"
            data = json.loads(result.read_text())
            data["footprint"]["paths"].append("src/descriptions.ts")
            result.write_text(json.dumps(data))
        stops = _stops(rec)
        _type(w, rec.name, f"EDIT {Path(rec.out_dir) / doc.filename} Revised: AC2 also covers descriptions")
        await _until(lambda: _stops(rec) > stops)
        await sup.open.tick()
        second = w.token(KEY, kind)
        assert (first, second) == (f"{KEY}-{kind}-v1", f"{KEY}-{kind}-v2")
        assert w.jira.status_of(KEY) is doc.review, "the ticket stays in review"
        states = {g.token: g.state for g in w.record(KEY).gates}
        assert states[first] is GateState.SUPERSEDED and states[second] is GateState.PENDING
        text = _show(w, f"delivery/{KEY}:docs/delivery/{KEY}/{folder}/v002.md")
        assert "Revised: AC2 also covers descriptions" in text and f"follow_up_of: {first}" in text
        if stage is Stage.PLANNING:
            fp = json.loads(_show(w, f"delivery/{KEY}:docs/delivery/{KEY}/plan/v002.footprint.json"))
            assert fp["plan_revision"] == 2 and "src/descriptions.ts" in fp["paths"]
            assert w.record(KEY).footprint_ref["revision"] == 2  # type: ignore[index]
        gate_comment = w.last_comment(KEY)
        assert second in gate_comment and "Follow-up revision" in gate_comment
        assert first in gate_comment and "AC2 also covers descriptions" in gate_comment
        rec = _open(w, doc.procedure)
        assert rec is not None and rec.revision == 2 and not rec.held

        # A question changes nothing.
        stops = _stops(rec)
        _type(w, rec.name, "why that wording?")
        await _until(lambda: _stops(rec) > stops)
        await sup.open.tick()
        assert w.token(KEY, kind) == second

        # Approving the follow-up revision moves the ticket on; changes made after that wait.
        w.decide(KEY, f"{approve} {second}", approved_to)
        await step(sup)
        assert w.jira.status_of(KEY) is then, w.last_comment(KEY)
        stops = _stops(rec)
        _type(w, rec.name, f"EDIT {Path(rec.out_dir) / doc.filename} One more change")
        await _until(lambda: _stops(rec) > stops)
        await sup.open.tick()
        waiting = _open(w, doc.procedure)
        assert waiting is not None and w.token(KEY, kind) == second
        assert f"a changed {doc.title} is published only while the ticket is in" in waiting.held


async def test_change_requests_and_development_end_by_asking_what_else(world: World) -> None:
    w = world
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.decide(
            KEY,
            f"CHANGE SPEC {w.token(KEY, 'SPEC')}\nF1: Search must also match descriptions.",
            Status.READY_REFINEMENT,
        )
        await step(sup)
        assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW, w.last_comment(KEY)
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)
        await step(sup)
    ask = "Are there any further changes you'd like to make?"
    first, changed = _prompts(w, "refine-ticket")
    assert ask not in first, "a first draft is not a change request"
    assert "change requests from Jira: F1" in changed and "have been actioned" in changed
    assert ask in changed and "close this window" in changed
    assert "next revision of the specification" in changed
    assert "specification.md in place" in changed.split("After writing the result file")[1]
    (plan,) = _prompts(w, "plan-ticket")
    assert ask not in plan
    (dev,) = _prompts(w, "implement-ticket")
    assert ask in dev and "next candidate" in dev and "actioned" not in dev
    assert "starting the app" not in dev, "no app preview is configured"


@pytest.fixture
def app_world(tmp_path: Path) -> Iterator[World]:
    server = [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"]
    w = make_world(
        tmp_path,
        interactive=True,
        extra={"preview": {"command": server, "setup": [], "url": "http://127.0.0.1:{port}/"}},
    )
    yield w
    subprocess.run(["tmux", "-L", w.cfg.claude.interactive.socket, "kill-server"], capture_output=True)


async def test_the_app_runs_from_the_finished_development_worktree(app_world: World) -> None:
    w = app_world
    opened: list[str] = []

    async def browser(url: str) -> str | None:
        opened.append(url)
        return None

    def app() -> OpenRecord | None:
        rec = _open(w, "implement-ticket")
        return rec if rec is not None and rec.preview is not None else None

    def ready() -> bool:
        rec = app()
        return rec is not None and rec.preview is not None and rec.preview.state == "ready"

    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        assert sup.open is not None
        sup.open.previews.opener = browser
        await _to_development(w, sup)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        (prompt,) = _prompts(w, "implement-ticket")
        assert "starting the app from this working copy" in prompt

        # Once development has finished, the app runs from its worktree and the browser opens.
        await _ticks(sup, ready)
        rec = app()
        assert rec is not None and rec.preview is not None
        url = rec.preview.url
        assert opened == [url] and url != "http://127.0.0.1:{port}/"
        assert _get(url + "src/pilot-1.ts") == "export const pilot_1 = true;\n"

        # A change asked for in the session shows up in the running app.
        _type(w, rec.name, "EDIT src/tweak.ts tweaked")
        await _until(lambda: _get(url + "src/tweak.ts") == "tweaked\n")

        # It stopped: `delivery preview` asks the coordinator to start it again.
        _tmux(w, "kill-session", "-t", f"={rec.preview.name}")
        await _ticks(
            sup, lambda: (r := app()) is not None and r.preview is not None and r.preview.state == "stopped"
        )
        assert main(["preview", KEY, "--config", str(w.cfg.source_path)]) == 0
        await _ticks(sup, ready)
        again = app()
        assert again is not None and again.preview is not None and len(opened) == 2
        url = again.preview.url
        assert _get(url + "src/tweak.ts") == "tweaked\n"

        # Closing the development session stops the app.
        _type(w, rec.name, "/exit")
        await _ticks(sup, lambda: _open(w, "implement-ticket") is None)
        assert not _alive(w, again.preview.name)
        assert _get(url) is None


async def test_after_actioning_change_requests_the_session_is_brought_up(tmp_path: Path) -> None:
    w = make_world(tmp_path, interactive=True, checks={"unit": ["sh", "-c", "! grep -rq bug src"]})
    w.scenario(
        {
            "implement-ticket": [
                {"edit": {"src/search.ts": "export const s = 1; // bug\n"}},
                {"edit": {"src/search.ts": "export const s = 1;\n"}},
            ]
        }
    )
    opened: list[str] = []

    async def opener(app: str, folder: Path, title: str, attach: list[str]) -> None:
        opened.append(title)

    try:
        w.new_ticket(KEY)
        w.submit(KEY)
        async with Supervisor(w.deps) as sup:
            assert sup.open is not None
            sup.open.opener, sup.open.attach_wait = opener, 0
            await _to_development(w, sup)
            await step(sup)  # c1: no change requests, so nothing is brought up
            await step(sup)  # verification fails: R1 for the failed check
            assert w.jira.status_of(KEY) is Status.CHANGES_REQUESTED
            assert opened == []
            w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
            await step(sup)  # c2 actions R1, publishes, then the session is brought up
            assert w.record(KEY).candidate_number == 2
            assert opened == [f"{KEY} implement-ticket"]
            dev = _open(w, "implement-ticket")
            assert dev is not None and _alive(w, dev.name)
        prompts = [i["argv"][0] for i in w.invocations() if i["procedure"] == "implement-ticket"]
        assert "The changes requested in Jira have been actioned" not in prompts[0]
        assert "change requests from Jira: R1" in prompts[1]
        assert "Are there any further changes you'd like to make?" in prompts[1]
    finally:
        subprocess.run(["tmux", "-L", w.cfg.claude.interactive.socket, "kill-server"], capture_output=True)
