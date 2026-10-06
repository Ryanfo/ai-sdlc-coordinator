"""Deviation decisions: parsing, who may accept, and how they read in Jira."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from delivery import comments, deviations
from delivery.feedback import DecisionKind, collect_feedback, parse_decision
from delivery.models import Deviation, DeviationRecord, SharedExecutionRecord, StageResult
from delivery.ports import JiraComment
from delivery.workflow import Status

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
APPROVER = "approver-0001"
DEV = "dev-account-0001"
TOKEN = "PILOT-1-CODE-c2"


def _comment(cid: str, body: str, author: str = APPROVER, minutes: int = 5) -> JiraComment:
    t = T0 + timedelta(minutes=minutes)
    return JiraComment(cid, author, t, t, body)


def _record(*ids: str, candidate: int = 2, state: str = "open") -> SharedExecutionRecord:
    return SharedExecutionRecord(
        ticket_key="PILOT-1",
        worker_id="w",
        developer_account_id=DEV,
        candidate_number=2,
        deviations=[
            DeviationRecord(id=i, summary=f"{i} differs", candidate=candidate, announced_at=T0, state=state)  # type: ignore[arg-type]
            for i in ids
        ],
    )


def test_accept_deviations_parses_bare_ids_lists_and_notes() -> None:
    d = parse_decision(f"ACCEPT DEVIATIONS {TOKEN}\nD1, D3\nD4: fine as built")
    assert d is not None and d.kind is DecisionKind.ACCEPT_DEVIATIONS and not d.problems
    assert d.items == {"D1": "", "D3": "", "D4": "fine as built"}
    bare = parse_decision(f"ACCEPT DEVIATIONS {TOKEN}")
    assert bare is not None and bare.items == {} and not bare.problems
    wrong = parse_decision("ACCEPT DEVIATIONS PILOT-1-SPEC-v2")
    assert wrong is not None and wrong.problems  # bound to the candidate, not a document


def test_change_requests_against_a_candidate_may_name_deviations() -> None:
    code = collect_feedback(
        [_comment("1", f"CHANGE CODE {TOKEN}\nD2: keep the blue button\nF1: fix the typo")],
        token=TOKEN,
        kinds={DecisionKind.CHANGE_CODE},
        since=T0,
        allowed_authors={APPROVER},
    )
    assert code.items == {"D2": "keep the blue button", "F1": "fix the typo"} and not code.problems
    spec = collect_feedback(
        [_comment("2", "CHANGE SPEC PILOT-1-SPEC-v2\nD2: no")],
        token="PILOT-1-SPEC-v2",
        kinds={DecisionKind.CHANGE_SPEC},
        since=T0,
        allowed_authors={APPROVER},
    )
    assert spec.problems  # a specification has no deviations


def test_only_approvers_accept_and_only_open_deviations_of_this_candidate() -> None:
    rec = _record("D1", "D2")
    some = deviations.accepted(
        [_comment("1", f"ACCEPT DEVIATIONS {TOKEN}\nD2\nD9")], rec, token=TOKEN, approvers={APPROVER}
    )
    assert some.ids == ("D2",) and any("D9" in p for p in some.problems)
    everyone = deviations.accepted(
        [_comment("1", f"ACCEPT DEVIATIONS {TOKEN}", author=DEV)], rec, token=TOKEN, approvers={APPROVER}
    )
    assert everyone.ids == () and everyone.problems
    earlier = deviations.accepted(
        [_comment("1", f"ACCEPT DEVIATIONS {TOKEN}", minutes=-5)], rec, token=TOKEN, approvers={APPROVER}
    )
    assert earlier.ids == ()  # written before the deviations were announced
    stale = _record("D1", candidate=1)
    assert deviations.open_deviations(stale) == []  # a newer candidate is verified afresh


def test_deviation_ids_are_unique_and_shaped() -> None:
    base = {
        "schema_version": 1,
        "contract_id": "delivery.review-ticket/v1",
        "run_id": "r1",
        "ticket_key": "PILOT-1",
        "stage": "verification",
        "procedure": "review-ticket",
        "input_revision": "a" * 64,
        "outcome": "completed",
        "summary": "ok",
    }
    one = {"id": "D1", "description": "green"}
    assert StageResult.model_validate({**base, "deviations": [one]}).deviations[0].id == "D1"
    with pytest.raises(ValidationError):
        StageResult.model_validate({**base, "deviations": [one, one]})
    with pytest.raises(ValidationError):
        Deviation.model_validate({"id": "F1", "description": "x"})


def test_comments_ask_rather_than_fail() -> None:
    devs = deviations.to_records(
        [
            Deviation(id="D1", description="Green button.", criterion_id="AC1", requested=True),
            Deviation(id="D2", description="Export button " * 40),
        ],
        2,
    )
    assert devs[1].summary.endswith("…") and len(devs[1].summary) <= deviations.SUMMARY_CHARS
    review = "\n".join(comments.deviations_section(devs, TOKEN, Status.CODE_REVIEW))
    assert "D1** (asked for by the developer; changes AC1)" in review
    assert "D2** (not asked for: Claude went beyond the specification)" in review
    assert f"ACCEPT DEVIATIONS {TOKEN}" in review and "Submit follow-up changes" in review
    assert "Release preparation waits until each is decided" in review
    changes = "\n".join(comments.deviations_section(devs, TOKEN, Status.CHANGES_REQUESTED))
    assert "SUBMIT CHANGES" in changes and "nobody names is left as is" in changes
    assert comments.deviations_section([], TOKEN, Status.CODE_REVIEW) == []
