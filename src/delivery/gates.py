"""Human gate validation, GitHub review/check evidence and approval supersession.

A human decision counts only when an authorised account posted the exact decision
token for the current artefact revision AND an authorised account performed the
matching Jira transition. The coordinator never performs a human route, so a human
route in the changelog was not made by the coordinator, even when the coordinator
authenticates as the same Jira account (a limitation recorded in the setup profile).
"""

from __future__ import annotations

from collections.abc import Container, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from delivery.feedback import (
    CommentDecision,
    DecisionKind,
    FeedbackSet,
    collect_feedback,
    decisions,
)
from delivery.models import (
    CheckResult,
    DecisionEvidence,
    GateKind,
    GateRecord,
    GateState,
    utcnow,
)
from delivery.ports import CheckRun, CommitStatus, JiraComment, PullRequest, Review, StatusChange

GATE_REVISION_PREFIX = {
    GateKind.SPEC: "v",
    GateKind.PLAN: "v",
    GateKind.RELEASE: "v",
    GateKind.CODE: "c",
    GateKind.ACCEPT: "c",
    GateKind.RECORD: "v",
}


def gate_token(ticket_key: str, kind: GateKind, revision: int) -> str:
    token_kind = "RELEASE" if kind is GateKind.RECORD else kind.value
    return f"{ticket_key}-{token_kind}-{GATE_REVISION_PREFIX[kind]}{revision}"


