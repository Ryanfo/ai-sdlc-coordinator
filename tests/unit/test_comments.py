"""Comments are short: what to do first, the status it moves into, only failures reported."""

from __future__ import annotations

import pytest

from delivery import comments
from delivery.models import CheckResult, Finding, Question, Severity
from delivery.workflow import LIFECYCLE_STAGES, STAGES, STATUS_NAMES

CHECK = CheckResult(name="unit", source="coordinator", conclusion="passed", sha="a" * 40)


def first_lines(text: str, n: int = 3) -> str:
    return "\n".join(text.splitlines()[:n])


def test_review_gates_lead_with_the_move() -> None:
    spec = comments.spec_gate("K-1-SPEC-v1", "u", 1, "summary", "approvers")
    assert "**To approve** (approvers): choose **Approve specification**" in first_lines(spec)
    assert "No comment needed" in first_lines(spec) and "```" not in spec and "K-1-SPEC-v1" not in spec
    assert "Approve specification** (moves into **Ready for planning**)" in first_lines(spec)
    assert "Request specification changes** (moves into **Ready for refinement**)" in spec

    plan = comments.plan_gate("u", "f", 1, "summary", "approvers", [])
    assert "**To approve** (approvers): choose **Approve plan**" in first_lines(plan)
    assert "Say what to change in a comment if you like" in plan and "Claude" in plan
    assert "Approve plan** (moves into **Ready for development**)" in first_lines(plan)
    assert "Request plan changes** (moves into **Ready for planning**)" in plan

    release = comments.release_gate("u", 1, "sha", "approvers", "prod")
    assert "**To approve** (approvers): choose **Approve release**" in first_lines(release)
    assert "moves the ticket to **Done**" in first_lines(release)
    assert "RECORD RELEASE" not in release  # read from GitHub, never typed in
    assert "Request release changes** (moves into **Ready for release preparation**)" in release


def test_code_gate_is_only_the_code_decision() -> None:
    code = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK], [], [], [], "rev")
    assert "**To approve the code**" in first_lines(code)
    assert "Approve code** (moves into **Acceptance review**)" in first_lines(code)
    assert "Request code changes** (moves into **Changes requested**)" in code
    assert "pr" in code and "[Verification report](v)" in code
    # Acceptance is a separate step with its own comment: no acceptance template here.
    assert "ACCEPT DELIVERY" not in code and "Accept delivery" not in code
    assert "gets its own comment" in code and "No comment needed" in code and "```" not in code


def test_acceptance_comment_is_only_the_acceptance_decision() -> None:
    kw = dict(worker_id="w", local_app=True, try_command=True, guide="Check the button.", guide_url=None)
    text = comments.acceptance_ready("K-1", 1, "a" * 40, "pr", **kw)
    assert "**To accept**" in first_lines(text)
    assert "Accept delivery** (moves into **Ready for release**)" in first_lines(text)
    assert "moves the ticket to Done when it sees the merge" in first_lines(text)
    with_proposal = comments.acceptance_ready("K-1", 1, "a" * 40, "pr", proposal=True, **kw)
    assert "Accept delivery** (moves into **Ready for release preparation**)" in first_lines(with_proposal)
    assert "No comment needed" in first_lines(text) and "```" not in text
    assert text.index("Request acceptance changes") < text.index("**Try it**")


def test_only_failed_checks_reach_the_ticket() -> None:
    bad = CheckResult(name="lint", source="ci", conclusion="failed", sha="b" * 40, target="candidate")
    code = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK, bad], [], [], [], "rev")
    assert "**Failed checks**" in code and "lint (ci/candidate): failed" in code
    assert "unit" not in code and "| Check |" not in code
    clean = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK], [], [], [], "rev")
    assert "Failed checks" not in clean and "passed" not in clean


def test_verification_failed_leads_with_the_fix_and_names_where_each_option_leads() -> None:
    finding = Finding(id="F1", severity=Severity.MAJOR, description="Wrong.")
    bad = CheckResult(name="lint", source="ci", conclusion="failed", sha="b" * 40, target="candidate")
    text = comments.verification_failed("pr", 1, "a" * 40, "r", "v", [CHECK, bad], [finding], [])
    assert "**To fix it**" in first_lines(text)
    assert "Submit implementation changes** (moves into **Ready for development**)" in first_lines(text)
    assert "Revise scope** to change what is built (moves into **Ready for refinement**)" in text
    assert "verify the same candidate again (moves into **Ready for verification**)" in text
    assert "lint (ci/candidate): failed" in text and "unit" not in text
    assert "say which in a comment first" in text and "SUBMIT CHANGES" not in text


@pytest.mark.parametrize("stage", [s.value for s in LIFECYCLE_STAGES])
def test_questions_blocked_and_waiting_name_the_ready_status(stage: str) -> None:
    ready = STATUS_NAMES[STAGES[next(s for s in STAGES if s.value == stage)].ready]
    q = comments.questions("u", [Question(id="Q1", question="Which?")], "you", stage)
    assert "**To answer** (you): reply in a comment, in your own words" in first_lines(q)
    assert "ANSWERS" not in q and "```" not in q
    assert "**To answer** (you)" in first_lines(q) and f"(moves into **{ready}**)" in first_lines(q)
    b = comments.blocked(stage, "reason", "do this", stage)
    assert "**Next action**: do this" in first_lines(b) and f"(moves into **{ready}**)" in b
    w = comments.waiting(stage, "reason", "do this")
    assert "**Next action**: do this" in first_lines(w) and f"stays in {ready}" in w


def test_candidate_and_done_say_nothing_is_needed() -> None:
    cand = comments.candidate_ready(1, "sha", "pr")
    assert "Nothing to do: moving into **Ready for verification**" in first_lines(cand)
    assert "start by themselves" in cand
    held = comments.candidate_ready(1, "sha", "pr", session_open=True)
    assert "start once the developer closes the Claude session" in first_lines(held)
    done = comments.done("sha", "prod", 7, "ana", "merge commit; contains the accepted candidate")
    assert "PR #7 was merged by ana as `sha` in `prod`" in done and "Provenance:" in done
    flagged = comments.done("sha", "prod", 7, None, "unapproved changes", flagged=True, deviations=["D1"])
    assert "**Look at this**" in flagged and "D1" in flagged


def test_comments_stay_short() -> None:
    """Jira is for decisions: a gate comment stays within a handful of lines."""
    code = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK], [], [], [], "rev")
    spec = comments.spec_gate("K-1-SPEC-v1", "u", 1, "summary", "approvers")
    failed = comments.verification_failed("pr", 1, "a" * 40, "r", "v", [CHECK], [], [])
    for text in (code, spec, failed):
        assert len(text.splitlines()) <= 25, text
