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
        # Refinement -> questions in Jira, paused with the round token.
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.NEEDS_CLARIFICATION
        assert "ANSWERS PILOT-1-REFINE-R1" in w.last_comment("PILOT-1")
        assert w.jira.issues["PILOT-1"].fields["customfield_10050"] == {"value": "refinement"}

        # A comment alone does not restart work.
        w.jira.human_comment("PILOT-1", DEV, "ANSWERS PILOT-1-REFINE-R1\nQ1: Title only.")
        assert await step(sup) == []
        w.jira.human_move("PILOT-1", Status.READY_REFINEMENT, DEV)
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW
        spec_v2 = w.token("PILOT-1", "SPEC")
        assert spec_v2 == "PILOT-1-SPEC-v2"

        # Change request bound to v2 produces v3; then approve v3.
        w.decide("PILOT-1", f"CHANGE SPEC {spec_v2}\nF1: Include synopsis.", Status.READY_REFINEMENT)
        assert await step(sup) == ["PILOT-1"]
        spec_v3 = w.token("PILOT-1", "SPEC")
        assert spec_v3 == "PILOT-1-SPEC-v3"
        w.decide("PILOT-1", f"APPROVE SPEC {spec_v3}", Status.READY_PLANNING)

        # Planning -> plan review -> approve.
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.PLAN_REVIEW
        w.decide("PILOT-1", f"APPROVE PLAN {w.token('PILOT-1', 'PLAN')}", Status.READY_DEVELOPMENT)

        # Development -> PR -> ready for verification; verification runs on the next poll.
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.READY_VERIFICATION
        rec = w.record("PILOT-1")
        assert rec.pr_number and rec.candidate_sha
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW
        code, accept = w.token("PILOT-1", "CODE"), w.token("PILOT-1", "ACCEPT")

        # Human code gate: independent GitHub review + Jira decision; then acceptance.
        w.github.approve(rec.pr_number, REVIEWER)
        w.decide("PILOT-1", f"APPROVE CODE {code}", Status.ACCEPTANCE_REVIEW)
        w.decide("PILOT-1", f"ACCEPT DELIVERY {accept}", Status.READY_RELEASE_PREPARATION)
        assert await step(sup) == ["PILOT-1"]
        assert w.jira.status_of("PILOT-1") is Status.RELEASE_REVIEW

        # Release approval, human merge and release record, then verification -> Done.
        rel = w.token("PILOT-1", "RELEASE")
        w.decide("PILOT-1", f"APPROVE RELEASE {rel}", Status.READY_RELEASE)
        merged = w.github.merge(rec.pr_number)
        w.decide(
            "PILOT-1",
            f"RECORD RELEASE {rel}\ncommit: {merged}\nenvironment: local-pilot\nmerged-pr: {rec.pr_number}",
            Status.READY_RELEASE_VERIFICATION,
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