class GateOutcome(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    WAITING = "waiting"
    REJECTED = "rejected"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class GateEval:
    outcome: GateOutcome
    reason: str
    evidence: DecisionEvidence | None = None
    feedback: FeedbackSet | None = None
    decisions: tuple[CommentDecision, ...] = ()
    next_action: str = ""


def _evidence(cd: CommentDecision, change: StatusChange) -> DecisionEvidence:
    return DecisionEvidence(
        comment_id=cd.comment.id,
        comment_author=cd.comment.author_account_id,
        comment_digest=cd.body_digest,
        comment_updated=cd.comment.updated,
        history_id=change.history_id,
        transition_author=change.author_account_id,
        transition_at=change.created,
    )


def evaluate_human_gate(
    gate: GateRecord,
    *,
    entry: StatusChange,
    comments: list[JiraComment],
    review_status_id: str,
    approve_status_id: str,
    change_status_id: str | None,
    approve_kind: DecisionKind,
    change_kinds: set[DecisionKind],
    approvers: Container[str],
    excluded_comment_ids: set[str] | None = None,
) -> GateEval:
    """Validate the human decision that moved a ticket out of a review status.

    ``entry`` is the status change that brought the ticket into its current status.
    """
    if gate.state is GateState.SUPERSEDED:
        return GateEval(
            GateOutcome.REJECTED,
            f"gate {gate.token} was superseded by {gate.superseded_by or 'a newer revision'}",
            next_action="Review the current revision and use its token.",
        )
    if entry.from_id != review_status_id:
        return GateEval(GateOutcome.REJECTED, "ticket did not arrive from the review status")
    if entry.created < gate.published_at:
        return GateEval(
            GateOutcome.REJECTED,
            f"transition predates gate {gate.token}; it belongs to an older revision",
        )
    exclude = excluded_comment_ids or set()
    approvals = decisions(
        comments,
        token=gate.token,
        kinds={approve_kind},
        since=gate.published_at,
        exclude_ids=exclude,
    )
    changes = decisions(
        comments, token=gate.token, kinds=change_kinds, since=gate.published_at, exclude_ids=exclude
    )
    auth_approvals = [cd for cd in approvals if cd.comment.author_account_id in approvers]
    auth_changes = [cd for cd in changes if cd.comment.author_account_id in approvers]
    unauthorised = [cd for cd in approvals + changes if cd not in auth_approvals + auth_changes]

    if entry.author_account_id is None:
        return GateEval(
            GateOutcome.REJECTED,
            "the transition has no human author (automation cannot count as human approval)",
        )
    if entry.author_account_id not in approvers:
        return GateEval(
            GateOutcome.REJECTED,
            f"transition by account {entry.author_account_id} who is not an authorised approver",
            next_action="An authorised approver must make this decision.",
        )
    if auth_approvals and auth_changes:
        return GateEval(
            GateOutcome.CONFLICT,
            f"conflicting decisions for {gate.token}: approval and change request both present",
            decisions=tuple(auth_approvals + auth_changes),
            next_action="Resolve the conflict in Jira, then repeat the intended action.",
        )

    if entry.to_id == approve_status_id:
        if not auth_approvals:
            reason = (
                "approval comment is from an unauthorised account"
                if unauthorised
                else f"no `{approve_kind.value} {gate.token}` comment found"
            )
            return GateEval(
                GateOutcome.WAITING,
                reason,
                next_action=f"Add the comment `{approve_kind.value} {gate.token}`.",
            )
        edited = [cd for cd in auth_approvals if cd.comment.edited]
        if edited:
            return GateEval(
                GateOutcome.CONFLICT,
                f"approval comment {edited[0].comment.id} was edited after posting",
                next_action="Add a fresh, unedited approval comment.",
            )
        problems = [p for cd in auth_approvals for p in cd.decision.problems]
        if problems:
            return GateEval(GateOutcome.REJECTED, "; ".join(problems))
        return GateEval(
            GateOutcome.APPROVED,
            f"{approve_kind.value} {gate.token} by {auth_approvals[0].comment.author_account_id}",
            evidence=_evidence(auth_approvals[0], entry),
            decisions=tuple(auth_approvals),
        )

    if change_status_id is not None and entry.to_id == change_status_id:
        fb = collect_feedback(
            comments,
            token=gate.token,
            kinds=change_kinds,
            since=gate.published_at,
            allowed_authors=approvers,
        )
        if not fb.comments:
            kind = sorted(change_kinds)[0]
            return GateEval(
                GateOutcome.WAITING,
                f"no `{kind.value} {gate.token}` change request found",
                next_action=f"Comment `{kind.value} {gate.token}` with numbered F1.. items.",
            )
        if fb.problems:
            return GateEval(GateOutcome.WAITING, "; ".join(fb.problems), feedback=fb)
        return GateEval(
            GateOutcome.CHANGES_REQUESTED,
            f"{len(fb.items)} feedback items for {gate.token}",
            evidence=_evidence(fb.comments[0], entry),
            feedback=fb,
            decisions=fb.comments,
        )

    return GateEval(GateOutcome.REJECTED, "transition target does not belong to this gate")


# --------------------------------------------------------------------------- GitHub


@dataclass(frozen=True)
class ReviewEval:
    ok: bool
    reason: str
    review: Review | None = None
    blocking: tuple[Review, ...] = ()


def evaluate_reviews(
    pr: PullRequest,
    reviews: list[Review],
    *,
    allowed_logins: list[str],
    require_independent: bool,
) -> ReviewEval:
    latest: dict[str, Review] = {}
    for r in sorted(reviews, key=lambda r: (r.submitted_at or utcnow(), r.id)):
        if r.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r.user_login.lower()] = r
    blocking = tuple(r for r in latest.values() if r.state == "CHANGES_REQUESTED")
    if blocking:
        return ReviewEval(False, f"changes requested by {blocking[0].user_login}", None, blocking)
    if not require_independent:
        return ReviewEval(True, "independent review not required by configuration")
    allowed = {u.lower() for u in allowed_logins}
    stale = []
    for r in latest.values():
        if r.state != "APPROVED":
            continue
        if r.user_login.lower() == pr.author_login.lower():
            continue  # the PR author cannot approve their own PR
        if r.user_type != "User":
            continue
        if allowed and r.user_login.lower() not in allowed:
            continue
        if r.commit_id != pr.head_sha:
            stale.append(r)
            continue
        return ReviewEval(True, f"approved by {r.user_login} at {r.commit_id[:12]}", r)
    if stale:
        return ReviewEval(
            False,
            f"approval by {stale[0].user_login} is for {stale[0].commit_id[:12]}, "
            f"not the current head {pr.head_sha[:12]}",
        )
    return ReviewEval(
        False,
        "no independent human GitHub approval on the current head (author cannot self-approve)",
    )


@dataclass(frozen=True)
class CIEval:
    ok: bool
    pending: bool
    results: list[CheckResult]
    problems: list[str] = field(default_factory=list)


_PRODUCER_LOGINS = {"github-actions": {"github-actions", "github-actions[bot]"}}


def _producer_ok(expected: str, actual: str) -> bool:
    return actual in _PRODUCER_LOGINS.get(expected, {expected, f"{expected}[bot]"})


