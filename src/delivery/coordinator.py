"""Run lifecycle: start, monitor, work, decide, publish, clean up.

One executor instance can drive any number of concurrent runs; each run has its own
journal, worktrees, ports, temporary directory and child process group.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from delivery import comments
from delivery.attachments import ATTACHMENT_STAGES, fetch_attachments
from delivery.claude import HUMAN_ACTION, ClaudeStatus
from delivery.designs import fetch_designs, figma_links
from delivery.git import BranchDiverged, GitError, WorktreeConflict
from delivery.intake import Intake, IntakeKind, TicketContext, brief_text, load_context
from delivery.journal import RunJournal, new_run_id
from delivery.models import Outcome, RunRecord, RunState, digest, utcnow
from delivery.ports import IntegrationError, UncertainResult
from delivery.publication import (
    PublicationError,
    PublicationUncertain,
    Publisher,
    TicketMoved,
)
from delivery.resources import ResourceExhausted
from delivery.runtime import ChildCallback, Decision, Deps, RunContext
from delivery.stages import STRATEGIES, OutputInvalid, StageStrategy, WorkerFailure, notes_for
from delivery.workflow import STAGES, STATUS_NAMES, Stage, Status


class StopRequested(Exception):
    """Operator stop, handover or supervisor shutdown."""

    def __init__(self, reason: str, hold: bool = True) -> None:
        self.reason = reason
        self.hold = hold
        super().__init__(reason)


# Run output key: the run stopped because Claude could not be used and waits for it.
WAITING_FOR_CLAUDE = "waiting_for_claude"


class ClaudeUnavailable(Exception):
    """Claude's login is missing or expired, or the usage limit is reached.

    Nothing about the ticket is wrong, so it is not blocked: the run waits (interrupted, not
    held) and the supervisor resumes it once a probe shows Claude works again.
    """

    def __init__(self, failure: WorkerFailure) -> None:
        assert failure.outcome is not None
        self.kind = "auth" if failure.outcome.status is ClaudeStatus.AUTH else "usage_limit"
        self.procedure = failure.procedure
        self.detail = failure.detail
        super().__init__(failure.detail)


def scoped_digest(ctx: TicketContext, selected_ids: list[str]) -> str:
    bodies = {c.id: c.body_text for c in ctx.comments if c.id in selected_ids}
    return digest({"brief": brief_text(ctx.issue), "comments": sorted(bodies.items())})


def attempt_key(ticket: str, stage: Stage, intake_material_digest: str) -> str:
    return f"{ticket}:{stage.value}:{intake_material_digest}"


class StageExecutor:
    def __init__(self, deps: Deps, on_child: ChildCallback | None = None) -> None:
        self.deps = deps
        self.on_child = on_child

    # ------------------------------------------------------------------ creation
    def create_run(
        self, ticket: TicketContext, intake: Intake, attempt: int = 1, adopted: bool = False
    ) -> RunContext:
        cfg = self.deps.cfg
        stage = intake.stage
        brief_digest = digest(brief_text(ticket.issue))
        material = intake.material(brief_digest)
        input_revision = digest(material)
        run_id = new_run_id(ticket.key, stage.value)
        record = RunRecord(
            ticket_key=ticket.key,
            run_id=run_id,
            attempt=attempt,
            stage=stage,
            developer_account_id=cfg.identity.developer_jira_account_id,
            worker_id=cfg.identity.worker_id,
            session_label=run_id,
            state=RunState.DISCOVERED,
            attempt_key=attempt_key(ticket.key, stage, input_revision),
            entry_history_id=intake.entry.history_id if intake.entry else None,
            adopted=adopted,
            input_revision=input_revision,
            brief_digest=brief_digest,
            selected_comment_ids=[c.id for c in intake.selected],
            config_digest=cfg.digest(),
            plugin_digest=self.deps.plugin.digest,
            timeout_seconds=cfg.runtime.timeout_seconds,
            outputs={"intake": intake.persisted(), "material": material},
        )
        journal = self.deps.store.run_journal(ticket.key, run_id)
        journal.create(record)
        (journal.dir / "inputs").mkdir(mode=0o700, exist_ok=True)
        return RunContext(
            self.deps,
            ticket,
            intake,
            record,
            journal,
            intake.record or ticket.record,
            self.on_child,
        )

    # ------------------------------------------------------------------ lifecycle
    async def execute(self, rc: RunContext) -> RunRecord:
        stage = rc.record.stage
        sd = STAGES[stage]
        strategy = STRATEGIES[stage](rc)
        try:
            await self._start(rc, sd.ready, sd.active)
            await self._fetch_attachments(rc)
            decision = await self._decide(rc, strategy, sd.active)
            rc.record.outputs["decision"] = decision.model_dump(mode="json")
            rc.record = rc.record.model_copy(update={"state": RunState.PUBLISHING})
            rc.save("publishing", outcome=decision.outcome)
            await self._publish(rc, strategy, decision)
        except StopRequested as stop:
            await self._interrupt(rc, stop.reason, hold=stop.hold)
        except ClaudeUnavailable as exc:
            await self._wait_for_claude(rc, exc)
        except asyncio.CancelledError:
            reason = rc.stop_reason or "supervisor shutdown"
            await asyncio.shield(self._interrupt(rc, reason, hold=rc.stop_hold))
            raise
        except TicketMoved as moved:
            await self._moved(rc, moved)
        except PublicationUncertain as exc:
            # Before a decision exists the run is resumable work; afterwards it is publication.
            state = RunState.PUBLISHING if "decision" in rc.record.outputs else RunState.INTERRUPTED
            rc.record = rc.record.model_copy(
                update={
                    "state": state,
                    "held": False,
                    "reason": f"uncertain: {exc}",
                    "next_action": "Automatic reconciliation on the next poll (`delivery recover` to force).",
                }
            )
            rc.save("publication_uncertain", error=str(exc))
        except BranchDiverged as exc:
            await self._block_after(
                rc,
                strategy,
                sd.active,
                f"remote branch diverged: {exc}",
                "Another push changed the ticket branch. Reconcile the branch by hand "
                "(no force push is ever performed), then resume.",
                "branch_diverged",
            )
        except PublicationError as exc:
            await self._fail(
                rc,
                str(exc),
                "Inspect with `delivery inspect`; fix the remote state, then "
                "`delivery recover`. No force push or blind retry is performed.",
            )
        except (IntegrationError, UncertainResult) as exc:
            rc.record = rc.record.model_copy(
                update={
                    "state": RunState.INTERRUPTED,
                    "reason": f"integration unavailable: {exc}",
                    "next_action": "Retried after connectivity returns.",
                }
            )
            rc.save("integration_error", error=str(exc))
        finally:
            self.deps.ports.release(rc.run_id)
        return rc.record

    async def resume_publication(self, rc: RunContext) -> RunRecord:
        """Re-run publication of a persisted decision (idempotent via operation markers)."""
        stage = rc.record.stage
        strategy = STRATEGIES[stage](rc)
        raw = rc.record.outputs.get("decision")
        if raw is None:
            raise ValueError("run has no persisted decision")
        try:
            await self._publish(rc, strategy, Decision.model_validate(raw))
        except TicketMoved as moved:
            await self._moved(rc, moved)
        except PublicationUncertain as exc:
            rc.record = rc.record.model_copy(update={"reason": f"uncertain: {exc}"})
            rc.save("publication_uncertain", error=str(exc))
        except (PublicationError, BranchDiverged) as exc:
            await self._fail(rc, str(exc), "Inspect and fix the remote state, then `delivery recover`.")
        return rc.record

    async def _fetch_attachments(self, rc: RunContext) -> None:
        if rc.record.stage not in ATTACHMENT_STAGES:
            return
        await self._fetch_designs(rc)
        if not rc.ticket.issue.attachments:
            return
        rc.attachments, rc.attachments_skipped = await fetch_attachments(
            rc.cfg.jira.attachments,
            self.deps.jira,
            rc.ticket.issue.attachments,
            rc.inputs_dir / "attachments",
        )
        rc.journal.events.append(
            "attachments",
            {
                "given": [{"file": a.filename, "sha256": a.sha256, "size": a.size} for a in rc.attachments],
                "skipped": [{"file": a.filename, "reason": a.reason} for a in rc.attachments_skipped],
            },
        )

    async def _fetch_designs(self, rc: RunContext) -> None:
        links = figma_links(rc.ticket.issue.description_text)
        if not links:
            return
        refining = rc.record.stage is Stage.REFINEMENT
        # Refinement writes the spec from the current design and pins its version; later
        # stages build and check against that pinned version.
        pinned = {} if refining else dict(rc.shared.design_versions)
        got = await fetch_designs(rc.cfg.figma, self.deps.figma, links, rc.inputs_dir / "designs", pinned)
        rc.designs, rc.designs_skipped = got.refs, got.skipped
        if refining:
            rc.shared = rc.shared.model_copy(update={"design_versions": got.versions})
        rc.journal.events.append(
            "designs",
            {
                "given": [
                    {"frame": d.frame_name, "version": d.version, "changed": d.changed_in_figma_since}
                    for d in got.refs
                ],
                "skipped": [{"url": d.url, "reason": d.reason} for d in got.skipped],
            },
        )
        if got.changed:
            await rc.publisher().comment(
                rc.key,
                f"design-drift-{rc.record.stage.value}",
                comments.design_drift(rc.record.stage.value, [(d.frame_name, d.url) for d in got.changed]),
            )

    async def _start(self, rc: RunContext, ready: Status, active: Status) -> None:
        """Start publication. Re-entrant: on resume, uncertain start operations are reconciled
        by their markers (confirmed ones are skipped) before any work continues."""
        resuming = rc.record.state not in (RunState.DISCOVERED, RunState.STARTING)
        if resuming and not rc.journal.pending_ops():
            return
        if not resuming:
            rc.record = rc.record.model_copy(update={"state": RunState.STARTING, "started_at": utcnow()})
            rc.save("starting")
        pub = rc.publisher()
        sd = STAGES[rc.record.stage]
        if rc.record.adopted:
            # A human already chose the start action; repeating it is impossible and unneeded.
            issue = await self.deps.jira.get_issue(rc.key)
            current = rc.cfg.status_by_id().get(issue.view.status_id)
            if current is not active:
                raise TicketMoved(rc.key, active, current)
        else:
            await pub.transition(rc.key, ready, sd.start_action)
        rc.shared = rc.shared.model_copy(
            update={
                "worker_id": rc.cfg.identity.worker_id,
                "developer_account_id": rc.cfg.identity.developer_jira_account_id,
                "current_run_id": rc.run_id,
                "current_stage": rc.record.stage,
                "current_state": RunState.RUNNING,
                "session_label": rc.record.session_label,
                "updated_at": utcnow(),
                "history": [
                    *rc.shared.history[-30:],
                    {"run": rc.run_id, "stage": rc.record.stage.value, "at": utcnow().isoformat()},
                ],
            }
        )
        await pub.save_record(rc.key, rc.shared, "start")
        await pub.set_resume_field(rc.key, None, "start")
        if rc.intake.kind is IntakeKind.READY:
            await pub.comment(
                rc.key,
                "start",
                comments.started(
                    rc.record.stage.value,
                    rc.run_id,
                    rc.cfg.identity.worker_id,
                    rc.intake.reason,
                    moved_by_hand=STATUS_NAMES[active] if rc.record.adopted else None,
                    models={p: rc.cfg.claude.model_for(p) for p in sd.procedures},
                    notes=len(notes_for(rc)),
                ),
            )
        rc.record = rc.record.model_copy(update={"state": RunState.RUNNING, "heartbeat_at": utcnow()})
        rc.save("running")

    async def _block_after(
        self, rc: RunContext, strategy: StageStrategy, active: Status, reason: str, action: str, kind: str
    ) -> None:
        """Move the ticket to Blocked after an unsafe-to-continue failure, if it is still ours."""
        d = Decision(outcome="blocked", reason=reason, action=action, blocker_kind=kind)
        rc.record.outputs["decision"] = d.model_dump(mode="json")
        try:
            await self._publish(rc, strategy, d)
        except (TicketMoved, PublicationError, PublicationUncertain, IntegrationError) as exc:
            await self._fail(
                rc,
                f"{reason}; could not move to Blocked: {exc}",
                "Inspect with `delivery inspect`, then `delivery recover`.",
            )

    async def _decide(self, rc: RunContext, strategy: StageStrategy, active: Status) -> Decision:
        if rc.intake.kind is IntakeKind.BLOCK:
            return Decision(
                outcome="blocked",
                reason=rc.intake.reason,
                action=rc.intake.next_action,
                blocker_kind=rc.intake.blocker_kind,
                resume_stage=rc.intake.resume_stage.value if rc.intake.resume_stage else None,
                gate_token=rc.intake.gate_token,
            )
        work = asyncio.create_task(self._work(strategy))
        monitor = asyncio.create_task(self._monitor(rc, work))
        try:
            return await work
        except asyncio.CancelledError:
            stale = rc.record.outputs.get("stale")
            if stale and not asyncio.current_task().cancelling():  # type: ignore[union-attr]
                return await self._stale_decision(rc, stale)
            raise
        except WorkerFailure as exc:
            if exc.outcome and exc.outcome.status in HUMAN_ACTION:
                raise ClaudeUnavailable(exc) from exc
            return self._worker_failure_decision(rc, exc)
        except OutputInvalid as exc:
            return Decision(
                outcome="blocked",
                reason=f"worker output rejected: {exc}",
                action="Inspect the run output (`delivery inspect`), then resume.",
                blocker_kind="invalid_output",
            )
        except (WorktreeConflict, GitError) as exc:
            return Decision(
                outcome="blocked",
                reason=f"repository operation failed: {exc}",
                action="Check the managed worktree/branch state, then resume.",
                blocker_kind="git",
            )
        except ResourceExhausted as exc:
            return Decision(
                outcome="blocked",
                reason=f"machine resource unavailable: {exc}",
                action="Free local resources (ports/disk), then resume.",
                blocker_kind="resource",
            )
        finally:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor

    async def _work(self, strategy: StageStrategy) -> Decision:
        await strategy.deps.repo.fetch()  # always start from the current remote state
        pre = await strategy.preflight()
        return pre if pre is not None else await strategy.work()

    def _worker_failure_decision(self, rc: RunContext, exc: WorkerFailure) -> Decision:
        status = exc.outcome.status if exc.outcome else None
        if status is ClaudeStatus.USAGE_LIMIT:
            action = (
                "Wait for the Claude subscription limit to reset, then resume. No paid API fallback is used."
            )
        elif status is ClaudeStatus.AUTH:
            action = "Sign in to Claude Code interactively on this machine (`claude`), then resume."
        elif status is ClaudeStatus.PLUGIN_MISSING:
            action = "Check `claude.plugin_path` and run `delivery doctor`, then resume."
        elif status is ClaudeStatus.TIMEOUT:
            minutes = rc.cfg.claude.timeout_for(exc.procedure, rc.cfg.runtime.timeout_seconds) // 60
            action = (
                f"The session ran longer than its {minutes}-minute limit. Check the log; if it was "
                f"making progress, raise [claude.timeout_minutes] {exc.procedure} in the config and "
                "restart the coordinator, then resume."
            )
        elif status is ClaudeStatus.MAX_TURNS:
            action = (
                f"The session used all {rc.cfg.claude.turns_for(exc.procedure)} turns allowed for "
                f"{exc.procedure}. Check the log; if it was making progress, raise "
                f"[claude.turn_limits] {exc.procedure} in the config and restart the "
                "coordinator, then resume."
            )
        elif status is ClaudeStatus.GUARDRAIL:
            action = (
                "The coordinator stopped the session because it was not making progress. Check the log "
                "for what it was stuck on; add guidance as a comment or adjust the plan if needed, then "
                "resume."
            )
        else:
            action = "Check the session log, fix the cause, then resume."
        action += f" Log: `delivery logs {rc.key}`."
        if rc.record.outputs.get("wip"):
            files = len(rc.record.outputs["wip"].get("files", []))
            action += f" The unfinished changes ({files} files) were kept; resuming continues from them."
        if exc.outcome and exc.outcome.permission_denials:
            action += f" ({len(exc.outcome.permission_denials)} permission denials were recorded.)"
        return Decision(
            outcome="blocked",
            reason=f"{exc.procedure}: {exc.detail}",
            action=action,
            blocker_kind=exc.blocker_kind,
        )

    async def _monitor(self, rc: RunContext, work: asyncio.Task[Decision]) -> None:
        """Watch assignee, status and scoped inputs while the worker runs."""
        cfg = rc.cfg
        active = STAGES[rc.record.stage].active
        baseline = scoped_digest(rc.ticket, rc.record.selected_comment_ids)
        while not work.done():
            await asyncio.sleep(cfg.runtime.heartbeat_seconds)
            try:
                fresh = await load_context(rc.deps.jira, cfg, rc.key)
            except Exception as exc:
                rc.journal.events.append("monitor_error", {"error": str(exc)})
                continue
            rc.record = rc.record.model_copy(update={"heartbeat_at": utcnow()})
            rc.save("heartbeat")
            reason = None
            if fresh.issue.view.assignee_account_id != cfg.identity.developer_jira_account_id:
                reason = "assignee_changed"
            elif fresh.status is not active:
                reason = f"status_changed:{fresh.status.value if fresh.status else 'unmapped'}"
            elif scoped_digest(fresh, rc.record.selected_comment_ids) != baseline:
                reason = "inputs_changed"
            if reason:
                rc.record.outputs["stale"] = reason
                rc.save("stale_detected", reason=reason)
                work.cancel()
                return

    async def _stale_decision(self, rc: RunContext, stale: str) -> Decision:
        if stale == "inputs_changed":
            return Decision(
                outcome="blocked",
                reason="the brief or selected comments changed while the worker ran; its "
                "result is stale and was not published",
                action="Resume the stage to rerun it with the current inputs.",
                blocker_kind="stale_input",
            )
        raise StopRequested(f"stale: {stale}; result preserved locally and not published", hold=True)

    async def _publish(self, rc: RunContext, strategy: StageStrategy, d: Decision) -> None:
        await strategy.publish(d)
        terminal = {
            "success": RunState.COMPLETED
            if rc.record.stage is Stage.RELEASE_VERIFICATION
            else RunState.AWAITING_HUMAN,
            "clarification": RunState.AWAITING_HUMAN,
            "verification_failed": RunState.AWAITING_HUMAN,
            "blocked": RunState.BLOCKED,
        }.get(d.outcome, RunState.AWAITING_HUMAN)
        outcome = {
            "success": Outcome.COMPLETED,
            "clarification": Outcome.NEEDS_CLARIFICATION,
            "blocked": Outcome.BLOCKED,
            "verification_failed": Outcome.FAILED,
        }.get(d.outcome)
        rc.record = rc.record.model_copy(
            update={
                "state": terminal,
                "outcome": outcome,
                "ended_at": utcnow(),
                "reason": d.reason[:2000],
                "next_action": d.action or _next_action(d.outcome, rc.record.stage),
            }
        )
        rc.save("published", outcome=d.outcome)
        await strategy.cleanup()

    async def _interrupt(self, rc: RunContext, reason: str, hold: bool) -> None:
        rc.record = rc.record.model_copy(
            update={
                "state": RunState.INTERRUPTED,
                "reason": reason,
                "held": hold,
                "hold_reason": reason if hold else "",
                "child": None,
                "next_action": ("`delivery recover " + rc.key + " --resume` to continue, or handover/cancel")
                if hold
                else "Resumes automatically when the supervisor restarts.",
            }
        )
        rc.save("interrupted", reason=reason, hold=hold)

    async def _wait_for_claude(self, rc: RunContext, exc: ClaudeUnavailable) -> None:
        rc.record.outputs[WAITING_FOR_CLAUDE] = {
            "kind": exc.kind,
            "procedure": exc.procedure,
            "detail": exc.detail,
            "at": utcnow().isoformat(),
        }
        rc.record = rc.record.model_copy(
            update={
                "state": RunState.INTERRUPTED,
                "held": False,
                "hold_reason": "",
                "child": None,
                "reason": f"waiting for Claude: {exc.detail}",
                "next_action": "Continues automatically once Claude works again; nothing to do in Jira.",
            }
        )
        rc.save("waiting_for_claude", kind=exc.kind, detail=exc.detail)
        await self.notice(
            rc,
            "waiting-for-claude",
            comments.claude_unavailable(rc.record.stage.value, exc.kind, rc.cfg.identity.worker_id),
        )

    async def notice(self, rc: RunContext, op: str, markdown: str) -> bool:
        """Best-effort informational comment, journaled apart from the run's publication."""
        journal = RunJournal(rc.cfg.runtime.state_dir / "intake" / rc.key)
        pub = Publisher(rc.cfg, self.deps.jira, None, None, journal, f"notice-{rc.run_id}")
        try:
            await pub.comment(rc.key, op, markdown)
        except Exception as exc:
            rc.journal.events.append("notice_not_posted", {"op": op, "error": str(exc)[:300]})
            return False
        return True

    async def _moved(self, rc: RunContext, moved: TicketMoved) -> None:
        if moved.actual is Status.CANCELLED:
            rc.record = rc.record.model_copy(
                update={
                    "state": RunState.FAILED,
                    "reason": "cancelled by a human; publication suppressed",
                    "ended_at": utcnow(),
                    "next_action": "None (cancelled).",
                }
            )
            rc.save("cancelled")
            await STRATEGIES[rc.record.stage](rc).cleanup()
            return
        await self._interrupt(
            rc,
            f"ticket moved by a human to {moved.actual.value if moved.actual else '?'}; "
            "nothing further was published",
            hold=True,
        )

    async def _fail(self, rc: RunContext, reason: str, action: str) -> None:
        rc.record = rc.record.model_copy(
            update={
                "state": RunState.FAILED,
                "reason": reason[:2000],
                "next_action": action,
                "ended_at": utcnow(),
            }
        )
        rc.save("failed", reason=reason)


def _next_action(outcome: str, stage: Stage) -> str:
    ready = STATUS_NAMES[STAGES[stage].ready]
    return {
        "success": {
            Stage.REFINEMENT: "Review the specification in Jira; approving moves it into Ready for planning.",
            Stage.PLANNING: "Review the plan in Jira; approving moves it into Ready for development.",
            Stage.DEVELOPMENT: "Nothing yet: it moves into Ready for verification and verification starts.",
            Stage.VERIFICATION: "Independent GitHub review, then Approve code in Jira "
            "(moves into Acceptance review).",
            Stage.RELEASE_PREPARATION: "Review the release proposal; approving moves it into "
            "Ready for release.",
            Stage.RELEASE_VERIFICATION: "Done.",
        }[stage],
        "clarification": f"Answer the questions in Jira, then Submit answers (moves into {ready}).",
        "verification_failed": "Submit implementation changes to fix (moves into Ready for development), "
        "or Revise scope.",
        "blocked": f"Resolve the blocker, then Resume in Jira (moves into {ready}).",
    }.get(outcome, "")


__all__ = [
    "RunJournal",
    "StageExecutor",
    "StopRequested",
    "attempt_key",
    "scoped_digest",
    "timedelta",
]
