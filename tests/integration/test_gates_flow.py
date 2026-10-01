"""Human gate integrity, verification loops and approval invalidation, end to end."""

from __future__ import annotations

from pathlib import Path

from conftest import APPROVER, DEV
from delivery.models import GateState
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import external_commit
from harness import REVIEWER, World, make_world, step

BUGGY = "export const search = (q: string) => q; // bug\n"
FIXED = "export const search = (q: string) => q.toLowerCase();\n"
NO_BUG = ["sh", "-c", "! grep -rq bug src"]


async def _approve_to_development(w: World, sup: Supervisor, key: str) -> None:
    await step(sup)
    w.decide(key, f"APPROVE SPEC {w.token(key, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    w.decide(key, f"APPROVE PLAN {w.token(key, 'PLAN')}", Status.READY_DEVELOPMENT)


async def test_failing_candidate_cannot_progress_and_fixed_candidate_supersedes(tmp_path: Path) -> None:
    w = make_world(tmp_path, checks={"unit": NO_BUG})
    w.scenario({"implement-ticket": [{"edit": {"src/search.ts": BUGGY}}, {"edit": {"src/search.ts": FIXED}}]})
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await _approve_to_development(w, sup, "PILOT-1")
        await step(sup)  # development c1
        await step(sup)  # verification of c1 fails on the coordinator-run check
        assert w.jira.status_of("PILOT-1") is Status.CHANGES_REQUESTED
        assert "coordinator check unit (candidate) failed" in w.last_comment("PILOT-1")
        rec = w.record("PILOT-1")
        assert rec.pending_feedback and rec.candidate_number == 1
        # A human submits changes within scope; the selected feedback reaches the worker.
        w.jira.human_move("PILOT-1", Status.READY_DEVELOPMENT, DEV)
        await step(sup)  # development c2 (new commit on the same PR)
        rec2 = w.record("PILOT-1")
        assert rec2.candidate_number == 2 and rec2.candidate_sha != rec.candidate_sha
        assert rec2.pr_number == rec.pr_number
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW
        assert w.token("PILOT-1", "CODE") == "PILOT-1-CODE-c2"
    env = next(Path(w.cfg.runtime.state_dir).rglob("runs/PILOT-1/*development*/inputs/feedback.json"))
    assert "R1" in env.read_text() or "F" in env.read_text()


async def test_code_gate_requires_independent_github_review_on_current_head(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await _approve_to_development(w, sup, "PILOT-1")
        await step(sup)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW
        code, accept = w.token("PILOT-1", "CODE"), w.token("PILOT-1", "ACCEPT")
        pr = w.record("PILOT-1").pr_number
        assert pr
        w.github.approve(pr, "dev-bot")  # the PR author approving their own PR does not count
        w.decide("PILOT-1", f"APPROVE CODE {code}", Status.ACCEPTANCE_REVIEW)
        w.decide("PILOT-1", f"ACCEPT DELIVERY {accept}", Status.READY_RELEASE_PREPARATION)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED
        assert "independent human GitHub approval" in w.last_comment("PILOT-1")
        # Fix: an independent reviewer approves; an approver resumes; the decision is re-validated.
        w.github.approve(pr, REVIEWER)
        w.jira.human_move("PILOT-1", Status.READY_RELEASE_PREPARATION, APPROVER)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.RELEASE_REVIEW, w.last_comment("PILOT-1")


async def test_new_commit_after_code_approval_invalidates_candidate(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await _approve_to_development(w, sup, "PILOT-1")
        await step(sup)
        await step(sup)
        rec = w.record("PILOT-1")
        assert rec.pr_number
        w.github.approve(rec.pr_number, REVIEWER)
        w.decide("PILOT-1", f"APPROVE CODE {w.token('PILOT-1', 'CODE')}", Status.ACCEPTANCE_REVIEW)
        # Someone pushes a "small" change after approval.
        external_commit(tmp_path, w.origin, "feature/PILOT-1", "src/late.ts", "x\n", "late")
        w.decide(
            "PILOT-1", f"ACCEPT DELIVERY {w.token('PILOT-1', 'ACCEPT')}", Status.READY_RELEASE_PREPARATION
        )
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED
        assert "not the verified candidate" in w.last_comment("PILOT-1")


async def test_unauthorised_approval_blocks_then_approver_resume_redecides(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await step(sup)
        token = w.token("PILOT-1", "SPEC")
        w.decide("PILOT-1", f"APPROVE SPEC {token}", Status.READY_PLANNING, author=DEV)  # not an approver
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED
        assert "not an authorised approver" in w.last_comment("PILOT-1")
        assert w.record("PILOT-1").pause.resume_stage.value == "planning"  # type: ignore[union-attr]
        w.jira.human_comment("PILOT-1", APPROVER, f"APPROVE SPEC {token}")
        w.jira.human_move("PILOT-1", Status.READY_PLANNING, APPROVER)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.PLAN_REVIEW
        spec = [g for g in w.record("PILOT-1").gates if g.token == token][0]
        assert (
            spec.state is GateState.APPROVED and spec.evidence and spec.evidence.transition_author == APPROVER
        )


async def test_wrong_resume_action_is_rejected_without_field_enforcement(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario(
        {
            "refine-ticket": [
                {"outcome": "needs_clarification", "questions": [{"id": "Q1", "question": "Which fields?"}]},
                {},
            ]
        }
    )
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.NEEDS_CLARIFICATION
        w.jira.issues["PILOT-1"].fields.clear()  # a site without the field condition
        w.jira.human_comment("PILOT-1", DEV, "ANSWERS PILOT-1-REFINE-R1\nQ1: title")
        w.jira.human_move("PILOT-1", Status.READY_PLANNING, DEV)  # wrong: "Submit planning answers"
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED
        assert "wrong resume action" in w.last_comment("PILOT-1")
        assert w.record("PILOT-1").pause.resume_stage.value == "refinement"  # type: ignore[union-attr]
        w.jira.human_move("PILOT-1", Status.READY_REFINEMENT, DEV)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW


async def test_edited_approval_requires_fresh_decision(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await step(sup)
        token = w.token("PILOT-1", "SPEC")
        c = w.jira.human_comment("PILOT-1", APPROVER, f"APPROVE SPEC {token}")
        w.jira.edit_comment("PILOT-1", c.id, f"APPROVE SPEC {token}")
        w.jira.human_move("PILOT-1", Status.READY_PLANNING, APPROVER)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED
        assert "edited" in w.last_comment("PILOT-1")


async def test_protected_paths_cannot_change_in_a_feature_ticket(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"implement-ticket": [{"edit": {".github/workflows/ci.yml": "on: push\n"}}]})
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await _approve_to_development(w, sup, "PILOT-1")
        await step(sup)
    assert w.jira.status_of("PILOT-1") is Status.BLOCKED
    assert ".github/workflows/ci.yml" in w.last_comment("PILOT-1")
    assert not w.github.prs


async def test_fabricated_contract_or_missing_plugin_never_succeeds(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario(
        {
            "PILOT-1:refine-ticket": [{"contract_id": "delivery.refine-ticket/v9"}],
            "PILOT-2:refine-ticket": [{"no_plugin": True}],
            "PILOT-3:refine-ticket": [{"artifact_path": "../../etc/passwd"}],
        }
    )
    for k in ("PILOT-1", "PILOT-2", "PILOT-3"):
        w.new_ticket(k)
        w.submit(k)
    async with Supervisor(w.deps) as sup:
        await step(sup)
    assert "procedure did not load" in w.last_comment("PILOT-1")
    assert "plugin did not load" in w.last_comment("PILOT-2")
    assert "escapes" in w.last_comment("PILOT-3") or "relative" in w.last_comment("PILOT-3")
    for k in ("PILOT-1", "PILOT-2", "PILOT-3"):
        assert w.jira.status_of(k) is Status.BLOCKED
