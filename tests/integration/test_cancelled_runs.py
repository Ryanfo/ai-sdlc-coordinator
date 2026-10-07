"""A ticket cancelled in Jira after its run stopped does not stay waiting in the journal or the office."""

from __future__ import annotations

from pathlib import Path

from conftest import DEV
from delivery.models import RunState
from delivery.office import OfficeFeed
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import sh
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


async def _to_code_review(w, sup: Supervisor, key: str) -> None:  # type: ignore[no-untyped-def]
    w.new_ticket(key)
    w.submit(key)
    await step(sup)
    w.move(key, Status.READY_PLANNING)
    await step(sup)
    w.move(key, Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    assert w.jira.status_of(key) is Status.CODE_REVIEW, w.last_comment(key)


def _files(origin: Path, branch: str) -> list[str]:
    out = sh("ls-tree", "-r", "--name-only", branch, cwd=origin)
    return [ln for ln in out.splitlines() if ln.startswith("docs/delivery/")]


def _branches(origin: Path) -> list[str]:
    return sh("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=origin).split()


async def test_cancelled_ticket_leaves_only_its_spec_and_plan(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    key = "PILOT-1"
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup, key)
        pr = w.record(key).pr_number
        assert pr and w.github.prs[pr].state == "open"
        assert f"feature/{key}" in _branches(w.origin)
        before = _files(w.origin, f"delivery/{key}")
        assert any("/executions/" in f for f in before)

        w.jira.human_move(key, Status.CANCELLED, DEV)
        await step(sup)

        assert _state(w, key) is RunState.CANCELLED
        assert w.github.prs[pr].state == "closed" and not w.github.prs[pr].merged
        assert "cancelled" in w.github.closed_comments[pr]
        assert f"feature/{key}" not in _branches(w.origin)
        after = _files(w.origin, f"delivery/{key}")
        root = f"docs/delivery/{key}"
        assert sorted(after) == [f"{root}/plan/v001.md", f"{root}/specification/v001.md"]
        for name in after:
            assert sh("show", f"delivery/{key}:{name}", cwd=w.origin).startswith(
                "<!-- delivery: ticket cancelled -->"
            )
        rec = w.deps.store.latest_run(key).record  # type: ignore[union-attr]
        assert rec and rec.tidied
        assert not (w.cfg.repository.worktree_root / key).exists()

        # Tidied once: another poll adds nothing.
        head = sh("rev-parse", f"delivery/{key}", cwd=w.origin)
        await step(sup)
        assert sh("rev-parse", f"delivery/{key}", cwd=w.origin) == head


async def test_a_failed_tidy_is_retried_and_does_not_hold_up_the_close(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    key = "PILOT-1"
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup, key)
        w.github.fail_branch_delete = True
        w.jira.human_move(key, Status.CANCELLED, DEV)
        await step(sup)
        rec = w.deps.store.latest_run(key).record  # type: ignore[union-attr]
        assert rec and rec.state is RunState.CANCELLED and not rec.tidied
        w.github.fail_branch_delete = False
        await step(sup)
        rec = w.deps.store.latest_run(key).record  # type: ignore[union-attr]
        assert rec and rec.tidied
        assert f"feature/{key}" not in _branches(w.origin)


async def test_a_merged_pr_is_left_alone(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    key = "PILOT-1"
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup, key)
        pr = w.record(key).pr_number
        assert pr
        w.github.merge(pr)
        w.jira.human_move(key, Status.CANCELLED, DEV)
        await step(sup)
        assert any("/executions/" in f for f in _files(w.origin, f"delivery/{key}"))
