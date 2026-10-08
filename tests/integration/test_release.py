"""Release: the human merge is the release, and only an accepted candidate can reach Done.

The coordinator reads the merge from GitHub, checks it contains the accepted candidate (a merge
that does not is flagged on the ticket, never blocked: it has happened) and moves the ticket to
Done. Accepting the delivery goes straight to Ready for release; there is no release stage.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import external_commit, sh
from harness import REVIEWER, World, make_world, step


async def _accepted(w: World, sup: Supervisor, key: str, *, approve_review: bool = True) -> int:
    w.new_ticket(key)
    w.submit(key)
    await step(sup)
    w.move(key, Status.READY_PLANNING)
    await step(sup)
    w.move(key, Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    rec = w.record(key)
    assert rec.pr_number
    if approve_review:
        w.github.approve(rec.pr_number, REVIEWER)
    w.move(key, Status.ACCEPTANCE_REVIEW)
    return rec.pr_number


async def _to_ready_release(w: World, sup: Supervisor, key: str) -> int:
    """Accepted: waiting for the merge."""
    pr = await _accepted(w, sup, key)
    w.move(key, Status.READY_RELEASE)
    return pr


def _world(tmp_path: Path) -> World:
    return make_world(tmp_path)


async def test_squash_merge_provenance_reaches_done(tmp_path: Path) -> None:
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _to_ready_release(w, sup, "PILOT-1")
        merged = w.github.merge(pr, how="squash")
        assert await step(sup) == []  # nothing is started: the poll completes the ticket
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    assert w.jira.issues["PILOT-1"].resolution == "Done"
    assert merged in w.last_comment("PILOT-1")
    assert f"PR #{pr} merged" in w.last_comment("PILOT-1")


async def test_unapproved_candidate_merged_is_flagged_not_blocked(tmp_path: Path) -> None:
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _to_ready_release(w, sup, "PILOT-1")
        external_commit(tmp_path, w.origin, "feature/PILOT-1", "src/sneaky.ts", "x\n", "sneaky")
        w.github.merge(pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    assert "Look at this:" in w.last_comment("PILOT-1")
    assert "unapproved changes were merged" in w.last_comment("PILOT-1")


async def test_conflict_resolved_when_merging_is_accepted_and_flagged(tmp_path: Path) -> None:
    # main moved on and conflicts; the human resolves it in the PR (merging main into the
    # branch, as GitHub's Resolve conflicts does) and then merges.
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _to_ready_release(w, sup, "PILOT-1")
        external_commit(tmp_path, w.origin, "main", "src/pilot-1.ts", "export const main = 1;\n", "m")
        work = tmp_path / "resolve"
        sh("clone", str(w.origin), str(work), cwd=tmp_path)
        sh("checkout", "feature/PILOT-1", cwd=work)
        subprocess.run(["git", "merge", "origin/main"], cwd=work, capture_output=True)
        (work / "src" / "pilot-1.ts").write_text("export const pilot_1 = true;\nexport const main = 1;\n")
        sh("commit", "-am", "Resolve conflicts with main", cwd=work)
        sh("push", "origin", "HEAD:refs/heads/feature/PILOT-1", cwd=work)
        w.github.merge(pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")


async def test_merge_is_read_from_github_and_completes_the_ticket(tmp_path: Path) -> None:
    # Nobody types the release into Jira: the human merge is the release.
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _to_ready_release(w, sup, "PILOT-1")
        before = len(w.comments("PILOT-1"))
        assert await step(sup) == []
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE  # waits for the merge, quietly
        assert len(w.comments("PILOT-1")) == before
        merged = w.github.merge(pr, merged_by="release-owner", how="squash")
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    assert "merged by release-owner" in w.last_comment("PILOT-1")
    record = w.record("PILOT-1").release["record"]
    assert record["source"] == "github"
    assert (record["commit"], record["environment"], record["merged_pr"]) == (merged, "local-pilot", pr)
    assert not any("RECORD RELEASE" in c for c in w.comments("PILOT-1"))


async def test_a_release_that_cannot_be_completed_in_jira_is_retried_next_poll(tmp_path: Path) -> None:
    # For example a Jira condition on the transition to Done.
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _to_ready_release(w, sup, "PILOT-1")
        w.github.merge(pr)
        w.jira.drop_routes.add((Status.READY_RELEASE, Status.DONE))
        report = await sup.poll_once()
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE
        assert any("release not completed" in s["reason"] for s in report.skipped)
        w.jira.drop_routes.clear()
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    # The retry repeats nothing that already happened.
    assert sum(c.startswith("Released: Done") for c in w.comments("PILOT-1")) == 1


async def test_pr_merged_before_acceptance_completes_on_accepting(tmp_path: Path) -> None:
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _accepted(w, sup, "PILOT-1")
        w.github.merge(pr)
        w.move("PILOT-1", Status.READY_RELEASE)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")


async def test_accepting_goes_straight_to_ready_for_release(tmp_path: Path) -> None:
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        await _accepted(w, sup, "PILOT-1")
        await sup.acceptance.tick(full=True)  # posts how to try it and what accepting does
        assert "Accept delivery (moves into Ready for release)" in w.last_comment("PILOT-1")
        w.move("PILOT-1", Status.READY_RELEASE)
        await step(sup)
        # Nothing runs and nothing waits for approval: only the merge is awaited.
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE
        assert [i["procedure"] for i in w.invocations()].count("verify-ticket") == 1


async def test_an_acceptance_by_someone_who_may_not_decide_is_reported_and_waits(tmp_path: Path) -> None:
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _accepted(w, sup, "PILOT-1")
        w.move("PILOT-1", Status.READY_RELEASE, author="stranger")
        w.github.merge(pr)
        report = await sup.poll_once()
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE  # not Done: nobody accepted it
        assert report.waiting and report.waiting[0]["ticket"] == "PILOT-1"
        assert "stranger" in report.waiting[0]["reason"] or "approver" in report.waiting[0]["reason"]


async def test_code_approved_without_an_independent_review_waits_for_it(tmp_path: Path) -> None:
    w = _world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr = await _accepted(w, sup, "PILOT-1", approve_review=False)
        w.move("PILOT-1", Status.READY_RELEASE)
        w.github.merge(pr)
        report = await sup.poll_once()
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE
        assert report.waiting and "code gate evidence" in report.waiting[0]["reason"]
        # Once the review is there the next poll goes on (a merged PR keeps its reviews).
        w.github.approve(pr, REVIEWER)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
