"""Deviations from the approved specification, and what humans decide about them.

People change things during development (usually by asking for them in the open Claude
session), so a candidate can differ from the approved specification in ways that work. The
review and verification report those as deviations (D1, D2...), separately from defects, and
a deviation never fails verification. For each one a human decides, by moving the ticket:

- acceptable: Approve code and Accept delivery accept the candidate as it is, deviations
  included. Release preparation first has Claude rewrite the specification to include them and
  publishes that revision as approved: no new refinement or planning round.
- not acceptable: request changes and name the D-item in a comment (``D2: follow the
  specification``); development changes the code back to the specification.

A deviation nobody names is never sent back to development: work is not undone unless asked.
"""

from __future__ import annotations

from delivery.models import Deviation, DeviationRecord, SharedExecutionRecord

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
