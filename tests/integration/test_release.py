"""Release provenance: only a recorded, accepted candidate can reach Done (handoff §16, M7)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import APPROVER
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import external_commit, sh
from harness import REVIEWER, World, make_world, step


async def _to_ready_release(w: World, sup: Supervisor, key: str) -> tuple[int, str]:
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
    w.github.approve(rec.pr_number, REVIEWER)
    w.move(key, Status.ACCEPTANCE_REVIEW)
    w.move(key, Status.READY_RELEASE_PREPARATION)
    await step(sup)
    rel = w.token(key, "RELEASE")
    w.move(key, Status.READY_RELEASE)
    return rec.pr_number, rel


def _record(w: World, key: str, rel: str, commit: str, pr: int) -> None:
    w.decide(
        key,
        Status.READY_RELEASE_VERIFICATION,
        f"RECORD RELEASE {rel}\ncommit: {commit}\nenvironment: local-pilot\nmerged-pr: {pr}",
    )


async def test_squash_merge_provenance_reaches_done(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr, rel = await _to_ready_release(w, sup, "PILOT-1")
        merged = w.github.merge(pr, how="squash")
        _record(w, "PILOT-1", rel, merged, pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    assert "squash" in w.last_comment("PILOT-1")


async def test_released_commit_containing_later_unrelated_merges_is_accepted(tmp_path: Path) -> None:
    w = make_world(tmp_path, extra={"checks": {"integration": ["unit"]}})
    async with Supervisor(w.deps) as sup:
        pr, rel = await _to_ready_release(w, sup, "PILOT-1")
        w.github.merge(pr)
        later = external_commit(tmp_path, w.origin, "main", "docs/notes.md", "n\n", "later")
        _record(w, "PILOT-1", rel, later, pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")


async def test_unrelated_release_sha_cannot_pass(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr, rel = await _to_ready_release(w, sup, "PILOT-1")
        unrelated = external_commit(tmp_path, w.origin, "main", "x.txt", "x\n", "unrelated")
        w.github.merge(pr)  # merge happens after the recorded commit: recorded SHA lacks it
        _record(w, "PILOT-1", rel, unrelated, pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.BLOCKED
    assert "does not contain merge" in w.last_comment("PILOT-1")
    assert w.record("PILOT-1").pause.resume_stage.value == "release_verification"  # type: ignore[union-attr]


async def test_unapproved_candidate_merged_is_flagged(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr, rel = await _to_ready_release(w, sup, "PILOT-1")
        external_commit(tmp_path, w.origin, "feature/PILOT-1", "src/sneaky.ts", "x\n", "sneaky")
        merged = w.github.merge(pr)
        _record(w, "PILOT-1", rel, merged, pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.BLOCKED
    assert "unapproved changes merged" in w.last_comment("PILOT-1")


async def test_failing_smoke_check_blocks_release_verification(tmp_path: Path) -> None:
    w = make_world(tmp_path, extra={"release.smoke_commands": {"smoke": ["false"]}})
    async with Supervisor(w.deps) as sup:
        pr, rel = await _to_ready_release(w, sup, "PILOT-1")
        merged = w.github.merge(pr)
        _record(w, "PILOT-1", rel, merged, pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.BLOCKED
    assert "never rolls back" in w.last_comment("PILOT-1")


async def test_conflict_resolved_when_merging_is_accepted_and_flagged(tmp_path: Path) -> None:
    # main moved on and conflicts; the human resolves it in the PR (merging main into the
    # branch, as GitHub's Resolve conflicts does) and then merges.
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr, rel = await _to_ready_release(w, sup, "PILOT-1")
        external_commit(tmp_path, w.origin, "main", "src/pilot-1.ts", "export const main = 1;\n", "m")
        work = tmp_path / "resolve"
        sh("clone", str(w.origin), str(work), cwd=tmp_path)
        sh("checkout", "feature/PILOT-1", cwd=work)
        subprocess.run(["git", "merge", "origin/main"], cwd=work, capture_output=True)
        (work / "src" / "pilot-1.ts").write_text("export const pilot_1 = true;\nexport const main = 1;\n")
        sh("commit", "-am", "Resolve conflicts with main", cwd=work)
        sh("push", "origin", "HEAD:refs/heads/feature/PILOT-1", cwd=work)
        merged = w.github.merge(pr)
        _record(w, "PILOT-1", rel, merged, pr)
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    assert "conflict resolution" in w.last_comment("PILOT-1")
    assert "src/pilot-1.ts" in w.last_comment("PILOT-1")


async def test_merge_is_read_from_github_and_recorded_by_the_coordinator(tmp_path: Path) -> None:
    # Nobody types the release into Jira: the human merge is the release.
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr, _ = await _to_ready_release(w, sup, "PILOT-1")
        before = len(w.comments("PILOT-1"))
        assert await step(sup) == []
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE  # waits for the merge, quietly
        assert len(w.comments("PILOT-1")) == before
        merged = w.github.merge(pr, merged_by="release-owner", how="squash")
        assert await step(sup) == ["PILOT-1"]
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    assert merged in w.last_comment("PILOT-1") and "squash" in w.last_comment("PILOT-1")
    started = [c for c in w.comments("PILOT-1") if c.startswith("Release verification started")]
    assert f"PR #{pr} merged by release-owner (read from GitHub)" in started[-1]
    record = w.record("PILOT-1").release["record"]
    assert record["source"] == "github"
    assert (record["commit"], record["environment"], record["merged_pr"]) == (merged, "local-pilot", str(pr))
    assert not any("RECORD RELEASE" in c for c in w.comments("PILOT-1"))


async def test_record_release_chosen_by_hand_before_the_merge_waits_for_it(tmp_path: Path) -> None:
    # Record release without a RECORD RELEASE comment: the release is still read from GitHub.
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr, _ = await _to_ready_release(w, sup, "PILOT-1")
        w.jira.human_move("PILOT-1", Status.READY_RELEASE_VERIFICATION, APPROVER)
        assert await step(sup) == []
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE_VERIFICATION
        assert f"PR #{pr} is not merged yet" in w.last_comment("PILOT-1")
        merged = w.github.merge(pr)
        assert await step(sup) == ["PILOT-1"]
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
    assert w.record("PILOT-1").release["record"]["commit"] == merged


async def test_release_not_recordable_in_jira_is_retried_next_poll(tmp_path: Path) -> None:
    # For example a Jira condition that lets only approvers choose Record release.
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        pr, _ = await _to_ready_release(w, sup, "PILOT-1")
        w.github.merge(pr)
        w.jira.drop_routes.add((Status.READY_RELEASE, Status.READY_RELEASE_VERIFICATION))
        report = await sup.poll_once()
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE
        assert any("release not recorded" in s["reason"] for s in report.skipped)
        w.jira.drop_routes.clear()
        assert await step(sup) == ["PILOT-1"]
    assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
