"""Full ticket lifecycle against real Git, fake Jira/GitHub and the fake claude CLI."""

from __future__ import annotations

from pathlib import Path

from conftest import APPROVER, DEV
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import REVIEWER, World, make_world, step

QUESTION = [{"id": "Q1", "question": "Should search include the description?", "rationale": "scope"}]


async def test_full_lifecycle_with_clarification_change_request_and_release(tmp_path: Path) -> None:
    w: World = make_world(tmp_path)
    w.scenario(
        {
            "refine-ticket": [{"outcome": "needs_clarification", "questions": QUESTION}, {}, {}],
        }
    )
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        # Refinement -> questions in Jira: answer in your own words, then Submit answers.
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.NEEDS_CLARIFICATION
        assert "Submit refinement answers" in w.last_comment("PILOT-1")
        assert "ANSWERS" not in w.last_comment("PILOT-1")
        assert w.jira.issues["PILOT-1"].fields["customfield_10050"] == {"value": "refinement"}

        # A comment alone does not restart work.
        w.jira.human_comment("PILOT-1", DEV, "Title only.")
        assert await step(sup) == []
        w.jira.human_move("PILOT-1", Status.READY_REFINEMENT, DEV)
        assert await step(sup) == ["PILOT-1"]
        assert w.envelope("refine-ticket")["feedback_items"] == {"A1": "Title only."}
        assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW
        spec_v2 = w.token("PILOT-1", "SPEC")
        assert spec_v2 == "PILOT-1-SPEC-v2"

        # A change request (the move, with a comment saying what) produces v3; moving it on
        # approves v3: no decision comments anywhere.
        w.decide("PILOT-1", Status.READY_REFINEMENT, "F1: Include synopsis.")
        assert await step(sup) == ["PILOT-1"]
        spec_v3 = w.token("PILOT-1", "SPEC")
        assert spec_v3 == "PILOT-1-SPEC-v3"
        w.move("PILOT-1", Status.READY_PLANNING)

        # Planning -> plan review -> approve.
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.PLAN_REVIEW
        w.move("PILOT-1", Status.READY_DEVELOPMENT)

        # Development -> PR -> ready for verification; verification runs on the next poll.
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.READY_VERIFICATION
        rec = w.record("PILOT-1")
        assert rec.pr_number and rec.candidate_sha
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW

        # Human code gate: independent GitHub review + the Jira move; then acceptance.
        w.github.approve(rec.pr_number, REVIEWER)
        w.move("PILOT-1", Status.ACCEPTANCE_REVIEW)
        w.move("PILOT-1", Status.READY_RELEASE_PREPARATION)
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.RELEASE_REVIEW

        # Release approval, human merge and release record, then verification -> Done.
        rel = w.token("PILOT-1", "RELEASE")
        w.move("PILOT-1", Status.READY_RELEASE)
        merged = w.github.merge(rec.pr_number)
        w.decide(
            "PILOT-1",
            Status.READY_RELEASE_VERIFICATION,
            f"RECORD RELEASE {rel}\ncommit: {merged}\nenvironment: local-pilot\nmerged-pr: {rec.pr_number}",
        )
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")
        assert w.jira.issues["PILOT-1"].resolution == "Done"
        assert "Release verified" in w.last_comment("PILOT-1")

    # Artefacts are versioned and append-only on the delivery branch.
    names = await w.repo.ls_tree("origin/delivery/PILOT-1", "docs/delivery/PILOT-1/")
    for expected in (
        "specification/v001.md",
        "specification/v002.md",
        "specification/v003.md",
        "plan/v001.md",
        "plan/v001.footprint.json",
        "releases/v001.md",
    ):
        assert f"docs/delivery/PILOT-1/{expected}" in names
    assert any("/reviews/" in n and n.endswith("review.md") for n in names)
    # The coordinator never merged anything itself and never used bypass flags.
    for inv in w.invocations():
        assert "--dangerously-skip-permissions" not in inv["argv"]
        assert "--restricted" in inv["argv"]
        assert not any(k.startswith(("ANTHROPIC_", "GH_", "GITHUB_", "JIRA_")) for k in inv["env_keys"])
    assert APPROVER not in {
        c.author_account_id for c in w.jira.comments_by_key["PILOT-1"] if "delivery-op:" in c.body_text
    }
