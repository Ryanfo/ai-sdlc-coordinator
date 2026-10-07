"""Review comments left on the pull request reach development as G-items."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from conftest import DEV
from delivery.ports import Review
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import REVIEWER, World, make_world, step

KEY = "PILOT-1"
TWO_RUNS = [{}, {"edit": {"src/search.ts": "export const search = 2;\n"}}]


def _envelope(w: World, procedure: str) -> dict[str, Any]:
    inv = [i for i in w.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"])][-1]
    prompt = inv["argv"][inv["argv"].index("-p") + 1]
    return dict(json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text()))


async def _to_code_review(w: World, sup: Supervisor) -> int:
    w.new_ticket(KEY)
    w.submit(KEY)
    await step(sup)
    w.move(KEY, Status.READY_PLANNING)
    await step(sup)
    w.move(KEY, Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    assert w.jira.status_of(KEY) is Status.CODE_REVIEW, w.last_comment(KEY)
    pr = w.record(KEY).pr_number
    assert pr
    return pr


async def test_pr_review_comments_become_change_items(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"implement-ticket": TWO_RUNS})
    async with Supervisor(w.deps) as sup:
        pr = await _to_code_review(w, sup)
        # Older than the candidate and nobody added to it since: already dealt with.
        w.github.comment_on_line(pr, "src/old.ts", 3, "old remark", at=datetime(2020, 1, 1, tzinfo=UTC))
        w.github.comment_on_line(
            pr,
            "src/pilot-1.ts",
            1,
            "Name this export after what it does.",
            REVIEWER,
            replies=(("dev", "Agreed, will rename."),),
        )
        done = w.github.comment_on_line(pr, "src/pilot-1.ts", 1, "Typo in the comment", REVIEWER)
        w.github.resolve(pr, done.id)
        w.github.reviews_by_pr.setdefault(pr, []).append(
            Review(901, REVIEWER, "CHANGES_REQUESTED", "x", datetime.now(UTC), body="Needs an empty state.")
        )
        w.github.reviews_by_pr[pr].append(
            Review(902, "ci-bot", "COMMENTED", "x", datetime.now(UTC), "Bot", body="Coverage 91%")
        )
        assert "Unresolved PR review conversations are included" in w.last_comment(KEY)

        # A change request with nothing written in Jira: the PR comments say it all.
        w.move(KEY, Status.CHANGES_REQUESTED)
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        assert await step(sup) == [KEY]
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        items = _envelope(w, "implement-ticket")["feedback_items"]
        assert set(items) == {"G1", "G2"}
        assert items["G1"].startswith("GitHub review comment by @reviewer on src/pilot-1.ts:1: Name this")
        assert "@dev replied: Agreed, will rename." in items["G1"]
        assert "https://github.com/example/app/pull/" in items["G1"]
        assert items["G2"] == "GitHub review by @reviewer (requested changes): Needs an empty state."


async def test_jira_comments_and_pr_comments_together(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"implement-ticket": TWO_RUNS})
    async with Supervisor(w.deps) as sup:
        pr = await _to_code_review(w, sup)
        w.github.comment_on_line(pr, "src/pilot-1.ts", 1, "Add a test for this", REVIEWER)
        w.decide(KEY, Status.CHANGES_REQUESTED, "F1: use the shared helper\nF2: rename it")
        # Narrowing the work is said in words; Claude follows it (the PR conversation stays in
        # until it is resolved on GitHub).
        w.jira.human_comment(KEY, DEV, "Only F1 please, keep the name.")
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        await step(sup)
        env = _envelope(w, "implement-ticket")
        assert env["feedback_items"] == {
            "F1": "use the shared helper",
            "F2": "rename it",
            "F3": "Only F1 please, keep the name.",
            "G1": env["feedback_items"]["G1"],
        }
        assert "Add a test for this" in env["feedback_items"]["G1"] and env["changes_requested"]


async def test_a_change_request_with_nothing_written_starts_and_claude_asks(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        w.move(KEY, Status.CHANGES_REQUESTED)
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        assert await step(sup) == [KEY]
        env = _envelope(w, "implement-ticket")
        assert env["changes_requested"] and env["feedback_items"] == {}
