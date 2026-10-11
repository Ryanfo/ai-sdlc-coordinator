"""Auto mode: nobody reviews, the coordinator works through the board one ticket at a time.

It makes every person's move itself (approve, answer, submit fixes, accept), merges the PR, and
then submits the next Backlog ticket, top of the board first. A ticket that needs another one is
held until that one is delivered. Anything it cannot finish is left for a person.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from delivery.ports import IssueLink
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, make_world, step

FIXED = {"edit": {"src/search.ts": "export const search = (xs: string[]) => xs;\n"}}
MAJOR = [{"id": "F1", "severity": "major", "description": "the search ignores the last item"}]


def _auto(tmp_path: Path, **kw: Any) -> World:
    w = make_world(tmp_path, **kw)
    w.cfg = w.cfg.for_auto()
    w.deps.cfg = w.cfg
    return w


def _backlog(w: World, key: str, rank: int, **kw: Any) -> None:
    w.jira.create(
        key, f"Feature {key}", "Users need search. AC1: title search is case-insensitive.", None, **kw
    )
    w.jira.issues[key].rank = rank


async def _until(sup: Supervisor, done: Callable[[], bool], polls: int = 40) -> None:
    for _ in range(polls):
        if done():
            return
        await step(sup)
    jira = sup.deps.jira
    where = {k: (i.status.value, (jira.comments_by_key[k] or [None])[-1]) for k, i in jira.issues.items()}
    raise AssertionError(f"auto mode did not get there: {where}")


def _status(w: World, key: str) -> Status:
    return w.jira.status_of(key)


async def test_works_through_the_board_in_rank_order_and_merges(tmp_path: Path) -> None:
    w = _auto(tmp_path)
    _backlog(w, "PILOT-1", rank=2)
    _backlog(w, "PILOT-2", rank=1)  # top of the board
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert _status(w, "PILOT-1") is Status.BACKLOG
        assert w.jira.issues["PILOT-2"].assignee == w.cfg.identity.developer_jira_account_id
        await _until(sup, lambda: _status(w, "PILOT-1") is Status.DONE)
    # One at a time: the next ticket starts only once the previous one is merged and Done.
    done = w.cfg.status_id(Status.DONE)
    finished = next(c.created for c in w.jira.changes_by_key["PILOT-2"] if c.to_id == done)
    assert w.jira.changes_by_key["PILOT-1"][0].created > finished
    pr = w.record("PILOT-2").pr_number
    assert pr and w.github.prs[pr].merged
    assert "PR #" in w.last_comment("PILOT-2") and "Released" in w.last_comment("PILOT-2")
    assert any("Auto mode: code approved" in c for c in w.comments("PILOT-2"))
    assert w.envelope("refine-ticket")["autonomous"] is True


async def test_failed_review_is_fixed_automatically(tmp_path: Path) -> None:
    w = _auto(tmp_path)
    _backlog(w, "PILOT-1", rank=1)
    w.scenario({"review-ticket": [{"findings": MAJOR}, {}], "implement-ticket": [{}, FIXED]})
    async with Supervisor(w.deps) as sup:
        await _until(sup, lambda: _status(w, "PILOT-1") is Status.DONE)
    devs = [i for i in w.invocations() if "/delivery:implement-ticket" in " ".join(i["argv"])]
    assert len(devs) == 2
    assert "F1" in w.envelope("implement-ticket")["feedback_items"]


async def test_fix_rounds_used_up_leaves_the_ticket_and_moves_on(tmp_path: Path) -> None:
    w = _auto(tmp_path, extra={"auto": {"review_rounds": 1}})
    _backlog(w, "PILOT-1", rank=1)
    _backlog(w, "PILOT-2", rank=2)
    w.scenario({"PILOT-1:review-ticket": [{"findings": MAJOR}], "PILOT-1:implement-ticket": [{}, FIXED]})
    async with Supervisor(w.deps) as sup:
        await _until(sup, lambda: _status(w, "PILOT-2") is Status.DONE)
    assert _status(w, "PILOT-1") is Status.CHANGES_REQUESTED
    assert "left for you" in w.last_comment("PILOT-1")


async def test_questions_are_answered_by_assuming(tmp_path: Path) -> None:
    w = _auto(tmp_path)
    _backlog(w, "PILOT-1", rank=1)
    q = [{"id": "Q1", "question": "Exact match or prefix?", "rationale": "scope"}]
    w.scenario({"refine-ticket": [{"outcome": "needs_clarification", "questions": q}, {}]})
    async with Supervisor(w.deps) as sup:
        await _until(sup, lambda: _status(w, "PILOT-1") is Status.DONE)
    assert any("Auto mode: no answers" in c for c in w.comments("PILOT-1"))
    assert not any("trying again" in c for c in w.comments("PILOT-1"))


async def test_a_ticket_blocked_by_another_is_preceded_by_it(tmp_path: Path) -> None:
    w = _auto(tmp_path)
    _backlog(w, "PILOT-1", rank=1)
    _backlog(w, "PILOT-2", rank=2)
    w.jira.issues["PILOT-1"].links.append(IssueLink("Blocks", "inward", "is blocked by", "PILOT-2"))
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert _status(w, "PILOT-1") is Status.BACKLOG
        assert _status(w, "PILOT-2") is not Status.BACKLOG
        await _until(sup, lambda: _status(w, "PILOT-1") is Status.DONE)


async def test_a_stage_blocked_on_another_ticket_waits_for_it_then_resumes(tmp_path: Path) -> None:
    w = _auto(tmp_path)
    _backlog(w, "PILOT-1", rank=1)
    _backlog(w, "PILOT-2", rank=2)
    w.scenario(
        {
            "PILOT-1:implement-ticket": [
                {"outcome": "blocked", "blocker_reason": "Depends on PILOT-2 (the search index)."},
                {},
            ]
        }
    )
    async with Supervisor(w.deps) as sup:
        await _until(sup, lambda: _status(w, "PILOT-1") is Status.BLOCKED)
        await _until(sup, lambda: _status(w, "PILOT-2") is Status.DONE)
        assert any("waiting for PILOT-2" in c for c in w.comments("PILOT-1"))
        await _until(sup, lambda: _status(w, "PILOT-1") is Status.DONE)
    assert any("dependency delivered" in c for c in w.comments("PILOT-1"))


async def test_a_pr_github_will_not_merge_is_left_for_a_person(tmp_path: Path) -> None:
    w = _auto(tmp_path)
    _backlog(w, "PILOT-1", rank=1)
    w.github.refuse_merge = "Merge method squash is not allowed on this repository"
    async with Supervisor(w.deps) as sup:
        await _until(sup, lambda: "PILOT-1" in sup.autopilot.state.stuck if sup.autopilot else False)
    assert _status(w, "PILOT-1") is Status.READY_RELEASE
    assert "would not merge" in w.last_comment("PILOT-1")


async def test_without_auto_mode_nothing_is_approved_or_merged(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    _backlog(w, "PILOT-1", rank=1)
    async with Supervisor(w.deps) as sup:
        assert sup.autopilot is None
        await step(sup)
    assert _status(w, "PILOT-1") is Status.BACKLOG
