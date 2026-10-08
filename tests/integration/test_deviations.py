"""Deviations from the approved specification are questions for a human, never failures.

Approving the code accepts them as built (the Done comment names them; the specification is not
rewritten); one named in a change request goes back to development, which changes the code back
to the specification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import DEV
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import REVIEWER, World, make_world, step

KEY = "PILOT-1"
GREEN = {
    "id": "D1",
    "description": "The search button is green; the specification says it is blue.",
    "criterion_id": "AC1",
    "requested": True,
    "request": "make the search button green",
}
EXPORT = {
    "id": "D2",
    "description": "An Export button downloads the results as CSV; the specification does not ask for it.",
    "requested": False,
}
DEVIATES = [{"criterion_id": "AC1", "description": "green, see D1", "status": "deviates"}]
# The second development run (the requested changes) edits something new.
TWO_RUNS = [{}, {"edit": {"src/search.ts": "export const search = 2;\n"}}]


def _calls(w: World, procedure: str) -> list[dict[str, Any]]:
    return [i for i in w.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"])]


def _envelope(inv: dict[str, Any]) -> dict[str, Any]:
    prompt = inv["argv"][inv["argv"].index("-p") + 1]
    return json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text())


async def _to_code_review(w: World, sup: Supervisor) -> None:
    w.new_ticket(KEY)
    w.submit(KEY)
    await step(sup)
    w.move(KEY, Status.READY_PLANNING)
    await step(sup)
    w.move(KEY, Status.READY_DEVELOPMENT)
    await step(sup)  # development c1
    await step(sup)  # verification of c1


async def test_deviation_is_flagged_and_approving_the_code_accepts_it(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"review-ticket": [{"deviations": [GREEN], "evidence": DEVIATES}, {}]})
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        # Not a failure: the candidate is in code review and the deviation is a question.
        assert w.jira.status_of(KEY) is Status.CODE_REVIEW
        gate = w.last_comment(KEY)
        assert "Deviations from the specification" in gate
        assert "D1 (asked for by the developer; changes AC1)" in gate
        assert "Approving the code accepts them" in gate and "ACCEPT DEVIATIONS" not in gate
        rec = w.record(KEY)
        assert [(d.id, d.state) for d in rec.deviations] == [("D1", "open")]
        assert rec.deviations[0].announced_at is not None
        names = await w.repo.ls_tree(f"origin/delivery/{KEY}", f"docs/delivery/{KEY}/reviews/")
        assert any(n.endswith("/deviations.json") for n in names)
        # The reviewer saw the candidate's commit messages (where session requests are recorded).
        review_env = _envelope(_calls(w, "review-ticket")[0])
        assert (
            Path(review_env["output"]["artifact_dir"]).parents[1] / "inputs" / "candidate-commits.txt"
        ).exists()

        # Approving the code and accepting the delivery accept it as built; the merge is the release
        # and the Done comment names the deviation. Nothing is rewritten.
        plan_token = w.token(KEY, "PLAN")
        w.github.approve(rec.pr_number or 0, REVIEWER)
        w.move(KEY, Status.ACCEPTANCE_REVIEW)
        w.move(KEY, Status.READY_RELEASE)
        w.github.merge(rec.pr_number or 0)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.DONE, w.last_comment(KEY)
        assert "Deviations accepted with the code: D1." in w.last_comment(KEY)
        assert len(_calls(w, "review-ticket")) == 1 and len(_calls(w, "verify-ticket")) == 1
        rec = w.record(KEY)
        assert w.token(KEY, "SPEC") == f"{KEY}-SPEC-v1" and w.token(KEY, "PLAN") == plan_token
        assert [(d.id, d.state) for d in rec.deviations] == [("D1", "open")]


async def test_rejected_deviation_goes_back_to_development(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario(
        {
            "review-ticket": [{"deviations": [GREEN, EXPORT], "evidence": DEVIATES}, {}],
            "implement-ticket": TWO_RUNS,
        }
    )
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        assert w.jira.status_of(KEY) is Status.CODE_REVIEW
        w.decide(KEY, Status.CHANGES_REQUESTED, "D2: drop the Export button")
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        await step(sup)  # development c2 changes the code back
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        env = _envelope(_calls(w, "implement-ticket")[-1])
        assert set(env["feedback_items"]) == {"D2"}  # D1 was not named, so it is left alone
        assert "drop the Export button" in env["feedback_items"]["D2"]
        assert "follows the approved specification" in env["feedback_items"]["D2"]
        assert w.record(KEY).candidate_number == 2


async def test_deviations_named_with_other_changes_go_back_and_the_rest_wait(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    major = [{"id": "F1", "severity": "major", "description": "Search ignores accents."}]
    w.scenario(
        {
            "review-ticket": [{"deviations": [GREEN, EXPORT], "findings": major, "evidence": DEVIATES}, {}],
            "implement-ticket": TWO_RUNS,
        }
    )
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        # The finding fails verification; the deviations are listed as questions alongside it.
        assert w.jira.status_of(KEY) is Status.CHANGES_REQUESTED
        failed = w.last_comment(KEY)
        assert "Deviations from the specification" in failed
        why = failed.split("Why")[1].split("Deviations from")[0]
        assert "F1" in why and "D1" not in why and "D2" not in why
        w.jira.human_comment(KEY, DEV, "D2: no Export button")
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
        implement = _envelope(_calls(w, "implement-ticket")[-1])
        assert set(implement["feedback_items"]) == {"F1", "D2"}  # D1 is not named: left as built
        # Nothing is accepted before the code is approved: the specification is unchanged.
        spec_input = next(a for a in implement["approved_artefacts"] if a["kind"] == "specification")
        assert spec_input["revision"] == 1


async def test_with_no_approver_list_anyone_can_decide(tmp_path: Path) -> None:
    """`approvals.jira_account_ids = []`: nobody's decision is refused for who they are."""
    w = make_world(tmp_path, extra={"approvals": {"jira_account_ids": []}})
    w.scenario({"review-ticket": [{"deviations": [EXPORT]}, {}]})
    anyone = "colleague-0042"
    async with Supervisor(w.deps) as sup:
        w.new_ticket(KEY)
        w.submit(KEY)
        await step(sup)
        assert "To approve: choose Approve specification" in w.last_comment(KEY)
        w.move(KEY, Status.READY_PLANNING, author=anyone)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.PLAN_REVIEW, w.last_comment(KEY)
        w.move(KEY, Status.READY_DEVELOPMENT, author=anyone)
        await step(sup)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.CODE_REVIEW, w.last_comment(KEY)
        assert "Approving the code accepts them" in w.last_comment(KEY)
        w.github.approve(w.record(KEY).pr_number or 0, REVIEWER)
        w.move(KEY, Status.ACCEPTANCE_REVIEW, author=anyone)
        w.move(KEY, Status.READY_RELEASE, author=anyone)
        w.github.merge(w.record(KEY).pr_number or 0)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.DONE, w.last_comment(KEY)
