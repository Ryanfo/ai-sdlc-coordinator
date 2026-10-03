"""Every comment that waits for a human says which status the ticket moves into next."""

from __future__ import annotations

import pytest

from delivery import comments
from delivery.models import CheckResult, Finding, Question, Severity
from delivery.workflow import STAGES, STATUS_NAMES

CHECK = CheckResult(name="unit", source="coordinator", conclusion="passed", sha="a" * 40)


def first_lines(text: str) -> str:
    return "\n".join(text.splitlines()[:3])


def test_review_gates_lead_with_the_next_status() -> None:
    spec = comments.spec_gate("K-1-SPEC-v1", "u", 1, "summary", "approvers")
    assert "**Ready to move into Ready for planning** once" in first_lines(spec)
    assert "Request specification changes** (moves into **Ready for refinement**)" in spec

    plan = comments.plan_gate("K-1-PLAN-v1", "u", "f", 1, "summary", "approvers", [])
    assert "**Ready to move into Ready for development** once" in first_lines(plan)
    assert "Request plan changes** (moves into **Ready for planning**)" in plan

    code = comments.code_gate(
        "K-1-CODE-c1", "K-1-ACCEPT-c1", "pr", "sha", "r", "v", [CHECK], [], [], [], "rev"
    )
    assert "**Ready to move into Acceptance review** once" in first_lines(code)
    assert "**Accept delivery** moves it into **Ready for release preparation**" in first_lines(code)
    assert "Request code changes** (moves into **Changes requested**)" in code

    release = comments.release_gate("K-1-RELEASE-v1", "u", 1, "sha", "approvers", "prod")
    assert "**Ready to move into Ready for release** once" in first_lines(release)
    assert "**Record release** (moves into **Ready for release verification**)" in release
    assert "Request release changes** (moves into **Ready for release preparation**)" in release


def test_verification_failed_names_where_each_option_leads() -> None:
    finding = Finding(id="F1", severity=Severity.MAJOR, description="Wrong.")
    text = comments.verification_failed("K-1-CODE-c1", "pr", 1, "a" * 40, "r", "v", [CHECK], [finding], [])
    assert "**Ready to move into Ready for development** to fix it" in first_lines(text)
    assert "Submit implementation changes** (moves into **Ready for development**)" in text
    assert "Revise scope** (moves into **Ready for refinement**)" in text
    assert "Submit follow-up changes** (moves into **Ready for verification**)" in text


@pytest.mark.parametrize("stage", [s.value for s in STAGES])
def test_questions_blocked_and_waiting_name_the_ready_status(stage: str) -> None:
    ready = STATUS_NAMES[STAGES[next(s for s in STAGES if s.value == stage)].ready]
    q = comments.questions("K-1-X-R1", "u", [Question(id="Q1", question="Which?")], "you", stage)
    assert f"**Ready to move into {ready}** once" in first_lines(q)
    b = comments.blocked(stage, "reason", "do this", stage)
    assert f"**Ready to move into {ready}** once" in first_lines(b)
    assert f"(moves into **{ready}**)" in b
    w = comments.waiting(stage, "reason", "do this")
    assert f"**Stays in {ready}**" in first_lines(w)


def test_candidate_and_done_say_nothing_is_needed() -> None:
    cand = comments.candidate_ready(1, "sha", "pr", "summary")
    assert "**Moving into Ready for verification**" in first_lines(cand) and "Nothing to do" in cand
    assert "Nothing more to do" in comments.done("sha", "prod", "u", "merge commit")
