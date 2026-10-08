"""Human gate integrity, verification loops and approval invalidation, end to end."""

from __future__ import annotations

from pathlib import Path

from conftest import APPROVER, DEV
from delivery.models import GateState
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import REVIEWER, World, make_world, step

BUGGY = "export const search = (q: string) => q; // bug\n"
FIXED = "export const search = (q: string) => q.toLowerCase();\n"
NO_BUG = ["sh", "-c", "! grep -rq bug src"]


async def _approve_to_development(w: World, sup: Supervisor, key: str) -> None:
    await step(sup)
    w.move(key, Status.READY_PLANNING)
    await step(sup)
    w.move(key, Status.READY_DEVELOPMENT)


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


async def test_code_gate_requires_independent_github_review(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await _approve_to_development(w, sup, "PILOT-1")
        await step(sup)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW
        pr = w.record("PILOT-1").pr_number
        assert pr
        w.github.approve(pr, "dev-bot")  # the PR author approving their own PR does not count
        w.move("PILOT-1", Status.ACCEPTANCE_REVIEW)
        w.move("PILOT-1", Status.READY_RELEASE)
        w.github.merge(pr)
        report = await sup.poll_once()
        # Not Done: nothing blocks it, but the approval does not stand until someone independent reviews.
        assert w.jira.status_of("PILOT-1") is Status.READY_RELEASE
        assert "independent human GitHub approval" in report.waiting[0]["reason"]
        w.github.approve(pr, REVIEWER)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.DONE, w.last_comment("PILOT-1")


async def test_unauthorised_approval_blocks_then_approver_resume_redecides(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await step(sup)
        token = w.token("PILOT-1", "SPEC")
        w.move("PILOT-1", Status.READY_PLANNING, author=DEV)  # not an approver
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED
        assert "not an authorised approver" in w.last_comment("PILOT-1")
        assert w.record("PILOT-1").pause.resume_stage.value == "planning"  # type: ignore[union-attr]
        # An approver's Resume is the decision: no comment.
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
        w.jira.human_comment("PILOT-1", DEV, "title")
        w.jira.human_move("PILOT-1", Status.READY_PLANNING, DEV)  # wrong: "Submit planning answers"
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED
        assert "wrong resume action" in w.last_comment("PILOT-1")
        assert w.record("PILOT-1").pause.resume_stage.value == "refinement"  # type: ignore[union-attr]
        w.jira.human_move("PILOT-1", Status.READY_REFINEMENT, DEV)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW


async def test_the_move_alone_approves_and_review_comments_go_with_it(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.submit("PILOT-1")
    async with Supervisor(w.deps) as sup:
        await step(sup)
        token = w.token("PILOT-1", "SPEC")
        # A comment alone decides nothing.
        w.jira.human_comment("PILOT-1", APPROVER, "Looks good, but call the button Download.")
        assert await step(sup) == []
        assert w.jira.status_of("PILOT-1") is Status.SPECIFICATION_REVIEW
        w.jira.human_move("PILOT-1", Status.READY_PLANNING, APPROVER)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.PLAN_REVIEW
        spec = next(g for g in w.record("PILOT-1").gates if g.token == token)
        assert (
            spec.state is GateState.APPROVED and spec.evidence and spec.evidence.transition_author == APPROVER
        )
        notes = [c["body"] for c in w.envelope("plan-ticket")["selected_comments"]]
        assert notes == ["Looks good, but call the button Download."]
        # Only coordinator comments on the ticket: nobody had to write a decision.
        humans = [c for c in w.jira.comments_by_key["PILOT-1"] if "delivery-op:" not in c.body_text]
        assert [c.body_text for c in humans] == ["Looks good, but call the button Download."]


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
