"""Human comments on a ticket: plain feedback, answers and the one remaining request line.

Decisions are Jira moves (delivery.gates): approving, asking for changes and submitting answers
need no comment. Comments are what people say, in their own words, and reach Claude as they
are: feedback for a change request, answers to a clarification round, notes left during a
review. Only one request still has a fixed first line, because it is not a move:
``CREATE TICKETS <token>`` (create proposed tickets).
"""

from __future__ import annotations

import re
from collections.abc import Container
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from delivery.ports import JiraComment
from delivery.workflow import STAGES, Stage

MARKER_PREFIX = "delivery-op:"
_KEY = r"[A-Z][A-Z0-9_]{1,9}-\d{1,9}"

GATE_TOKEN = re.compile(rf"^(?P<key>{_KEY})-(?P<kind>SPEC|PLAN|RELEASE)-v(?P<rev>\d{{1,4}})$")


class DecisionKind(StrEnum):
    # Create the tickets a specification or a spike's findings proposed (S1, S2...).
    CREATE_TICKETS = "CREATE TICKETS"


_HEADER = re.compile(r"^(?P<verb>CREATE)\s+(?P<subject>TICKETS)\s+(?P<token>\S+)\s*$")
_ITEM = re.compile(r"^(?P<id>[QFDS]\d{1,3})\s*[:.)-]\s*(?P<text>.*)$")
# Proposed-ticket IDs on their own, alone or as a list ("S1", "S1, S3"): no note needed.
_S_LIST = re.compile(r"^S\d{1,3}(?:\s*[,;\s]\s*S\d{1,3})*\s*[.]?$")


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    token: str
    items: dict[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


def is_coordinator_comment(comment: JiraComment) -> bool:
    return MARKER_PREFIX in comment.body_text


def token_matches_kind(kind: DecisionKind, token: str) -> bool:
    # A specification's proposals, or a spike's findings (reviewed as its plan).
    m = GATE_TOKEN.match(token)
    return bool(m and m.group("kind") in ("SPEC", "PLAN"))


def parse_decision(text: str) -> Decision | None:
    """Parse a request comment (CREATE TICKETS). None when it is not one."""
    lines = [ln.strip().strip("`").strip() for ln in text.strip().splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None
    head, body = lines[0], lines[1:]
    m = _HEADER.match(head)
    if not m:
        return None
    try:
        kind = DecisionKind(f"{m.group('verb')} {m.group('subject')}")
    except ValueError:
        return None
    token = m.group("token")
    problems: list[str] = []
    if not token_matches_kind(kind, token):
        problems.append(f"token {token!r} does not match {kind.value}")
    items: dict[str, str] = {}
    current: str | None = None
    for ln in body:
        if MARKER_PREFIX in ln:
            continue
        if _S_LIST.match(ln):
            for sid in re.findall(r"S\d{1,3}", ln):
                if sid in items:
                    problems.append(f"{sid} appears more than once")
                items[sid] = ""
            current = None
        elif im := _ITEM.match(ln):
            current = im.group("id")
            if current in items:
                problems.append(f"{current} appears more than once")
            items[current] = im.group("text").strip()
        elif current is not None:
            items[current] = (items[current] + "\n" + ln).strip()
    return Decision(kind, token, items, tuple(problems))


@dataclass(frozen=True)
class CommentDecision:
    comment: JiraComment
    decision: Decision


def decisions(
    comments: list[JiraComment],
    *,
    token: str,
    kinds: set[DecisionKind],
    since: datetime | None = None,
) -> list[CommentDecision]:
    """Request comments for one token since a gate was published, oldest first."""
    out = []
    for c in comments:
        if is_coordinator_comment(c):
            continue
        if since and c.created < since:
            continue
        d = parse_decision(c.body_text)
        if d and d.kind in kinds and d.token == token:
            out.append(CommentDecision(c, d))
    return sorted(out, key=lambda cd: (cd.comment.created, cd.comment.id))


def round_token(ticket_key: str, stage: Stage, n: int) -> str:
    """The name of a clarification round (internal: nobody types it)."""
    return f"{ticket_key}-{STAGES[stage].round_code}-R{n}"


# --------------------------------------------------------------------------- what people said


def said(
    comments: list[JiraComment],
    *,
    since: datetime | None,
    authors: Container[str],
) -> list[JiraComment]:
    """What people wrote on the ticket in a window, oldest first, in their own words.

    Leaves out the coordinator's comments (even when it uses the same Jira account), request
    lines (CREATE TICKETS) and ``FOR CLAUDE`` notes, which reach Claude as notes
    already. Comments by accounts that may not decide are left out too.
    """
    out = [
        c
        for c in comments
        if not is_coordinator_comment(c)
        and c.author_account_id in authors
        and (since is None or c.created >= since)
        and parse_decision(c.body_text) is None
        and not _FOR_CLAUDE.match(c.body_text.strip())
        and c.body_text.strip()
    ]
    return sorted(out, key=lambda c: (c.created, c.id))


def items(
    comments: list[JiraComment], *, named: str, free: str, taken: Container[str] = ()
) -> dict[str, str]:
    """Items for Claude from plain comments. Lines a person numbered themselves (``F2: ...`` or,
    for change requests, ``D1: ...``; ``Q1: ...`` for answers) keep their IDs (``named`` lists
    the letters); a comment without such lines is one item, numbered ``free`` 1, 2...

    A change ID already used (here or in ``taken``, such as a review finding the person is
    talking about) is kept apart (``F1@<comment id>``); a repeated answer replaces the earlier
    one, so a person can correct an answer by writing it again.
    """
    out: dict[str, str] = {}
    n = 0
    for c in comments:
        found: dict[str, str] = {}
        current: str | None = None
        for ln in (ln.strip() for ln in c.body_text.strip().splitlines()):
            if MARKER_PREFIX in ln:
                continue
            m = _ITEM.match(ln)
            if m and m.group("id")[0] in named:
                current = m.group("id")
                found[current] = m.group("text").strip()
            elif current is not None and ln:
                found[current] = (found[current] + "\n" + ln).strip()
        if not found:
            n += 1
            while f"{free}{n}" in out or f"{free}{n}" in taken:
                n += 1
            out[f"{free}{n}"] = c.body_text.strip()
            continue
        for iid, text in found.items():
            clash = iid in out or iid in taken
            key = f"{iid}@{c.id}" if clash and not iid.startswith("Q") else iid
            out[key] = text
    return out


_FOR_CLAUDE = re.compile(r"^FOR\s+CLAUDE\b", re.I)


_NOTE = re.compile(
    r"^FOR\s+CLAUDE"
    r"(?:\s+(?P<stage>refinement|planning|development|verification|"
    r"release(?:[ _](?:preparation|verification))?))?"
    r"\s*[:\-]?\s*(?P<text>.*)$",
    re.I,
)


_PROJECT_SCOPE = re.compile(r"^FOR\s+CLAUDE\s+project\b", re.I)


def note_text(comment: JiraComment, stage: str) -> str | None:
    """The guidance in a ``FOR CLAUDE [stage]`` comment for this stage, or None.

    The first line is ``FOR CLAUDE`` (every stage) or ``FOR CLAUDE development`` (one stage;
    ``verification`` also covers review); the note follows on that line
    after a colon or on the next lines.
    """
    if is_coordinator_comment(comment):
        return None
    first, _, rest = comment.body_text.strip().partition("\n")
    if _PROJECT_SCOPE.match(first.strip()):
        return None  # project guidance (delivery.guidance), not a note for this ticket's sessions
    m = _NOTE.match(first.strip())
    if not m:
        return None
    scope = (m.group("stage") or "").lower().replace(" ", "_")
    if scope and scope != stage:
        return None
    text = "\n".join(t for t in (m.group("text").strip(), rest.strip()) if t)
    return text or None


def claude_notes(
    comments: list[JiraComment], *, stage: str, allowed_authors: Container[str], limit: int = 10
) -> list[tuple[JiraComment, str]]:
    """Notes for Claude for this stage from the assignee or approvers, oldest first."""
    notes = [
        (c, text)
        for c in comments
        if c.author_account_id in allowed_authors and (text := note_text(c, stage)) is not None
    ]
    return notes[-limit:]
