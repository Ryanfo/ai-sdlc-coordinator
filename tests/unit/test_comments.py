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
    spec = comments.spec_gate("K-1-SPEC-v1", "u", 1)
    assert "**To approve**: choose **Approve specification**" in first_lines(spec)
    assert "```" not in spec and "K-1-SPEC-v1" not in spec and "No comment needed" not in spec
    assert "Approve specification** (moves into **Ready for planning**)" in first_lines(spec)
    assert "Request specification changes** (moves into **Ready for refinement**)" in spec

    plan = comments.plan_gate("u", "f", 1, [])
    assert "**To approve**: choose **Approve plan**" in first_lines(plan)
    assert "Approve plan** (moves into **Ready for development**)" in first_lines(plan)
    assert "Request plan changes** (moves into **Ready for planning**)" in plan

    release = comments.release_gate("u", 1, "sha")
    assert "**To approve**: choose **Approve release**" in first_lines(release)
    assert "Then merge the PR" in first_lines(release)
    assert "RECORD RELEASE" not in release  # read from GitHub, never typed in
    assert "Request release changes** (moves into **Ready for release preparation**)" in release
    assert "never merges" not in release and "No comment needed" not in release


def test_gates_carry_no_names_or_summaries() -> None:
    for text in (
        comments.spec_gate("t", "u", 1),
        comments.plan_gate("u", "f", 1, []),
        comments.code_gate(1, "pr", "sha", "r", "v", [CHECK], [], [], []),
        comments.release_gate("u", 1, "sha"),
    ):
        assert "approvers" not in text and "authorised" not in text and "anyone" not in text


def test_code_gate_is_only_the_code_decision() -> None:
    code = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK], [], [], [])
    assert "**To approve**" in first_lines(code)
    assert "Approve code** (moves into **Acceptance review**)" in first_lines(code)
    assert "Request code changes** (moves into **Changes requested**)" in code
    assert "pr" in code and "[Verification](v)" in code
    # Acceptance is a separate step with its own comment: no acceptance template here.
    assert "ACCEPT DELIVERY" not in code and "Accept delivery" not in code and "```" not in code


def test_acceptance_comment_links_the_guide_instead_of_quoting_it() -> None:
    kw = dict(worker_id="w", local_app=True, try_command=True)
    text = comments.acceptance_ready("K-1", 1, "pr", guide_url="https://x/guide", **kw)
    assert "**To accept**" in first_lines(text)
    assert "Accept delivery** (moves into **Ready for release preparation**)" in first_lines(text)
    assert "```" not in text and "[Acceptance guide](https://x/guide)" in text
    assert text.index("Request acceptance changes") < text.index("delivery try K-1")
    assert "AC1" not in text


def test_only_failed_checks_reach_the_ticket() -> None:
    bad = CheckResult(name="lint", source="ci", conclusion="failed", sha="b" * 40, target="candidate")
    code = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK, bad], [], [], [])
    assert "**Failed checks**" in code and "lint (ci/candidate): failed" in code
    assert "unit" not in code and "| Check |" not in code
    clean = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK], [], [], [])
    assert "Failed checks" not in clean and "passed" not in clean


def test_verification_failed_leads_with_the_fix_and_names_where_each_option_leads() -> None:
    finding = Finding(id="F1", severity=Severity.MAJOR, description="Wrong.")
    bad = CheckResult(name="lint", source="ci", conclusion="failed", sha="b" * 40, target="candidate")
    text = comments.verification_failed("pr", 1, "a" * 40, "r", "v", [CHECK, bad], [finding], [])
    assert "**To fix it**" in first_lines(text)
    assert "Submit implementation changes** (moves into **Ready for development**)" in first_lines(text)
    assert "**Revise scope** (moves into **Ready for refinement**)" in text
    assert "lint (ci/candidate): failed" in text and "unit" not in text and "SUBMIT CHANGES" not in text


@pytest.mark.parametrize("stage", [s.value for s in LIFECYCLE_STAGES])
def test_questions_blocked_and_waiting_name_the_ready_status(stage: str) -> None:
    ready = STATUS_NAMES[STAGES[next(s for s in STAGES if s.value == stage)].ready]
    q = comments.questions("u", [Question(id="Q1", question="Which?")], stage)
    assert "**To answer**: reply in a comment, then choose" in first_lines(q)
    assert "ANSWERS" not in q and "```" not in q
    assert f"(moves into **{ready}**)" in first_lines(q)
    b = comments.blocked(stage, "reason", "do this", stage)
    assert "**Next action**: do this" in first_lines(b) and f"(moves into **{ready}**)" in b
    w = comments.waiting(stage, "reason", "do this")
    assert "**Next action**: do this" in first_lines(w) and f"stays in {ready}" in w


def test_candidate_and_done_stay_minimal() -> None:
    cand = comments.candidate_ready(1, "sha", "pr")
    assert "Verification starts next" in first_lines(cand)
    held = comments.candidate_ready(1, "sha", "pr", session_open=True)
    assert "once the developer closes the Claude session" in first_lines(held)
    done = comments.done("sha", "prod", "u")
    assert "Released commit `sha` in `prod`" in done


def test_comments_stay_short() -> None:
    """Jira is for decisions: a gate comment stays within a handful of lines."""
    code = comments.code_gate(1, "pr", "sha", "r", "v", [CHECK], [], [], [])
    spec = comments.spec_gate("K-1-SPEC-v1", "u", 1)
    failed = comments.verification_failed("pr", 1, "a" * 40, "r", "v", [CHECK], [], [])
    for text in (code, spec, failed):
        assert len(text.splitlines()) <= 8, text
