"""A candidate under review is flagged when the base branch moves past it."""

from __future__ import annotations

from pathlib import Path

from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import external_commit
from harness import World, make_world, step

KEY = "PILOT-1"


async def _to_code_review(w: World, sup: Supervisor) -> None:
    w.new_ticket(KEY)
    w.submit(KEY)
    await step(sup)
    w.move(KEY, Status.READY_PLANNING)
    await step(sup)
    w.move(KEY, Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    assert w.jira.status_of(KEY) is Status.CODE_REVIEW, w.last_comment(KEY)


async def test_unrelated_changes_on_main_say_nothing(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        before = len(w.comments(KEY))
        external_commit(tmp_path, w.origin, "main", "docs/notes.md", "notes\n", "notes")
        await w.repo.fetch()  # tick() fetches once for every ticket it checks
        drift = await sup.staleness.check(KEY)
        assert drift is not None and drift.verified_base != drift.base and not drift.kinds
        await sup.staleness.tick(force=True)
        assert len(w.comments(KEY)) == before


async def test_changes_to_the_same_files_are_flagged_once(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        n = w.record(KEY).candidate_number
        # PILOT-8 merged and adds the same file this candidate adds: both change it, and it conflicts.
        external_commit(
            tmp_path, w.origin, "main", "src/pilot-1.ts", "export const other = 1;\n", "PILOT-8 filters"
        )
        await sup.staleness.tick(force=True)
        text = w.last_comment(KEY)
        assert f"Candidate c{n} may be out of date" in text
        assert "change files this candidate also changes: src/pilot-1.ts" in text
        assert "no longer merges cleanly with main: conflicts in src/pilot-1.ts" in text
        assert "Merged since, touching those files" in text
        assert "Submit follow-up changes" in text and "Nothing waits for this" in text
        # Said once; the ticket stays where it is.
        before = len(w.comments(KEY))
        await sup.staleness.tick(force=True)
        assert len(w.comments(KEY)) == before
        assert w.jira.status_of(KEY) is Status.CODE_REVIEW