def evaluate_ci(
    sha: str,
    *,
    required_names: list[str],
    expected_producer: str,
    check_runs: list[CheckRun],
    statuses: list[CommitStatus],
    allow_neutral: Sequence[str] = (),
    allow_skipped: Sequence[str] = (),
) -> CIEval:
    """Require each configured check from the expected producer on the exact SHA.

    Missing, pending, skipped and neutral are not passes unless explicitly allowed.
    Repeated runs with the same name: the most recent attempt is authoritative.
    """
    results: list[CheckResult] = []
    problems: list[str] = []
    pending = False
    for name in required_names:
        runs = [r for r in check_runs if r.name == name and r.head_sha == sha]
        stats = [s for s in statuses if s.context == name]
        foreign = [r.app_slug for r in runs if not _producer_ok(expected_producer, r.app_slug)]
        foreign += [s.creator_login for s in stats if not _producer_ok(expected_producer, s.creator_login)]
        if foreign:
            problems.append(f"{name}: ignored result from unexpected producer {sorted(set(foreign))}")
        runs = [r for r in runs if _producer_ok(expected_producer, r.app_slug)]
        stats = [s for s in stats if _producer_ok(expected_producer, s.creator_login)]
        if not runs and not stats:
            results.append(CheckResult(name=name, source="ci", sha=sha, conclusion="missing"))
            problems.append(f"{name}: no result from {expected_producer} for {sha[:12]}")
            continue
        conclusions: list[tuple[str, str]] = []
        if runs:
            run = max(runs, key=lambda r: r.id)
            if run.status != "completed":
                conclusions.append(("pending", run.html_url))
            elif (
                run.conclusion == "success"
                or (run.conclusion == "neutral" and name in allow_neutral)
                or (run.conclusion == "skipped" and name in allow_skipped)
            ):
                conclusions.append(("passed", run.html_url))
            else:
                conclusions.append(("failed", run.html_url))
                problems.append(f"{name}: check run concluded {run.conclusion}")
        if stats:
            st = max(stats, key=lambda s: s.id)
            mapping = {"success": "passed", "pending": "pending"}
            c = mapping.get(st.state, "failed")
            conclusions.append((c, st.target_url))
            if c == "failed":
                problems.append(f"{name}: commit status {st.state}")
        if any(c == "failed" for c, _ in conclusions):
            final = "failed"
        elif any(c == "pending" for c, _ in conclusions):
            final = "pending"
            pending = True
        else:
            final = "passed"
        results.append(
            CheckResult(
                name=name,
                source="ci",
                sha=sha,
                conclusion=final,
                url=conclusions[0][1] or None,
                producer=expected_producer,
            )
        )
    ok = all(r.conclusion == "passed" for r in results)
    return CIEval(ok, pending, results, problems)


# --------------------------------------------------------------------------- supersession

_DOWNSTREAM: dict[GateKind, set[GateKind]] = {
    GateKind.SPEC: {
        GateKind.SPEC,
        GateKind.PLAN,
        GateKind.CODE,
        GateKind.ACCEPT,
        GateKind.RELEASE,
        GateKind.RECORD,
    },
    GateKind.PLAN: {
        GateKind.PLAN,
        GateKind.CODE,
        GateKind.ACCEPT,
        GateKind.RELEASE,
        GateKind.RECORD,
    },
    GateKind.CODE: {GateKind.CODE, GateKind.ACCEPT, GateKind.RELEASE, GateKind.RECORD},
    GateKind.RELEASE: {GateKind.RELEASE, GateKind.RECORD},
}


def supersede_for_new_revision(
    gates: list[GateRecord], changed: GateKind, new_token: str
) -> list[GateRecord]:
    """Mark gates that depended on the changed artefact as superseded (kept as history)."""
    affected = _DOWNSTREAM[changed]
    out = []
    for g in gates:
        if g.kind in affected and g.state is not GateState.SUPERSEDED and g.token != new_token:
            g = g.model_copy(update={"state": GateState.SUPERSEDED, "superseded_by": new_token})
        out.append(g)
    return out


def current_gate(gates: list[GateRecord], kind: GateKind) -> GateRecord | None:
    live = [g for g in gates if g.kind is kind and g.state is not GateState.SUPERSEDED]
    return max(live, key=lambda g: g.revision) if live else None


def approved_gate(gates: list[GateRecord], kind: GateKind) -> GateRecord | None:
    g = current_gate(gates, kind)
    return g if g and g.state is GateState.APPROVED else None
