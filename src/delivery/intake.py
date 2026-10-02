"""Intake: validate how a ticket reached its ready status before any work starts.

The coordinator reads the status change that brought the ticket into its current ready
status, looks up the permitted route, and validates the input that route requires
(brief, answers for the right round, feedback bound to the reviewed revision, a human
decision for the current gate, a recorded release). Outcomes:

* READY: start the stage with the selected inputs.
* WAIT: a human input is missing (for example the answer comment); explain once and
  re-check on every poll. Nothing starts.
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

from delivery.config import Config
from delivery.feedback import (
    DecisionKind,
    collect_answers,
    collect_feedback,
    decisions,
)
from delivery.gates import (
    GateEval,
    GateOutcome,
    approved_gate,
    current_gate,
    evaluate_ci,
    evaluate_human_gate,
    evaluate_reviews,
)
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
from delivery.workflow import (
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
    release_record: dict[str, Any] | None = None

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
            "release_record": self.release_record,
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
            data.get("release_record"),
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
            "release": self.release_record,
        }


class CodeEvidence(Protocol):
    async def __call__(self, record: SharedExecutionRecord) -> tuple[bool, str]: ...


def brief_text(issue: JiraIssue) -> str:
    return f"{issue.view.summary}\n\n{issue.description_text}".strip()


def brief_is_usable(issue: JiraIssue) -> bool:
    desc = re.sub(r"\s+", " ", issue.description_text).strip()
    return bool(issue.view.summary.strip()) and len(desc) >= 20


def _with_gate(rec: SharedExecutionRecord, gate: GateRecord) -> SharedExecutionRecord:
    gates = [gate if g.token == gate.token else g for g in rec.gates]
    return rec.model_copy(update={"gates": gates})


def _decided(gate: GateRecord, ev: GateEval, state: GateState) -> GateRecord:
    return gate.model_copy(update={"state": state, "evidence": ev.evidence, "decided_at": utcnow()})


# Jira-only review gates: (review status, approve target, change target, approve, change kinds).
# The code gate also needs GitHub evidence, so it is only decided on its own route.
_JIRA_GATES: dict[GateKind, tuple[Status, Status, Status, DecisionKind, set[DecisionKind]]] = {
    GateKind.SPEC: (
        Status.SPECIFICATION_REVIEW,
        Status.READY_PLANNING,
        Status.READY_REFINEMENT,
        DecisionKind.APPROVE_SPEC,
        {DecisionKind.CHANGE_SPEC},
    ),
    GateKind.PLAN: (
        Status.PLAN_REVIEW,
        Status.READY_DEVELOPMENT,
        Status.READY_PLANNING,
        DecisionKind.APPROVE_PLAN,
        {DecisionKind.CHANGE_PLAN},
    ),
    GateKind.RELEASE: (
        Status.RELEASE_REVIEW,
        Status.READY_RELEASE,
        Status.READY_RELEASE_PREPARATION,
        DecisionKind.APPROVE_RELEASE,
        {DecisionKind.CHANGE_RELEASE},
    ),
}


class IntakeEvaluator:
    def __init__(self, cfg: Config, github: GitHubPort | None) -> None:
        self.cfg = cfg
        self.github = github
        self.ids = {s: sid for s, sid in cfg.workflow.statuses.items()}
        self.by_id = cfg.status_by_id()
        self.approvers = set(cfg.approvals.jira_account_ids)
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
        approve: DecisionKind,
        change: set[DecisionKind],
        approvers: set[str] | None = None,
    ) -> GateEval:
        return evaluate_human_gate(
            gate,
            entry=entry,
            comments=ctx.comments,
            review_status_id=self.ids[review],
            approve_status_id=self.ids[approve_to],
            change_status_id=self.ids[change_to] if change_to else None,
            approve_kind=approve,
            change_kinds=change,
            approvers=approvers or self.approvers,
        )

    def catch_up_gates(self, ctx: TicketContext) -> SharedExecutionRecord:
        """Record approvals the coordinator did not see as they happened.

        A gate is normally decided when the coordinator finds the ticket in the ready status the
        approval led to. If a human moved the ticket on before the next poll (for example by
        dragging it on the board), that moment is missed. The decision is still in Jira's
        history, so validate it there exactly as it would have been validated live: approval
        transition out of the review status by an approver, after the gate was published, with
        the current token in a comment.
        """
        rec = ctx.record
        for kind, (review, approve_to, change_to, approve, change) in _JIRA_GATES.items():
            gate = current_gate(rec.gates, kind)
            if gate is None or gate.state is not GateState.PENDING:
                continue
            for entry in ctx.changes:
                if entry.from_id != self.ids[review] or entry.to_id != self.ids[approve_to]:
                    continue
                if entry.created < gate.published_at:
                    continue
                ev = self._gate_eval(ctx, gate, entry, review, approve_to, change_to, approve, change)
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
            Stage.RELEASE_PREPARATION,
            Stage.RELEASE_VERIFICATION,
        ):
            need.append(GateKind.SPEC)
        if stage in (
            Stage.DEVELOPMENT,
            Stage.VERIFICATION,
            Stage.RELEASE_PREPARATION,
            Stage.RELEASE_VERIFICATION,
        ):
            need.append(GateKind.PLAN)
        if stage in (Stage.RELEASE_PREPARATION, Stage.RELEASE_VERIFICATION):
            need += [GateKind.CODE, GateKind.ACCEPT]
        if stage is Stage.RELEASE_VERIFICATION:
            need.append(GateKind.RELEASE)
        missing = [k.value for k in need if approved_gate(rec.gates, k) is None]
        if missing:
            return f"no current approved {', '.join(missing)} decision"
        if stage in (Stage.VERIFICATION, Stage.RELEASE_PREPARATION) and not rec.candidate_sha:
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

        if src in PAUSED_STATUSES:
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
            answers = collect_answers(
                ctx.comments,
                token=pause.round_token,
                question_ids=pause.question_ids,
                since=pause.published_at,
                allowed_authors=self.humans,
                submitted_at=entry.created,
            )
            usable = [cd for cd in answers.comments if cd.comment.id not in answers.edited_after_submit]
            if not usable or not answers.answers:
                hint = "; ".join(answers.problems) or f"no `ANSWERS {pause.round_token}` comment found"
                return self._wait(
                    stage,
                    hint,
                    f"Comment using the `ANSWERS {pause.round_token}` template (Q1:, Q2:...). "
                    "The coordinator picks it up on its next poll.",
                    entry,
                    Requirement.CLARIFICATION_ANSWERS,
                )
            return Intake(
                IntakeKind.READY,
                stage,
                Requirement.CLARIFICATION_ANSWERS,
                f"answers for {pause.round_token}: {', '.join(sorted(answers.answers))}"
                + (f"; unanswered {', '.join(answers.missing)}" if answers.missing else ""),
                entry=entry,
                selected=[cd.comment for cd in usable],
                round_token=pause.round_token,
                feedback_items=answers.answers,
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
        approve = {
            GateKind.SPEC: DecisionKind.APPROVE_SPEC,
            GateKind.PLAN: DecisionKind.APPROVE_PLAN,
            GateKind.CODE: DecisionKind.APPROVE_CODE,
            GateKind.ACCEPT: DecisionKind.ACCEPT_DELIVERY,
            GateKind.RELEASE: DecisionKind.APPROVE_RELEASE,
        }.get(gate.kind)
        if approve is None:
            return self._block(stage, f"cannot re-decide {token}", "Contact the delivery lead.", entry=entry)
        ev = self._gate_eval(ctx, gate, entry, Status.BLOCKED, STAGES[stage].ready, None, approve, set())
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
        # The original human move may have carried a second decision (code approval is
        # followed by acceptance; release approval by the release record). Re-validate it
        # against its original transition now that the first decision is settled.
        follow = {
            (Stage.RELEASE_PREPARATION, GateKind.CODE): (
                Status.ACCEPTANCE_REVIEW,
                Status.READY_RELEASE_PREPARATION,
                self._req_acceptance,
            ),
            (Stage.RELEASE_VERIFICATION, GateKind.RELEASE): (
                Status.READY_RELEASE,
                Status.READY_RELEASE_VERIFICATION,
                self._req_release_record,
            ),
        }.get((stage, gate.kind))
        if follow:
            src, dst, handler = follow
            original = ctx.latest_change(self.ids[src], self.ids[dst])
            if original is not None:
                sub: Intake = await handler(
                    TicketContext(ctx.issue, ctx.status, ctx.comments, ctx.changes, rec), stage, src, original
                )
                sub.entry = entry
                if sub.kind is IntakeKind.READY:
                    sub.requirement = Requirement.BLOCKER_RESOLVED
                return sub
        return Intake(
            IntakeKind.READY,
            stage,
            Requirement.BLOCKER_RESOLVED,
            ev.reason,
            entry=entry,
            record=rec,
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
        review: Status,
        approve_to: Status,
        change_to: Status | None,
        approve: DecisionKind,
        change: set[DecisionKind],
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
        ev = self._gate_eval(ctx, gate, e, review, approve_to, change_to, approve, change)
        if ev.outcome is GateOutcome.WAITING:
            return self._wait(stage, ev.reason, ev.next_action, e)
        if ev.outcome in (GateOutcome.REJECTED, GateOutcome.CONFLICT) or ev.outcome is not expect:
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
        fb = ev.feedback
        return Intake(
            IntakeKind.READY,
            stage,
            reason=ev.reason,
            record=rec,
            selected=[cd.comment for cd in (fb.comments if fb else ev.decisions)],
            feedback_token=gate.token,
            feedback_items=dict(fb.items) if fb else {},
        )

    async def _req_spec_changes(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(
            ctx,
            stage,
            e,
            GateKind.SPEC,
            Status.SPECIFICATION_REVIEW,
            Status.READY_PLANNING,
            Status.READY_REFINEMENT,
            DecisionKind.APPROVE_SPEC,
            {DecisionKind.CHANGE_SPEC},
            GateOutcome.CHANGES_REQUESTED,
        )

    async def _req_spec_approval(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(
            ctx,
            stage,
            e,
            GateKind.SPEC,
            Status.SPECIFICATION_REVIEW,
            Status.READY_PLANNING,
            Status.READY_REFINEMENT,
            DecisionKind.APPROVE_SPEC,
            {DecisionKind.CHANGE_SPEC},
            GateOutcome.APPROVED,
        )

    async def _req_plan_changes(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(
            ctx,
            stage,
            e,
            GateKind.PLAN,
            Status.PLAN_REVIEW,
            Status.READY_DEVELOPMENT,
            Status.READY_PLANNING,
            DecisionKind.APPROVE_PLAN,
            {DecisionKind.CHANGE_PLAN},
            GateOutcome.CHANGES_REQUESTED,
        )

    async def _req_plan_approval(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(
            ctx,
            stage,
            e,
            GateKind.PLAN,
            Status.PLAN_REVIEW,
            Status.READY_DEVELOPMENT,
            Status.READY_PLANNING,
            DecisionKind.APPROVE_PLAN,
            {DecisionKind.CHANGE_PLAN},
            GateOutcome.APPROVED,
        )

    async def _req_release_changes(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        return await self._gate_route(
            ctx,
            stage,
            e,
            GateKind.RELEASE,
            Status.RELEASE_REVIEW,
            Status.READY_RELEASE,
            Status.READY_RELEASE_PREPARATION,
            DecisionKind.APPROVE_RELEASE,
            {DecisionKind.CHANGE_RELEASE},
            GateOutcome.CHANGES_REQUESTED,
        )

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
        fb = collect_feedback(
            ctx.comments,
            token=spec.token,
            kinds={DecisionKind.REVISE_SCOPE},
            since=since.created if since else None,
            allowed_authors=self.approvers,
        )
        if not fb.items or fb.problems:
            return self._wait(
                stage,
                "; ".join(fb.problems) or f"no `REVISE SCOPE {spec.token}` comment found",
                f"Comment `REVISE SCOPE {spec.token}` with numbered F1.. scope changes.",
                e,
            )
        return Intake(
            IntakeKind.READY,
            stage,
            reason=f"scope revision of {spec.token}",
            record=rec,
            selected=[cd.comment for cd in fb.comments],
            feedback_token=spec.token,
            feedback_items=dict(fb.items),
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
        items: dict[str, str] = {}
        selected: list[JiraComment] = []
        token: str | None = None
        if origin is Status.VERIFYING:
            for f in rec.pending_feedback:
                items[str(f.get("id"))] = str(f.get("description"))
            code_gate = current_gate(rec.gates, GateKind.CODE)
            token = code_gate.token if code_gate else None
        elif origin in (Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW):
            kind = GateKind.CODE if origin is Status.CODE_REVIEW else GateKind.ACCEPT
            gate = current_gate(rec.gates, kind)
            if gate is None:
                return self._block(
                    stage, f"no current {kind.value} gate", "Contact the delivery lead.", entry=e
                )
            change = DecisionKind.CHANGE_CODE if kind is GateKind.CODE else DecisionKind.CHANGE_ACCEPTANCE
            approve = DecisionKind.APPROVE_CODE if kind is GateKind.CODE else DecisionKind.ACCEPT_DELIVERY
            approve_to = (
                Status.ACCEPTANCE_REVIEW if kind is GateKind.CODE else Status.READY_RELEASE_PREPARATION
            )
            ev = self._gate_eval(
                ctx, gate, into[-1], origin, approve_to, Status.CHANGES_REQUESTED, approve, {change}
            )
            if ev.outcome is GateOutcome.WAITING:
                return self._wait(stage, ev.reason, ev.next_action, e)
            if ev.outcome is not GateOutcome.CHANGES_REQUESTED or ev.feedback is None:
                return self._block(
                    stage,
                    ev.reason,
                    ev.next_action or "Resolve the change request.",
                    kind="invalid_decision",
                    entry=e,
                )
            items, token = dict(ev.feedback.items), gate.token
            selected = [cd.comment for cd in ev.feedback.comments]
            rec = _with_gate(rec, _decided(gate, ev, GateState.CHANGES_REQUESTED))
        if token:
            sub = decisions(
                ctx.comments,
                token=token,
                kinds={DecisionKind.SUBMIT_CHANGES},
                since=into[-1].created,
            )
            sub = [cd for cd in sub if cd.comment.author_account_id in self.humans]
            if sub:
                chosen = set(sub[-1].decision.items)
                if chosen:
                    items = {k: v for k, v in items.items() if k.split("@")[0] in chosen}
                selected.append(sub[-1].comment)
        if not items:
            return self._wait(
                stage,
                "no feedback items selected for the implementation changes",
                "Comment the change request with numbered F1.. items.",
                e,
            )
        return Intake(
            IntakeKind.READY,
            stage,
            reason=f"{len(items)} change items",
            record=rec,
            selected=selected,
            feedback_token=token,
            feedback_items=items,
        )

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
        return Intake(IntakeKind.READY, stage, reason=f"candidate {rec.candidate_sha[:12]}", record=rec)

    async def _code_evidence(self, rec: SharedExecutionRecord) -> tuple[bool, str]:
        if self.github is None or rec.pr_number is None or not rec.candidate_sha:
            return False, "no pull request or candidate recorded"
        pr = await self.github.get_pr(rec.pr_number)
        if pr.head_sha != rec.candidate_sha:
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

    async def _req_acceptance(self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange) -> Intake:
        rec = ctx.record
        code = current_gate(rec.gates, GateKind.CODE)
        accept = current_gate(rec.gates, GateKind.ACCEPT)
        if code is None or accept is None:
            return self._block(
                stage,
                "code/acceptance gates are not recorded",
                "Return the ticket to verification.",
                entry=e,
            )
        if code.state is not GateState.APPROVED:
            code_entry = ctx.latest_change(
                self.ids[Status.CODE_REVIEW], self.ids[Status.ACCEPTANCE_REVIEW], code.published_at
            )
            if code_entry is None:
                return self._block(
                    stage,
                    "no Approve code transition after the code gate",
                    "Return through Code review.",
                    entry=e,
                )
            ev = self._gate_eval(
                ctx,
                code,
                code_entry,
                Status.CODE_REVIEW,
                Status.ACCEPTANCE_REVIEW,
                Status.CHANGES_REQUESTED,
                DecisionKind.APPROVE_CODE,
                {DecisionKind.CHANGE_CODE},
            )
            if ev.outcome is not GateOutcome.APPROVED:
                kind = "invalid_decision"
                return self._block(
                    stage,
                    f"code approval invalid: {ev.reason}",
                    ev.next_action or "Resolve the code decision.",
                    kind=kind,
                    entry=e,
                    gate_token=code.token,
                )
            ok, why = await self._code_evidence(rec)
            if not ok:
                return self._block(
                    stage,
                    f"code gate evidence: {why}",
                    "Obtain an independent GitHub review and passing CI on the current "
                    "head, then an approver resumes.",
                    kind="invalid_decision",
                    entry=e,
                    gate_token=code.token,
                )
            evidence = ev.evidence
            rec = _with_gate(rec, _decided(code, ev, GateState.APPROVED))
            _ = evidence
        ev2 = self._gate_eval(
            ctx,
            accept,
            e,
            Status.ACCEPTANCE_REVIEW,
            Status.READY_RELEASE_PREPARATION,
            Status.CHANGES_REQUESTED,
            DecisionKind.ACCEPT_DELIVERY,
            {DecisionKind.CHANGE_ACCEPTANCE},
        )
        if ev2.outcome is GateOutcome.WAITING:
            return self._wait(stage, ev2.reason, ev2.next_action, e)
        if ev2.outcome is not GateOutcome.APPROVED:
            return self._block(
                stage,
                ev2.reason,
                ev2.next_action or "Resolve the acceptance decision.",
                kind="invalid_decision",
                entry=e,
                gate_token=accept.token,
            )
        rec = _with_gate(rec, _decided(accept, ev2, GateState.APPROVED))
        return Intake(
            IntakeKind.READY,
            stage,
            reason="code approved and delivery accepted",
            record=rec,
            selected=[cd.comment for cd in ev2.decisions],
        )

    async def _req_release_record(
        self, ctx: TicketContext, stage: Stage, src: Status, e: StatusChange
    ) -> Intake:
        rec = ctx.record
        rel = current_gate(rec.gates, GateKind.RELEASE)
        if rel is None:
            return self._block(
                stage, "no release gate recorded", "Return through release preparation.", entry=e
            )
        if rel.state is not GateState.APPROVED:
            rel_entry = ctx.latest_change(
                self.ids[Status.RELEASE_REVIEW], self.ids[Status.READY_RELEASE], rel.published_at
            )
            if rel_entry is None:
                return self._block(
                    stage,
                    "no Approve release transition after the release gate",
                    "Return through Release review.",
                    entry=e,
                )
            ev = self._gate_eval(
                ctx,
                rel,
                rel_entry,
                Status.RELEASE_REVIEW,
                Status.READY_RELEASE,
                Status.READY_RELEASE_PREPARATION,
                DecisionKind.APPROVE_RELEASE,
                {DecisionKind.CHANGE_RELEASE},
            )
            if ev.outcome is not GateOutcome.APPROVED:
                return self._block(
                    stage,
                    f"release approval invalid: {ev.reason}",
                    ev.next_action or "Resolve the release decision.",
                    kind="invalid_decision",
                    entry=e,
                    gate_token=rel.token,
                )
            rec = _with_gate(rec, _decided(rel, ev, GateState.APPROVED))
        if e.author_account_id not in self.humans:
            return self._block(
                stage,
                "Record release performed by an unauthorised account",
                "The release owner must record the release.",
                entry=e,
            )
        found = decisions(
            ctx.comments,
            token=rel.token,
            kinds={DecisionKind.RECORD_RELEASE},
            since=rel.published_at,
        )
        found = [cd for cd in found if cd.comment.author_account_id in self.humans]
        if not found:
            return self._wait(
                stage,
                f"no `RECORD RELEASE {rel.token}` comment found",
                "Comment the RECORD RELEASE template with commit and environment.",
                e,
            )
        cd = found[-1]
        if cd.comment.edited:
            return self._block(
                stage, "release record comment was edited", "Add a fresh record comment.", entry=e
            )
        commit = cd.decision.fields.get("commit", "")
        env = cd.decision.fields.get("environment", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or not env:
            return self._wait(
                stage,
                "release record needs a full 40-character commit SHA and environment",
                "Comment RECORD RELEASE with `commit: <sha>` and `environment: <name>`.",
                e,
            )
        if env != self.cfg.release.environment:
            return self._block(
                stage,
                f"recorded environment {env!r} is not {self.cfg.release.environment!r}",
                "Record the release in the configured environment.",
                entry=e,
            )
        pr = cd.decision.fields.get("merged-pr") or cd.decision.fields.get("pr")
        record = {
            "commit": commit,
            "environment": env,
            "merged_pr": pr,
            "comment_id": cd.comment.id,
            "recorded_by": cd.comment.author_account_id,
        }
        return Intake(
            IntakeKind.READY,
            stage,
            reason=f"release {commit[:12]} in {env}",
            record=rec,
            selected=[cd.comment],
            release_record=record,
        )
