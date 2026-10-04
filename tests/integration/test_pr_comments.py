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
    w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)
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
        assert "G-items" in w.last_comment(KEY)  # the code review comment says how this works

        # A change request with no F-items of its own: the PR comments say it all.
        code = w.token(KEY, "CODE")
        w.decide(KEY, f"CHANGE CODE {code}\nSee the comments on the pull request.", Status.CHANGES_REQUESTED)
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        assert await step(sup) == [KEY]
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        items = _envelope(w, "implement-ticket")["feedback_items"]
        assert set(items) == {"G1", "G2"}
        assert items["G1"].startswith("GitHub review comment by @reviewer on src/pilot-1.ts:1: Name this")
        assert "@dev replied: Agreed, will rename." in items["G1"]
        assert "https://github.com/example/app/pull/" in items["G1"]
        assert items["G2"] == "GitHub review by @reviewer (requested changes): Needs an empty state."


async def test_jira_items_and_pr_comments_together_and_submit_changes_keeps_pr_comments(
    tmp_path: Path,
) -> None:
    w = make_world(tmp_path)
    w.scenario({"implement-ticket": TWO_RUNS})
    async with Supervisor(w.deps) as sup:
        pr = await _to_code_review(w, sup)
        w.github.comment_on_line(pr, "src/pilot-1.ts", 1, "Add a test for this", REVIEWER)
        code = w.token(KEY, "CODE")
        w.decide(
            KEY, f"CHANGE CODE {code}\nF1: use the shared helper\nF2: rename it", Status.CHANGES_REQUESTED
        )
        # Only F1 from Jira; the PR's open conversation stays in (resolve it to leave it out).
        w.jira.human_comment(KEY, DEV, f"SUBMIT CHANGES {code}\nF1: as asked")
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        await step(sup)
        items = _envelope(w, "implement-ticket")["feedback_items"]
        assert set(items) == {"F1", "G1"}
        assert "Add a test for this" in items["G1"]


async def test_a_change_request_with_nothing_to_change_still_waits(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        code = w.token(KEY, "CODE")
        w.decide(KEY, f"CHANGE CODE {code}\nPlease improve it.", Status.CHANGES_REQUESTED)
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        assert await step(sup) == []
        text = w.last_comment(KEY)
        assert "no feedback items selected" in text and "review comments on the pull request" in text
