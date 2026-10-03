"""Deviations from the approved specification, and what humans decide about them.

People change things during development (usually by asking for them in the open Claude
session), so a candidate can differ from the approved specification in ways that work. The
review and verification report those as deviations (D1, D2...), separately from defects, and
a deviation never fails verification. For each one a human decides:

- acceptable: an approver comments ``ACCEPT DEVIATIONS <code token>`` (optionally listing the
  IDs). The next coordinator run on the ticket has Claude rewrite the specification to include
  them and publishes that revision as approved: no new refinement or planning round.
- not acceptable: the D-item goes in a change request (``CHANGE CODE`` / ``SUBMIT CHANGES``)
  and development changes the code back to the specification.

A deviation nobody names is never sent back to development: work is not undone unless asked.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from datetime import datetime

from delivery.feedback import CommentDecision, DecisionKind, decisions
from delivery.models import Deviation, DeviationRecord, SharedExecutionRecord
from delivery.ports import JiraComment

SUMMARY_CHARS = 280


def to_records(found: list[Deviation], candidate: int) -> list[DeviationRecord]:
    """Compact records for the shared record (the full text goes to deviations.json)."""
    out = []
    for d in found:
        text = " ".join(d.description.split())
        if len(text) > SUMMARY_CHARS:
            text = text[: SUMMARY_CHARS - 2].rsplit(" ", 1)[0] + " …"
        out.append(
            DeviationRecord(
                id=d.id,
                summary=text,
                criterion_id=d.criterion_id,
                requested=d.requested,
                candidate=candidate,
            )
        )
    return out


def open_deviations(rec: SharedExecutionRecord) -> list[DeviationRecord]:
    """Undecided deviations of the current candidate (a new candidate is verified afresh)."""
    return [d for d in rec.deviations if d.state == "open" and d.candidate == rec.candidate_number]


@dataclass(frozen=True)
class Acceptance:
    ids: tuple[str, ...]
    comments: tuple[CommentDecision, ...]
    problems: tuple[str, ...]


def accepted(
    comments: list[JiraComment],
    rec: SharedExecutionRecord,
    *,
    token: str,
    approvers: Container[str],
) -> Acceptance:
    """Open deviations an approver accepted with ``ACCEPT DEVIATIONS <token>``. A comment with
    no IDs accepts every deviation open when it was written."""
    pending = open_deviations(rec)
    if not pending:
        return Acceptance((), (), ())
    times = [d.announced_at for d in pending if d.announced_at is not None]
    since: datetime | None = min(times) if times else None
    found = decisions(comments, token=token, kinds={DecisionKind.ACCEPT_DEVIATIONS}, since=since)
    ids = {d.id for d in pending}
    chosen: set[str] = set()
    used: list[CommentDecision] = []
    problems: list[str] = []
    for cd in found:
        if cd.comment.author_account_id not in approvers:
            problems.append(f"comment {cd.comment.id} is not from an approver (only approvers accept)")
            continue
        problems.extend(cd.decision.problems)
        named = set(cd.decision.items)
        other = sorted(n for n in named if not n.startswith("D"))
        unknown = sorted(n for n in named - ids if n.startswith("D"))
        if other:
            problems.append(f"comment {cd.comment.id} lists {', '.join(other)}; deviations are D1, D2...")
        if unknown:
            problems.append(f"comment {cd.comment.id} names {', '.join(unknown)}: not open deviations")
        chosen |= (named & ids) if named else ids
        used.append(cd)
    order = [d.id for d in pending if d.id in chosen]
    return Acceptance(tuple(order), tuple(used), tuple(problems))


def change_back(record: DeviationRecord, note: str) -> str:
    """A rejected deviation as a development work item."""
    where = f" ({record.criterion_id})" if record.criterion_id else ""
    text = (
        f"Deviation from the approved specification{where} that was not accepted: {record.summary} "
        "Change the code so it follows the approved specification here."
    )
    return f"{text} Note: {note}" if note else text


def describe(d: Deviation | None, record: DeviationRecord) -> str:
    """An accepted deviation as input for the specification rewrite."""
    if d is None:
        where = f" Criterion: {record.criterion_id}." if record.criterion_id else ""
        return f"{record.summary}{where}"
    parts = [d.description]
    if d.criterion_id:
        parts.append(f"Criterion: {d.criterion_id}.")
    if d.requested:
        parts.append(f"Asked for by the developer: {d.request or 'yes'}.")
    if d.spec_change:
        parts.append(f"Proposed specification wording: {d.spec_change}")
    return " ".join(parts)
