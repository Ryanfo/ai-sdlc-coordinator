"""`delivery revert`: a revert pull request, a Bug for the rework, and a comment."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import DEV
from delivery.revert import RevertError, done_before, revert_release
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import sh
from harness import REVIEWER, World, make_world, step

KEY = "PILOT-1"


async def _to_done(w: World, sup: Supervisor, how: str) -> int:
    w.new_ticket(KEY, summary="Search tasks")
    w.submit(KEY)
    await step(sup)
    w.move(KEY, Status.READY_PLANNING)
    await step(sup)
    w.move(KEY, Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    pr = w.record(KEY).pr_number
    assert pr
    w.github.approve(pr, REVIEWER)
    w.move(KEY, Status.ACCEPTANCE_REVIEW)
    w.move(KEY, Status.READY_RELEASE_PREPARATION)
    await step(sup)
    w.move(KEY, Status.READY_RELEASE)
    w.github.merge(pr, how=how)
    await step(sup)  # the merge is the release: recorded and verified
    assert w.jira.status_of(KEY) is Status.DONE, w.last_comment(KEY)
    return pr


@pytest.mark.parametrize("how", ["merge", "squash"])
async def test_a_released_ticket_is_reverted_by_pull_request(tmp_path: Path, how: str) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _to_done(w, sup, how)
    assert sh("show", "main:src/pilot-1.ts", cwd=w.origin) == "export const pilot_1 = true;"
    before = set(w.jira.issues)
    done = await revert_release(w.cfg, w.jira, w.github, KEY, "search is too slow in production")
    # A pull request that takes the change out; nothing is merged.
    revert = next(p for p in w.github.prs.values() if p.url == done["revert_pr"])
    assert revert.state == "open" and revert.base_ref == "main" and not revert.merged
    assert revert.title == f"Revert {KEY}: Search tasks"
    assert f"Reverts #{pr} ({KEY})" in revert.body and "search is too slow" in revert.body
    gone = sh("ls-tree", "--name-only", revert.head_sha, "src/", cwd=w.origin)
    assert "pilot-1.ts" not in gone
    assert sh("show", "main:src/pilot-1.ts", cwd=w.origin) == "export const pilot_1 = true;"
    # A Bug for the rework, linked, and a comment with both.
    (bug,) = set(w.jira.issues) - before
    issue = w.jira.issues[bug]
    assert done["bug"] == bug and issue.issue_type == "Bug" and issue.status is Status.BACKLOG
    assert issue.summary == f"Rework {KEY}: Search tasks" and "search is too slow" in issue.description
    assert [link.other_key for link in issue.links] == [KEY]
    text = w.last_comment(KEY)
    assert "Release being reverted" in text and done["revert_pr"] in text and bug in text
    assert done_before(w.cfg, KEY) == done


async def test_only_a_merged_ticket_can_be_reverted(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    with pytest.raises(RevertError, match="no pull request"):
        await revert_release(w.cfg, w.jira, w.github, KEY, "")
    async with Supervisor(w.deps) as sup:
        w.submit(KEY, DEV)
        await step(sup)
        w.move(KEY, Status.READY_PLANNING)
        await step(sup)
        w.move(KEY, Status.READY_DEVELOPMENT)
        await step(sup)
    with pytest.raises(RevertError, match="is not merged"):
        await revert_release(w.cfg, w.jira, w.github, KEY, "")
