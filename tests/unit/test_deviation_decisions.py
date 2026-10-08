"""Deviation decisions: naming them in a change request, and how they read in Jira."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from delivery import comments, deviations
from delivery.feedback import items
from delivery.models import Deviation, DeviationRecord, SharedExecutionRecord, StageResult
from delivery.ports import JiraComment
from delivery.workflow import Status

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
APPROVER = "approver-0001"
DEV = "dev-account-0001"


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


def test_change_requests_against_a_candidate_may_name_deviations() -> None:
    written = [_comment("1", "D2: keep the blue button\nF1: fix the typo")]
    assert items(written, named="FD", free="F") == {"D2": "keep the blue button", "F1": "fix the typo"}


def test_only_open_deviations_of_this_candidate() -> None:
    assert [d.id for d in deviations.open_deviations(_record("D1", "D2"))] == ["D1", "D2"]
    assert deviations.open_deviations(_record("D1", state="accepted")) == []
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
    review = "\n".join(comments.deviations_section(devs, Status.CODE_REVIEW))
    assert "D1** (asked for by the developer; changes AC1)" in review
    assert "D2** (not asked for: Claude went beyond the specification)" in review
    assert "Approving the code accepts them" in review and "request code changes" in review
    assert "ACCEPT DEVIATIONS" not in review
    changes = "\n".join(comments.deviations_section(devs, Status.CHANGES_REQUESTED))
    assert "Submit implementation" in changes
    assert comments.deviations_section([], Status.CODE_REVIEW) == []
