"""Deterministic correlation of human comments: answers, change requests and decisions.

Exact syntax is the v1 correlation mechanism. Nothing here uses a model to interpret a
comment, and a vague comment is never treated as approval. If a token is missing or
ambiguous the ticket stays paused and the human is told exactly how to resubmit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from delivery.models import digest
from delivery.ports import JiraComment
from delivery.workflow import STAGES, Stage

MARKER_PREFIX = "delivery-op:"
_KEY = r"[A-Z][A-Z0-9_]{1,9}-\d{1,9}"
_ROUND_CODES = "|".join(d.round_code for d in STAGES.values())

GATE_TOKEN = re.compile(rf"^(?P<key>{_KEY})-(?P<kind>SPEC|PLAN|RELEASE)-v(?P<rev>\d{{1,4}})$")
CANDIDATE_TOKEN = re.compile(rf"^(?P<key>{_KEY})-(?P<kind>CODE|ACCEPT)-c(?P<rev>\d{{1,4}})$")
ROUND_TOKEN = re.compile(rf"^(?P<key>{_KEY})-(?P<code>{_ROUND_CODES})-R(?P<n>\d{{1,3}})$")
WARNING_ID = re.compile(r"^OVL-[0-9a-f]{10}$")


class DecisionKind(StrEnum):
    APPROVE_SPEC = "APPROVE SPEC"
    CHANGE_SPEC = "CHANGE SPEC"
    APPROVE_PLAN = "APPROVE PLAN"
    CHANGE_PLAN = "CHANGE PLAN"
    APPROVE_CODE = "APPROVE CODE"
    CHANGE_CODE = "CHANGE CODE"
    ACCEPT_DELIVERY = "ACCEPT DELIVERY"
    CHANGE_ACCEPTANCE = "CHANGE ACCEPTANCE"
    APPROVE_RELEASE = "APPROVE RELEASE"
    CHANGE_RELEASE = "CHANGE RELEASE"
    RECORD_RELEASE = "RECORD RELEASE"
    REVISE_SCOPE = "REVISE SCOPE"
    SUBMIT_CHANGES = "SUBMIT CHANGES"
    ANSWERS = "ANSWERS"
    OVERLAP = "OVERLAP"


_TOKEN_KIND: dict[DecisionKind, str] = {
    DecisionKind.APPROVE_SPEC: "SPEC",
    DecisionKind.CHANGE_SPEC: "SPEC",
    DecisionKind.REVISE_SCOPE: "SPEC",
    DecisionKind.APPROVE_PLAN: "PLAN",
    DecisionKind.CHANGE_PLAN: "PLAN",
    DecisionKind.APPROVE_CODE: "CODE",
    DecisionKind.CHANGE_CODE: "CODE",
    DecisionKind.SUBMIT_CHANGES: "CODE",
    DecisionKind.ACCEPT_DELIVERY: "ACCEPT",
    DecisionKind.CHANGE_ACCEPTANCE: "ACCEPT",
    DecisionKind.APPROVE_RELEASE: "RELEASE",
    DecisionKind.CHANGE_RELEASE: "RELEASE",
    DecisionKind.RECORD_RELEASE: "RELEASE",
}

_HEADER = re.compile(
    r"^(?P<verb>APPROVE|CHANGE|ACCEPT|RECORD|REVISE|SUBMIT)\s+"
    r"(?P<subject>SPEC|PLAN|CODE|DELIVERY|ACCEPTANCE|RELEASE|SCOPE|CHANGES)\s+"
    r"(?P<token>\S+)\s*$"
)
_ANSWERS = re.compile(r"^ANSWERS\s+(?P<token>\S+)\s*$")
_OVERLAP = re.compile(
    r"^OVERLAP\s+(?P<token>OVL-[0-9a-f]{10})\s+(?P<choice>PROCEED|RESCOPE|WAIT\s+" + _KEY + r")\s*$"
)
_ITEM = re.compile(r"^(?P<id>[QF]\d{1,3})\s*[:.)-]\s*(?P<text>.*)$")
_FIELD = re.compile(r"^(?P<name>commit|environment|merged-pr|pr)\s*:\s*(?P<value>\S+)\s*$", re.I)


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    token: str
    items: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    choice: str = ""
    problems: tuple[str, ...] = ()


def is_coordinator_comment(comment: JiraComment) -> bool:
    return MARKER_PREFIX in comment.body_text


def token_matches_kind(kind: DecisionKind, token: str) -> bool:
    if kind is DecisionKind.ANSWERS:
        return bool(ROUND_TOKEN.match(token))
    if kind is DecisionKind.OVERLAP:
        return bool(WARNING_ID.match(token))
    expected = _TOKEN_KIND[kind]
    m = GATE_TOKEN.match(token) or CANDIDATE_TOKEN.match(token)
    return bool(m and m.group("kind") == expected)


def parse_decision(text: str) -> Decision | None:
    """Parse a human decision comment. Returns None when it is not a decision at all."""
    lines = [ln.strip().strip("`").strip() for ln in text.strip().splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None
    head, body = lines[0], lines[1:]
    kind: DecisionKind | None = None
    token = ""
    choice = ""
    if m := _HEADER.match(head):
        try:
            kind = DecisionKind(f"{m.group('verb')} {m.group('subject')}")
        except ValueError:
            return None
        token = m.group("token")
    elif m := _ANSWERS.match(head):
        kind, token = DecisionKind.ANSWERS, m.group("token")
    elif m := _OVERLAP.match(head):
        kind, token = DecisionKind.OVERLAP, m.group("token")
        choice = " ".join(m.group("choice").split())
    if kind is None:
        return None
    problems: list[str] = []
    if not token_matches_kind(kind, token):
        problems.append(f"token {token!r} does not match {kind.value}")
    items: dict[str, str] = {}
    fields: dict[str, str] = {}
    current: str | None = None
    for ln in body:
        if MARKER_PREFIX in ln:
            continue
        if im := _ITEM.match(ln):
            current = im.group("id")
            if current in items:
                problems.append(f"{current} appears more than once")
            items[current] = im.group("text").strip()
        elif fm := _FIELD.match(ln):
            fields[fm.group("name").lower()] = fm.group("value")
            current = None
        elif current is not None:
            items[current] = (items[current] + "\n" + ln).strip()
    return Decision(kind, token, items, fields, choice, tuple(problems))


@dataclass(frozen=True)
class CommentDecision:
    comment: JiraComment
    decision: Decision

    @property
    def body_digest(self) -> str:
        return digest(self.comment.body_text)


def decisions(
    comments: list[JiraComment],
    *,
    token: str,
    kinds: set[DecisionKind],
    since: datetime | None = None,
    exclude_ids: set[str] | None = None,
) -> list[CommentDecision]:
    """Human decisions for one token since a gate was published, oldest first."""
    out = []
    for c in comments:
        if exclude_ids and c.id in exclude_ids:
            continue
        if is_coordinator_comment(c):
            continue
        if since and c.created < since:
            continue
        d = parse_decision(c.body_text)
        if d and d.kind in kinds and d.token == token:
            out.append(CommentDecision(c, d))
    return sorted(out, key=lambda cd: (cd.comment.created, cd.comment.id))


# --------------------------------------------------------------------------- answers


def round_token(ticket_key: str, stage: Stage, n: int) -> str:
    return f"{ticket_key}-{STAGES[stage].round_code}-R{n}"


@dataclass(frozen=True)
class AnswerSet:
    token: str
    answers: dict[str, str]
    comments: tuple[CommentDecision, ...]
    missing: tuple[str, ...]
    unauthorised: tuple[str, ...]
    edited_after_submit: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing and not self.problems

    @property
    def usable(self) -> bool:
        """At least one authorised answer exists for this round."""
        return bool(self.answers) and not self.problems


def collect_answers(
    comments: list[JiraComment],
    *,
    token: str,
    question_ids: list[str],
    since: datetime,
    allowed_authors: set[str],
    submitted_at: datetime | None = None,
) -> AnswerSet:
    """Merge several answer comments for one round. Later answers to the same ID win."""
    found = decisions(comments, token=token, kinds={DecisionKind.ANSWERS}, since=since)
    answers: dict[str, str] = {}
    used: list[CommentDecision] = []
    unauthorised: list[str] = []
    edited: list[str] = []
    problems: list[str] = []
    for cd in found:
        if cd.comment.author_account_id not in allowed_authors:
            unauthorised.append(cd.comment.id)
            continue
        if submitted_at and cd.comment.updated > submitted_at and cd.comment.edited:
            edited.append(cd.comment.id)
        problems.extend(cd.decision.problems)
        unknown = [q for q in cd.decision.items if q not in question_ids]
        if unknown:
            problems.append(f"comment {cd.comment.id} answers unknown questions {unknown}")
        for qid, text in cd.decision.items.items():
            if qid in question_ids and text:
                answers[qid] = text
        used.append(cd)
    missing = tuple(q for q in question_ids if q not in answers)
    if edited:
        problems.append(f"answer comments edited after Submit answers: {edited}")
    return AnswerSet(
        token, answers, tuple(used), missing, tuple(unauthorised), tuple(edited), tuple(problems)
    )


@dataclass(frozen=True)
class FeedbackSet:
    token: str
    items: dict[str, str]
    comments: tuple[CommentDecision, ...]
    unauthorised: tuple[str, ...]
    problems: tuple[str, ...]


def collect_feedback(
    comments: list[JiraComment],
    *,
    token: str,
    kinds: set[DecisionKind],
    since: datetime | None,
    allowed_authors: set[str],
) -> FeedbackSet:
    """Numbered feedback (F1, F2...) bound to one artefact or candidate token."""
    found = decisions(comments, token=token, kinds=kinds, since=since)
    items: dict[str, str] = {}
    used: list[CommentDecision] = []
    unauthorised: list[str] = []
    problems: list[str] = []
    for cd in found:
        if cd.comment.author_account_id not in allowed_authors:
            unauthorised.append(cd.comment.id)
            continue
        problems.extend(cd.decision.problems)
        for fid, text in cd.decision.items.items():
            if not fid.startswith("F"):
                problems.append(f"comment {cd.comment.id} uses {fid}; feedback items are F1, F2...")
            elif text:
                key = fid if fid not in items else f"{fid}@{cd.comment.id}"
                items[key] = text
        used.append(cd)
    if used and not items:
        problems.append("change request has no numbered feedback items (F1: ...)")
    return FeedbackSet(token, items, tuple(used), tuple(unauthorised), tuple(problems))


# --------------------------------------------------------------------------- templates


def answer_template(token: str, question_ids: list[str]) -> str:
    return "\n".join([f"ANSWERS {token}", *(f"{q}: <your answer>" for q in question_ids)])


def change_template(kind: DecisionKind, token: str) -> str:
    return f"{kind.value} {token}\nF1: <requested change>\nF2: <another change>"


def approve_template(kind: DecisionKind, token: str) -> str:
    return f"{kind.value} {token}"


def record_release_template(token: str, environment: str) -> str:
    return (
        f"{DecisionKind.RECORD_RELEASE.value} {token}\n"
        "commit: <released commit SHA on the base branch>\n"
        f"environment: {environment}\n"
        "merged-pr: <PR number>"
    )
