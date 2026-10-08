"""Intake: validate how a ticket reached its ready status before any work starts.

The coordinator reads the status change that brought the ticket into its current ready
status, looks up the permitted route, and validates the input that route requires
(brief, the move that decided the current gate, what people wrote since the revision or
questions were published, a recorded release). Outcomes:

* READY: start the stage with the selected inputs.
* WAIT: something outside the ticket is missing (for example the PR is not merged yet);
  explain once and re-check on every poll. Nothing starts. A human decision is the Jira move
  itself: no comment is ever waited for.
* BLOCK: the route or decision is invalid (wrong resume stage, unauthorised actor,
  conflicting or edited decision, missing prerequisite). The coordinator starts the stage
  only to move it to Blocked with the correct resume stage and an explanation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError

from delivery import deviations
from delivery.config import Config
from delivery.feedback import items, said
from delivery.gates import (
    GateEval,
    GateOutcome,
    approved_gate,
    current_gate,
    evaluate_ci,
    evaluate_human_gate,
    evaluate_reviews,
)
from delivery.gates import gate_token as make_gate_token
from delivery.models import (
    PROPERTY_KEY,
    GateKind,
    GateRecord,
    GateState,
    SharedExecutionRecord,
    digest,
    utcnow,
)
from delivery.ports import GitHubPort, JiraComment, JiraIssue, JiraPort, StatusChange
from delivery.pr_feedback import review_items
from delivery.workflow import (
    FOLLOW_UP_SOURCES,
    PAUSED_STATUSES,
    ROUTES,
    STAGES,
    STATUS_NAMES,
    Actor,
    Requirement,
    Stage,
    Status,
    human_route,
    resume_route,
)


class RecordCorrupt(Exception):
    pass


async def load_record(jira: JiraPort, cfg: Config, key: str) -> SharedExecutionRecord:
    raw = await jira.get_property(key, PROPERTY_KEY)
    if raw is None:
        return SharedExecutionRecord(
            ticket_key=key,
            worker_id=cfg.identity.worker_id,
            developer_account_id=cfg.identity.developer_jira_account_id,
        )
    try:
        return SharedExecutionRecord.model_validate(raw)
    except ValidationError as exc:
        detail = f"{key}: shared execution record is invalid ({exc.error_count()} errors)"
        raise RecordCorrupt(detail) from None


@dataclass
class TicketContext:
    issue: JiraIssue
    status: Status | None
    comments: list[JiraComment]
    changes: list[StatusChange]
    record: SharedExecutionRecord

    @property
    def key(self) -> str:
        return self.issue.key

    def latest_entry(self, status_id: str) -> StatusChange | None:
        entries = [c for c in self.changes if c.to_id == status_id]
        return entries[-1] if entries else None

    def latest_change(self, from_id: str, to_id: str, since: datetime | None = None) -> StatusChange | None:
        found = [
            c
            for c in self.changes
            if c.from_id == from_id and c.to_id == to_id and (since is None or c.created >= since)
        ]
        return found[-1] if found else None


async def load_context(jira: JiraPort, cfg: Config, key: str) -> TicketContext:
    issue = await jira.get_issue(key)
    comments = await jira.comments(key)
    changes = sorted(await jira.status_changes(key), key=lambda c: (c.created, c.history_id))
    record = await load_record(jira, cfg, key)
    status = cfg.status_by_id().get(issue.view.status_id)
    return TicketContext(issue, status, comments, changes, record)


class IntakeKind(StrEnum):
    READY = "ready"
    WAIT = "wait"
    BLOCK = "block"


@dataclass
class Intake:
    kind: IntakeKind
    stage: Stage
    requirement: Requirement | None = None
    reason: str = ""
    next_action: str = ""
    entry: StatusChange | None = None
    selected: list[JiraComment] = field(default_factory=list)
    round_token: str | None = None
    feedback_token: str | None = None
    feedback_items: dict[str, str] = field(default_factory=dict)
    record: SharedExecutionRecord | None = None
    resume_stage: Stage | None = None
    blocker_kind: str = ""
    gate_token: str | None = None

    def persisted(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "stage": self.stage.value,
            "requirement": self.requirement.value if self.requirement else None,
            "reason": self.reason,
            "next_action": self.next_action,
            "entry": self.entry.history_id if self.entry else None,
            "selected": [c.id for c in self.selected],
            "round_token": self.round_token,
            "feedback_token": self.feedback_token,
            "feedback_items": self.feedback_items,
            "resume_stage": self.resume_stage.value if self.resume_stage else None,
            "blocker_kind": self.blocker_kind,
            "gate_token": self.gate_token,
        }

    @classmethod
    def restore(cls, data: dict[str, Any], ctx: TicketContext) -> Intake:
        """Rebuild an intake for a resumed run from persisted data and fresh comments."""
        ids = set(data.get("selected") or [])
        entry = next((c for c in ctx.changes if c.history_id == data.get("entry")), None)
        return cls(
            IntakeKind(data.get("kind", "ready")),
            Stage(data["stage"]),
            Requirement(data["requirement"]) if data.get("requirement") else None,
            data.get("reason", ""),
            data.get("next_action", ""),
            entry,
            [c for c in ctx.comments if c.id in ids],
            data.get("round_token"),
            data.get("feedback_token"),
            dict(data.get("feedback_items") or {}),
            ctx.record,
            Stage(data["resume_stage"]) if data.get("resume_stage") else None,
            data.get("blocker_kind", ""),
            data.get("gate_token"),
        )

    def material(self, brief_digest: str) -> dict[str, Any]:
        """Material inputs only. Outputs of a run (new pending gates, a new candidate) are
        excluded so that the same human entry always maps to the same attempt key."""
        rec = self.record
        decided = [
            (g.token, g.state.value, g.artefact_commit, g.candidate_sha)
            for g in (rec.gates if rec else [])
            if g.state in (GateState.APPROVED, GateState.CHANGES_REQUESTED)
        ]
        return {
            "stage": self.stage.value,
            "entry": self.entry.history_id if self.entry else None,
            "brief": brief_digest,
            "comments": sorted((c.id, digest(c.body_text)) for c in self.selected),
            "round": self.round_token,
            "feedback": self.feedback_token,
            "items": sorted(self.feedback_items.items()),
            "decided_gates": sorted(decided, key=str),
        }


@dataclass
class ReleaseIntake:
    """A ticket in Ready for release: can it be completed? Once its PR is merged and the decisions
    that led here (code approved, delivery accepted) stand, ``record`` carries them and
    ``merge_commit`` is the release."""

    ready: bool
    reason: str
    next_action: str = ""
    record: SharedExecutionRecord | None = None
    pr_number: int | None = None
    merge_commit: str = ""
    merged_by: str | None = None


class CodeEvidence(Protocol):
    async def __call__(self, record: SharedExecutionRecord) -> tuple[bool, str]: ...


def brief_text(issue: JiraIssue) -> str:
    """The brief as one string; its digest decides whether the input changed.

    Attachments are part of the brief: adding, replacing or removing a design changes it.
    """
    text = f"{issue.view.summary}\n\n{issue.description_text}".strip()
    if issue.attachments:
        listed = sorted(issue.attachments, key=lambda a: a.id)
        text += "\n\nAttachments:\n" + "\n".join(
            f"- {a.filename} ({a.mime_type or 'unknown type'}, {a.size} bytes, id {a.id})" for a in listed
        )
    return text


def brief_is_usable(issue: JiraIssue) -> bool:
    desc = re.sub(r"\s+", " ", issue.description_text).strip()
    return bool(issue.view.summary.strip()) and len(desc) >= 20


def _with_gate(rec: SharedExecutionRecord, gate: GateRecord) -> SharedExecutionRecord:
    gates = [gate if g.token == gate.token else g for g in rec.gates]
    return rec.model_copy(update={"gates": gates})


def _decided(gate: GateRecord, ev: GateEval, state: GateState) -> GateRecord:
    return gate.model_copy(update={"state": state, "evidence": ev.evidence, "decided_at": utcnow()})


# Jira-only review gates: (review status, approve target, change target). The code gate also
# needs GitHub evidence, so it is only decided on its own route.
_JIRA_GATES: dict[GateKind, tuple[Status, Status, Status]] = {
    GateKind.SPEC: (Status.SPECIFICATION_REVIEW, Status.READY_PLANNING, Status.READY_REFINEMENT),
    GateKind.PLAN: (Status.PLAN_REVIEW, Status.READY_DEVELOPMENT, Status.READY_PLANNING),
}


class IntakeEvaluator:
    def __init__(self, cfg: Config, github: GitHubPort | None) -> None:
        self.cfg = cfg
        self.github = github
        self.ids = {s: sid for s, sid in cfg.workflow.statuses.items()}
        self.by_id = cfg.status_by_id()
        self.approvers = cfg.approvals.approvers()
        self.humans = self.approvers | {cfg.identity.developer_jira_account_id}

    # ------------------------------------------------------------------ helpers
    def _block(
        self,
        stage: Stage,
        reason: str,
        action: str,
        *,
        kind: str = "invalid_input",
        resume: Stage | None = None,
        entry: StatusChange | None = None,
        gate_token: str | None = None,
        requirement: Requirement | None = None,
    ) -> Intake:
        return Intake(
            IntakeKind.BLOCK,
            stage,
            requirement,
            reason,
            action,
            entry,
            resume_stage=resume or stage,
            blocker_kind=kind,
            gate_token=gate_token,
        )

    def _wait(
        self,
        stage: Stage,
        reason: str,
        action: str,
        entry: StatusChange | None,
        requirement: Requirement | None = None,
    ) -> Intake:
        return Intake(IntakeKind.WAIT, stage, requirement, reason, action, entry)

    def _gate_eval(
        self,
        ctx: TicketContext,
        gate: GateRecord,
        entry: StatusChange,
        review: Status,
        approve_to: Status,
        change_to: Status | None,
    ) -> GateEval:
        return evaluate_human_gate(
            gate,
            entry=entry,
            comments=ctx.comments,
            review_status_id=self.ids[review],
            approve_status_id=self.ids[approve_to],
            change_status_id=self.ids[change_to] if change_to else None,
            approvers=self.approvers,
        )

    def catch_up_gates(self, ctx: TicketContext) -> SharedExecutionRecord:
        """Record approvals the coordinator did not see as they happened.

        A gate is normally decided when the coordinator finds the ticket in the ready status the
        approval led to. If a human moved the ticket on before the next poll (for example by
        dragging it on the board), that moment is missed. The decision is still in Jira's
        history, so validate it there exactly as it would have been validated live: approval
        transition out of the review status by an approver, after the gate was published.
        """
        rec = ctx.record
        for kind, (review, approve_to, change_to) in _JIRA_GATES.items():
            gate = current_gate(rec.gates, kind)
            if gate is None or gate.state is not GateState.PENDING:
                continue
            for entry in ctx.changes:
                if entry.from_id != self.ids[review] or entry.to_id != self.ids[approve_to]:
                    continue
                if entry.created < gate.published_at:
                    continue
                ev = self._gate_eval(ctx, gate, entry, review, approve_to, change_to)
                if ev.outcome is GateOutcome.APPROVED:
                    rec = _with_gate(rec, _decided(gate, ev, GateState.APPROVED))
                    break
        return rec

    def prerequisites(self, stage: Stage, rec: SharedExecutionRecord) -> str | None:
        need: list[GateKind] = []
        if stage in (
            Stage.PLANNING,
            Stage.DEVELOPMENT,
            Stage.VERIFICATION,
        ):
            need.append(GateKind.SPEC)
        if stage in (
            Stage.DEVELOPMENT,
            Stage.VERIFICATION,
        ):
            need.append(GateKind.PLAN)
        missing = [k.value for k in need if approved_gate(rec.gates, k) is None]
        if missing:
            return f"no current approved {', '.join(missing)} decision"
        if stage is Stage.VERIFICATION and not rec.candidate_sha:
            return "no implementation candidate is recorded"
        return None

    # ------------------------------------------------------------------ main
    async def evaluate(self, ctx: TicketContext, stage: Stage) -> Intake:
        sd = STAGES[stage]
        caught = self.catch_up_gates(ctx)
        if caught is not ctx.record:
            ctx = replace(ctx, record=caught)
        rec = ctx.record
        entry = ctx.latest_entry(self.ids[sd.ready])
        if entry is None:
            return self._block(
                stage,
                f"ticket is in {sd.ready.value} without a recorded transition",
                "Move the ticket using the workflow actions.",
            )
        src = self.by_id.get(entry.from_id)
        if src is None:
            return self._block(
                stage,
                f"arrived from unmapped status {entry.from_name!r}",
                "Check the workflow mapping with `delivery workflow inspect`.",
                entry=entry,
            )

        if stage is Stage.RESOLUTION:
            result = self._resolution_request(ctx, src, entry)
        elif src in PAUSED_STATUSES:
            result = await self._resume(ctx, stage, src, entry)
        else:
            result = await self._route(ctx, stage, src, entry)
        if result.kind is IntakeKind.READY:
            problem = self.prerequisites(stage, result.record or rec)
            if problem:
                return self._block(
                    stage,
                    problem,
                    "Return the ticket through the review gates.",
                    kind="missing_prerequisite",
                    entry=entry,
                )
        return result

    async def _resume(self, ctx: TicketContext, stage: Stage, src: Status, entry: StatusChange) -> Intake:
        rec = ctx.record
        pause = rec.pause
        if pause is None:
            return self._block(
                stage,
                f"the ticket came back from {STATUS_NAMES[src]} through a resume action, but the "
                "coordinator never paused it there (it was probably moved by hand)",
                f"Choose Resume {stage.value.replace('_', ' ')} to continue. Ticket moves are made "
                "by the coordinator; humans approve, answer and resume.",
                kind="moved_by_hand",
                entry=entry,
            )
        if resume_route(src, STAGES[stage].ready, pause.resume_stage) is None:
            return self._block(
                stage,
                f"wrong resume action: the ticket paused in {pause.resume_stage.value}, not {stage.value}",
                f"Use the {pause.resume_stage.value} resume action from {src.value}.",
                kind="wrong_resume",
                resume=pause.resume_stage,
                entry=entry,
            )
        if entry.author_account_id not in self.humans:
            return self._block(
                stage,
                "resume performed by an account that is not the assignee or an approver",
                "The assignee or an approver must resume.",
                entry=entry,
            )
        cleared = rec.model_copy(update={"pause": None})
        if src is Status.NEEDS_CLARIFICATION:
            if not pause.round_token or pause.published_at is None:
                return self._block(
                    stage,
                    "no clarification round recorded",
                    "Contact the delivery lead.",
                    entry=entry,
                )
            # Whatever people wrote since the questions were posted, in their own words: Claude
            # matches it to the questions (``Q2: ...`` lines keep their question). With nothing
            # written, the stage resumes anyway and asks again only if it still cannot proceed.
            written = said(ctx.comments, since=pause.published_at, authors=self.humans)
            answers = items(written, named="Q", free="A")
            return Intake(
                IntakeKind.READY,
                stage,
                Requirement.CLARIFICATION_ANSWERS,
                f"answers for {pause.round_token}: "
                + (", ".join(sorted(answers)) if answers else "none written (Claude asks again if needed)"),
                entry=entry,
                selected=written,
                round_token=pause.round_token,
                feedback_items=answers,
                record=cleared,
            )
        # Blocked -> resume
        if pause.blocker_kind == "invalid_decision" and pause.gate_token:
            return await self._redecide(ctx, stage, entry, pause.gate_token, cleared)
        return Intake(
            IntakeKind.READY,
            stage,
            Requirement.BLOCKER_RESOLVED,
            f"resumed after blocker: {pause.reason[:200]}",
            entry=entry,
            record=cleared,
        )

    async def _redecide(
        self,
        ctx: TicketContext,
        stage: Stage,
        entry: StatusChange,
        token: str,
        rec: SharedExecutionRecord,
    ) -> Intake:
        """After an invalid decision blocked the stage, an approver's Resume is the decision."""
        gate = next((g for g in rec.gates if g.token == token), None)
        if gate is None or gate.state is GateState.SUPERSEDED:
            return self._block(
                stage,
                f"gate {token} is no longer current",
                "Review the current revision.",
                entry=entry,
            )
        if gate.kind not in (GateKind.SPEC, GateKind.PLAN, GateKind.CODE, GateKind.ACCEPT):
            return self._block(stage, f"cannot re-decide {token}", "Contact the delivery lead.", entry=entry)
        ev = self._gate_eval(ctx, gate, entry, Status.BLOCKED, STAGES[stage].ready, None)
        if ev.outcome is not GateOutcome.APPROVED:
            return self._block(
                stage,
                ev.reason,
                ev.next_action or "An approver must resume.",
                kind="invalid_decision",
                entry=entry,
                gate_token=token,
            )
        if gate.kind is GateKind.CODE:
            ok, why = await self._code_evidence(rec)
            if not ok:
                return self._block(
                    stage,
                    why,
                    "Obtain the GitHub review/CI for the current head.",
                    kind="invalid_decision",
                    entry=entry,
                    gate_token=token,
                )
        rec = _with_gate(rec, _decided(gate, ev, GateState.APPROVED))
        return Intake(
            IntakeKind.READY,
            stage,
            Requirement.BLOCKER_RESOLVED,
            ev.reason,
            entry=entry,
            record=rec,
        )

    def _resolution_request(self, ctx: TicketContext, src: Status, entry: StatusChange) -> Intake:
        """A person asked for the blocker to be resolved with Claude (Blocked -> Ready for
        resolution). The ticket goes back to the stage that blocked, so that stage must be known."""
        stage = Stage.RESOLUTION
        rec = ctx.record
        if src is not Status.BLOCKED:
            return self._block(
                stage,
                f"arrived from {STATUS_NAMES[src]}, not Blocked",
                "Request resolution is only offered on a Blocked ticket.",
                entry=entry,
            )
        if entry.author_account_id not in self.humans:
            return self._wait(
                stage,
                "resolution requested by an account that is not the assignee or an approver",
                "The assignee or an approver must request it.",
                entry,
                Requirement.RESOLUTION_REQUEST,
            )
        if rec.pause is not None and rec.pause.blocker_kind == "invalid_decision":
            return self._wait(
                stage,
                "the ticket is Blocked because an approval or decision was rejected",
                "An approver chooses Resume to decide again; a resolution session cannot change a "
                "human decision.",
                entry,
                Requirement.RESOLUTION_REQUEST,
            )
        resume = rec.pause.resume_stage if rec.pause is not None else None
        if resume is None and ctx.issue.view.resume_stage in {s.value for s in Stage}:
            resume = Stage(ctx.issue.view.resume_stage)
        if resume is None or resume is Stage.RESOLUTION:
            return self._wait(
                stage,
                "the coordinator did not record which stage blocked, so there is nowhere to return to",
                "Move the ticket back to Blocked and choose the Resume action for its stage, or Cancel.",
                entry,
                Requirement.RESOLUTION_REQUEST,
            )
        pause = rec.pause
        return Intake(
            IntakeKind.READY,
            stage,
            Requirement.RESOLUTION_REQUEST,
            "resolution requested" + (f" for: {pause.reason[:200]}" if pause and pause.reason else ""),
            entry=entry,
            record=rec,
            resume_stage=resume,
            blocker_kind=pause.blocker_kind if pause else "",
        )

    async def _req_resolved(self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange) -> Intake:
        """The ticket came back from a resolution session: the stage that blocked starts again."""
        return Intake(
            IntakeKind.READY,
            stage,
            reason="resumed after the blocker was resolved with the developer",
            record=ctx.record.model_copy(update={"pause": None}),
        )

    async def _route(self, ctx: TicketContext, stage: Stage, src: Status, entry: StatusChange) -> Intake:
        sd = STAGES[stage]
        routes = human_route(src, sd.ready)
        coordinator = [
            r for r in ROUTES if r.actor is Actor.COORDINATOR and r.source is src and r.target is sd.ready
        ]
        if not routes and not coordinator:
            return self._block(
                stage,
                f"{src.value} -> {sd.ready.value} is not a permitted route",
                "Use the workflow actions from the review statuses.",
                entry=entry,
            )
        req = (routes or coordinator)[0].requires
        handler = getattr(self, f"_req_{req.value}", None)
        if handler is None:
            return self._block(
                stage,
                f"unsupported requirement {req.value}",
                "Contact the delivery lead.",
                entry=entry,
            )
        intake: Intake = await handler(ctx, stage, src, entry)
        intake.requirement = req
        intake.entry = entry
        return intake

    # ------------------------------------------------------------------ requirements
    async def _req_brief(self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange) -> Intake:
        if not brief_is_usable(ctx.issue):
            return self._wait(
                stage,
                "the brief is empty or too short",
                "Add the problem, scope, exclusions and acceptance criteria to the "
                "description. The coordinator re-checks automatically.",
                e,
            )
        return Intake(IntakeKind.READY, stage, reason="brief submitted", record=ctx.record)

    async def _gate_route(
        self,
        ctx: TicketContext,
        stage: Stage,
        e: StatusChange,
        kind: GateKind,
        expect: GateOutcome,
    ) -> Intake:
        rec = ctx.record
        gate = current_gate(rec.gates, kind)
        if gate is None:
            return self._block(
                stage,
                f"no current {kind.value} gate is recorded",
                "Return the ticket through the review gate.",
                entry=e,
            )
        ev = self._gate_eval(ctx, gate, e, *_JIRA_GATES[kind])
        if ev.outcome is not expect:
            return self._block(
                stage,
                ev.reason,
                ev.next_action or "Resolve the decision in Jira.",
                kind="invalid_decision",
                entry=e,
                gate_token=gate.token,
                resume=stage,
            )
        state = GateState.APPROVED if expect is GateOutcome.APPROVED else GateState.CHANGES_REQUESTED
        rec = _with_gate(rec, _decided(gate, ev, state))
        return Intake(
            IntakeKind.READY,
            stage,
            reason=ev.reason,
            record=rec,
            selected=list(ev.comments),
            feedback_token=gate.token,
            feedback_items=dict(ev.items),
        )

    async def _req_spec_changes(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(ctx, stage, e, GateKind.SPEC, GateOutcome.CHANGES_REQUESTED)

    async def _req_spec_approval(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(ctx, stage, e, GateKind.SPEC, GateOutcome.APPROVED)

    async def _req_plan_changes(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(ctx, stage, e, GateKind.PLAN, GateOutcome.CHANGES_REQUESTED)

    async def _req_plan_approval(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(ctx, stage, e, GateKind.PLAN, GateOutcome.APPROVED)

    async def _req_scope_revision(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        rec = ctx.record
        spec = current_gate(rec.gates, GateKind.SPEC)
        if spec is None:
            return self._block(
                stage, "no specification gate to revise", "Contact the delivery lead.", entry=e
            )
        if e.author_account_id not in self.approvers:
            return self._block(
                stage,
                "scope revision must be decided by an approver",
                "An approver must use Revise scope.",
                entry=e,
            )
        since = ctx.latest_entry(self.ids[Status.CHANGES_REQUESTED])
        written = said(ctx.comments, since=since.created if since else None, authors=self.approvers)
        fb = items(written, named="F", free="F")
        return Intake(
            IntakeKind.READY,
            stage,
            reason=f"scope revision of {spec.token}"
            + ("" if fb else " (no comment: Claude asks what to change)"),
            record=rec,
            selected=written,
            feedback_token=spec.token,
            feedback_items=fb,
        )

    async def _req_implementation_changes(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        rec = ctx.record
        if e.author_account_id not in self.humans:
            return self._block(
                stage,
                "implementation changes submitted by an unauthorised account",
                "The assignee or an approver must submit changes.",
                entry=e,
            )
        into = [c for c in ctx.changes if c.to_id == self.ids[Status.CHANGES_REQUESTED]]
        if not into:
            return self._block(
                stage,
                "no record of how the ticket reached Changes requested",
                "Contact the delivery lead.",
                entry=e,
            )
        origin = self.by_id.get(into[-1].from_id)
        found: dict[str, str] = {}
        token: str | None = None
        since = into[-1].created
        if origin is Status.VERIFYING:
            for f in rec.pending_feedback:
                found[str(f.get("id"))] = str(f.get("description"))
            # The failure comment names the candidate's code token even before any code gate
            # exists (a first candidate that failed verification).
            token = make_gate_token(ctx.key, GateKind.CODE, rec.candidate_number)
        elif origin in (Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW):
            kind = GateKind.CODE if origin is Status.CODE_REVIEW else GateKind.ACCEPT
            gate = current_gate(rec.gates, kind)
            if gate is None:
                return self._block(
                    stage, f"no current {kind.value} gate", "Contact the delivery lead.", entry=e
                )
            approve_to = Status.ACCEPTANCE_REVIEW if kind is GateKind.CODE else Status.READY_RELEASE
            ev = self._gate_eval(ctx, gate, into[-1], origin, approve_to, Status.CHANGES_REQUESTED)
            if ev.outcome is not GateOutcome.CHANGES_REQUESTED:
                return self._block(
                    stage,
                    ev.reason,
                    ev.next_action or "Resolve the change request.",
                    kind="invalid_decision",
                    entry=e,
                )
            token, since = gate.token, gate.published_at
            rec = _with_gate(rec, _decided(gate, ev, GateState.CHANGES_REQUESTED))
        if self.github is not None and rec.pr_number:
            # Unresolved review conversations on the PR since this candidate was published.
            found.update(
                await review_items(self.github, rec.pr_number, self._candidate_published(ctx, into[-1]))
            )
        # What people wrote since the change was asked for (or the candidate failed), in their own
        # words: each comment is an item, and one can narrow the work ("only F2") or name a
        # deviation to change back ("D1: follow the specification"). With nothing written and
        # nothing found, development starts anyway and asks what to change.
        written = said(ctx.comments, since=since, authors=self.humans)
        found.update(items(written, named="FD", free="F", taken=found))
        found = self._deviation_items(rec, found)
        return Intake(
            IntakeKind.READY,
            stage,
            reason=f"{len(found)} change items" if found else "no change items: Claude asks what to change",
            record=rec,
            selected=written,
            feedback_token=token,
            feedback_items=found,
        )

    def _candidate_published(self, ctx: TicketContext, before: StatusChange) -> datetime | None:
        """When the current candidate went in for verification (before ``before``)."""
        found = [
            c
            for c in ctx.changes
            if c.to_id == self.ids[Status.READY_VERIFICATION] and c.created <= before.created
        ]
        return found[-1].created if found else None

    def _deviation_items(self, rec: SharedExecutionRecord, found: dict[str, str]) -> dict[str, str]:
        """Deviations named in the change request (`D2: <note>`) become work items that change
        the code back to the specification. Deviations nobody named are left as they are."""
        known = {d.id: d for d in deviations.open_deviations(rec)}
        out = {k: v for k, v in found.items() if not k.startswith("D")}
        for key, note in [(k, v) for k, v in found.items() if k.startswith("D")]:
            did = key.split("@")[0]
            if did in known and did not in out:
                out[did] = deviations.change_back(known[did], note)
        return out

    async def _req_stage_success(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        rec = ctx.record
        if not rec.candidate_sha:
            return self._block(
                stage,
                "no implementation candidate is recorded",
                "Return the ticket to development.",
                entry=e,
            )
        reason = f"candidate {rec.candidate_sha[:12]}"
        if src in FOLLOW_UP_SOURCES and rec.current_stage is Stage.VERIFICATION:
            # Moved back by hand with no follow-up pushed: the same code is verified again.
            reason = (
                f"candidate c{rec.candidate_number} {rec.candidate_sha[:12]} again, unchanged since its "
                "last verification (Submit follow-up changes re-verifies the same code; to change "
                "the code use Submit implementation changes)"
            )
        return Intake(IntakeKind.READY, stage, reason=reason, record=rec)

    async def _req_plan_with_specification(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        """Fast track: planning published the plan written with the specification as approved."""
        rec = ctx.record
        plan = approved_gate(rec.gates, GateKind.PLAN)
        spec = approved_gate(rec.gates, GateKind.SPEC)
        if plan is None or spec is None:
            return self._block(
                stage,
                "the fast-track plan is not recorded as approved with the specification",
                "Return the ticket through Plan review.",
                entry=e,
            )
        return Intake(
            IntakeKind.READY,
            stage,
            reason=f"plan v{plan.revision:03d} approved with specification v{spec.revision:03d} (fast track)",
            record=rec,
        )

    async def _code_evidence(self, rec: SharedExecutionRecord, *, merged: bool = False) -> tuple[bool, str]:
        """Independent review and passing CI on the verified candidate's head. ``merged``: the PR
        is already merged, so a head that is no longer the candidate is not a reason to wait; what
        was merged is reported when the release is checked (delivery.release)."""
        if self.github is None or rec.pr_number is None or not rec.candidate_sha:
            return False, "no pull request or candidate recorded"
        pr = await self.github.get_pr(rec.pr_number)
        if pr.head_sha != rec.candidate_sha:
            if merged:
                return True, "the merged head differs from the candidate"
            return False, (
                f"PR head {pr.head_sha[:12]} is not the verified candidate "
                f"{rec.candidate_sha[:12]}; a changed candidate needs fresh verification"
            )
        reviews = await self.github.reviews(pr.number)
        rv = evaluate_reviews(
            pr,
            reviews,
            allowed_logins=self.cfg.approvals.github_logins,
            require_independent=self.cfg.approvals.require_independent_github_review,
        )
        if not rv.ok:
            return False, rv.reason
        ci = evaluate_ci(
            pr.head_sha,
            required_names=self.cfg.checks.ci.required_names,
            expected_producer=self.cfg.checks.ci.expected_producer,
            check_runs=await self.github.check_runs(pr.head_sha),
            statuses=await self.github.statuses(pr.head_sha),
            allow_neutral=self.cfg.checks.ci.allow_neutral,
            allow_skipped=self.cfg.checks.ci.allow_skipped,
        )
        if not ci.ok:
            return False, "CI for the current head is not passing: " + "; ".join(ci.problems[:5])
        return True, rv.reason

    async def _decisions_before_release(
        self, ctx: TicketContext, entry: StatusChange
    ) -> tuple[SharedExecutionRecord | None, str, str]:
        """Code approved (with an independent review and passing CI) and the delivery accepted, as
        the Jira history and GitHub show them now. The record with both gates decided, or None
        with the reason and what to do. ``entry`` is the Accept delivery move into Ready for
        release. The PR is merged by now, so a head that is no longer the verified candidate is not
        a reason to wait: delivery.release reports what was merged."""
        rec = ctx.record
        code = current_gate(rec.gates, GateKind.CODE)
        accept = current_gate(rec.gates, GateKind.ACCEPT)
        if code is None or accept is None:
            return None, "code/acceptance gates are not recorded", "Return the ticket to verification."
        if code.state is not GateState.APPROVED:
            code_entry = ctx.latest_change(
                self.ids[Status.CODE_REVIEW], self.ids[Status.ACCEPTANCE_REVIEW], code.published_at
            )
            if code_entry is None:
                return None, "no Approve code transition after the code gate", "Return through Code review."
            ev = self._gate_eval(
                ctx,
                code,
                code_entry,
                Status.CODE_REVIEW,
                Status.ACCEPTANCE_REVIEW,
                Status.CHANGES_REQUESTED,
            )
            if ev.outcome is not GateOutcome.APPROVED:
                return (
                    None,
                    f"code approval invalid: {ev.reason}",
                    ev.next_action or "Resolve the code decision.",
                )
            ok, why = await self._code_evidence(rec, merged=True)
            if not ok:
                return (
                    None,
                    f"code gate evidence: {why}",
                    "Obtain an independent GitHub review and passing CI on the current head.",
                )
            rec = _with_gate(rec, _decided(code, ev, GateState.APPROVED))
        if accept.state is not GateState.APPROVED:
            ev2 = self._gate_eval(
                ctx,
                accept,
                entry,
                Status.ACCEPTANCE_REVIEW,
                Status.READY_RELEASE,
                Status.CHANGES_REQUESTED,
            )
            if ev2.outcome is not GateOutcome.APPROVED:
                return None, ev2.reason, ev2.next_action or "Resolve the acceptance decision."
            rec = _with_gate(rec, _decided(accept, ev2, GateState.APPROVED))
        return rec, "", ""

    # ------------------------------------------------------------------ release
    async def merged_release(self, ctx: TicketContext) -> ReleaseIntake:
        """A ticket in Ready for release is complete once its PR is merged.

        The human merge is the release; the coordinator only reads it from GitHub. The decisions
        behind the move into Ready for release (Approve code, Accept delivery) are validated only
        now, because Accept delivery leads here directly and no stage follows it. A problem with a
        decision is reported and re-checked on every poll (fix it in Jira or GitHub and the next
        poll goes on); it never blocks the ticket.
        """
        rec = ctx.record
        if self.github is None or rec.pr_number is None:
            return ReleaseIntake(
                False,
                "no pull request is recorded, so the release cannot be read from GitHub",
                "Cancel the ticket if it was moved here by hand.",
            )
        pr = await self.github.get_pr(rec.pr_number)
        if not pr.merged or not pr.merge_commit_sha:
            return ReleaseIntake(
                False,
                f"PR #{pr.number} is not merged yet",
                f"Merge PR #{pr.number} on GitHub; the coordinator moves the ticket to Done when it sees "
                "the merge.",
            )
        entry = ctx.latest_entry(self.ids[Status.READY_RELEASE])
        src = self.by_id.get(entry.from_id) if entry else None
        if entry is None or src is not Status.ACCEPTANCE_REVIEW:
            came = f"from {STATUS_NAMES[src]}" if src else "without a recorded transition"
            return ReleaseIntake(
                False,
                f"the ticket was moved to Ready for release {came}, not by Accept delivery",
                "Cancel the ticket, or return it through acceptance.",
            )
        decided, reason, next_action = await self._decisions_before_release(ctx, entry)
        if decided is None:
            return ReleaseIntake(False, reason, next_action)
        missing = [
            k.value
            for k in (GateKind.SPEC, GateKind.PLAN, GateKind.CODE, GateKind.ACCEPT)
            if approved_gate(decided.gates, k) is None
        ]
        if missing:
            return ReleaseIntake(
                False,
                f"no current approved {', '.join(missing)} decision",
                "Return the ticket through the review gates.",
            )
        return ReleaseIntake(
            True,
            f"PR #{pr.number} merged as {pr.merge_commit_sha[:12]}",
            record=decided,
            pr_number=pr.number,
            merge_commit=pr.merge_commit_sha,
            merged_by=pr.merged_by,
        )
