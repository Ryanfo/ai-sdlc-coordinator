"""The supervisor: one per Jira site + developer identity, any number of sessions.

Each poll discovers every eligible ticket and dispatches each one as its own task. A
slow, blocked, failed or human-gated ticket never holds up another. There is no
session-count limit; claims and dedup are per ticket and per attempt. Dispatch can be
paused manually while existing sessions continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delivery import __version__, console
from delivery import comments as comment_text
from delivery.claude import ChildHandle
from delivery.control import ControlServer, socket_path
from delivery.coordinator import StageExecutor
from delivery.intake import (
    Intake,
    IntakeEvaluator,
    IntakeKind,
    RecordCorrupt,
    TicketContext,
    brief_text,
    load_context,
)
from delivery.journal import JournalCorrupt, RunJournal, SupervisorRecord
from delivery.models import ACTIVE_RUN_STATES, RunRecord, RunState, digest, utcnow
from delivery.open_sessions import OpenSessions
from delivery.ownership import (
    FileLock,
    LockHeld,
    TicketClaims,
    active_jql,
    evaluate_eligibility,
    ready_jql,
    supervisor_lock,
)
from delivery.ports import IntegrationError
from delivery.proc import pid_alive, process_start_marker, signal_group
from delivery.publication import Publisher
from delivery.runtime import Deps, RunContext
from delivery.stages import change_ids
from delivery.workflow import STAGES, STATUS_NAMES, Stage, Status, stage_for_active

log = logging.getLogger("delivery")


@dataclass
class Session:
    key: str
    rc: RunContext
    task: asyncio.Task[RunRecord]
    process: ChildHandle | None = None


@dataclass
class PollReport:
    discovered: list[dict[str, Any]] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    waiting: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    error: str = ""


class Supervisor:
    def __init__(
        self, deps: Deps, *, dry_run: bool = False, emit: Callable[[str], None] | None = None
    ) -> None:
        self.deps = deps
        self.cfg = deps.cfg
        self.dry_run = dry_run
        self.emit = emit or (lambda s: log.info(s))
        self.sessions: dict[str, Session] = {}
        self.claims = TicketClaims(deps.store.locks_dir)
        self.executor = StageExecutor(deps, on_child=self._on_child)
        self.evaluator = IntakeEvaluator(self.cfg, deps.github)
        self.stop_event = asyncio.Event()
        self.lock: FileLock | None = None
        self.control: ControlServer | None = None
        self.record = SupervisorRecord(
            identity_key=self.cfg.identity_key,
            worker_id=self.cfg.identity.worker_id,
            developer_account_id=self.cfg.identity.developer_jira_account_id,
        )
        self.corrupt: dict[str, str] = {}
        self._noted: set[str] = set()
        self.backoff_until: float = 0.0
        self.backoff_seconds: float = 0.0
        # Tickets being started or having a follow-up published: never both at once.
        self.busy: set[str] = set()
        self.open: OpenSessions | None = (
            OpenSessions(deps, self.emit, is_running=lambda k: k in self.sessions, busy=self.busy)
            if self.cfg.claude.interactive.enabled
            else None
        )
        self._open_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> Supervisor:
        self.deps.store.init()
        self.lock = supervisor_lock(self.cfg)
        self.lock.acquire(
            {
                "worker_id": self.cfg.identity.worker_id,
                "identity": self.cfg.identity_key,
                "mode": "dry-run" if self.dry_run else "run",
            }
        )
        previous = self.deps.store.load_supervisor()
        if previous:
            self.record = previous
        self.record = self.record.model_copy(
            update={
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": utcnow(),
                "heartbeat_at": utcnow(),
                "stopped_at": None,
                "version": __version__,
            }
        )
        if not self.dry_run:
            path = socket_path(self.cfg.runtime.state_dir, self.cfg.identity_key)
            self.control = ControlServer(path, self.handle)
            await self.control.start()
            self.record = self.record.model_copy(update={"control_socket": str(path)})
        self.deps.store.save_supervisor(self.record, "supervisor_started", pid=os.getpid())
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.shutdown()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_event.set)

    async def shutdown(self) -> None:
        """Stop and checkpoint every owned child, then release the identity lock.

        Sessions left open for questions keep running in tmux; they are watched again on restart.
        """
        if self._open_task:
            self._open_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._open_task
        if self.open and (left := self.open.registry.all()):
            names = ", ".join(f"{r.ticket_key} {r.procedure}" for r in left)
            self.emit(console.line(f"still open in tmux (watched again on restart): {names}"))
        tasks = []
        for s in list(self.sessions.values()):
            s.rc.stop_reason, s.rc.stop_hold = "supervisor shutdown", False
            s.task.cancel()
            tasks.append(s.task)
        if tasks:
            await asyncio.wait(tasks, timeout=60)
        if self.control:
            await self.control.stop()
        self.record = self.record.model_copy(update={"stopped_at": utcnow(), "control_socket": None})
        self.deps.store.save_supervisor(
            self.record, "supervisor_stopped", interrupted=[t.get_name() for t in tasks]
        )
        if self.lock:
            self.lock.release()
        await self.deps.jira.close()

    async def run(self, once: bool = False) -> None:
        if not self.dry_run:
            await self.deps.repo.ensure()
        await self.reconcile()
        if self.open and not self.dry_run and not once:
            self._open_task = asyncio.create_task(self._watch_open(), name="open-sessions")
        while not self.stop_event.is_set():
            if not self.record.dispatch_paused:
                await self.poll_once()
            elif not once:
                self.emit(console.line("dispatch paused; existing sessions continue"))
            if once:
                if self.sessions:
                    await asyncio.wait([s.task for s in self.sessions.values()])
                break
            delay = self.cfg.runtime.poll_seconds + random.uniform(0, self.cfg.runtime.poll_jitter_seconds)
            delay = max(delay, self.backoff_until - asyncio.get_running_loop().time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)

    async def _watch_open(self) -> None:
        """Sessions left open after hand-off: follow-up changes, closing (delivery.open_sessions)."""
        assert self.open is not None
        while not self.stop_event.is_set():
            try:
                await self.open.tick()
            except Exception:
                log.exception("open-session check failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=OPEN_SESSION_TICK_SECONDS)

    # ------------------------------------------------------------------ discovery
    async def poll_once(self) -> PollReport:
        report = PollReport()
        loop = asyncio.get_running_loop()
        if loop.time() < self.backoff_until:
            return report
        try:
            issues = await self.deps.jira.search(ready_jql(self.cfg))
            working = await self.deps.jira.search(active_jql(self.cfg))
        except IntegrationError as exc:
            # Back off the Jira request stream only; running sessions are unaffected.
            self.backoff_seconds = min(max(self.backoff_seconds * 2, 15.0), 600.0)
            self.backoff_until = loop.time() + self.backoff_seconds
            report.error = str(exc)
            self.record = self.record.model_copy(update={"last_poll_error": str(exc)[:500]})
            self.deps.store.save_supervisor(self.record)
            self.emit(console.line(f"poll failed ({exc}); retrying in {self.backoff_seconds:.0f}s"))
            return report
        self.backoff_seconds = 0.0
        candidates: list[tuple[str, str, TicketContext, Stage]] = []
        for issue in issues:
            elig = evaluate_eligibility(issue.view, self.cfg, set(self.sessions))
            if not elig.eligible or elig.stage is None:
                report.skipped.append({"ticket": issue.key, "reason": "; ".join(elig.reasons)})
                continue
            if issue.key in self.corrupt:
                report.skipped.append({"ticket": issue.key, "reason": self.corrupt[issue.key]})
                continue
            try:
                ctx = await load_context(self.deps.jira, self.cfg, issue.key)
            except (IntegrationError, RecordCorrupt) as exc:
                report.skipped.append({"ticket": issue.key, "reason": str(exc)})
                continue
            entry = ctx.latest_entry(self.cfg.status_id(STAGES[elig.stage].ready))
            ready_at = entry.created.isoformat() if entry else ""
            candidates.append((ready_at, issue.key, ctx, elig.stage))
        # Deterministic order for reproducible logs; it never serialises execution.
        for ready_at, key, ctx, stage in sorted(candidates, key=lambda c: (c[0], c[1])):
            report.discovered.append({"ticket": key, "stage": stage.value, "ready_at": ready_at})
            await self.consider(ctx, stage, report)
        for issue in working:
            await self._check_moved_by_hand(issue.key, issue.view.status_id, report)
        self.record = self.record.model_copy(
            update={"last_poll_at": utcnow(), "last_poll_error": "", "heartbeat_at": utcnow()}
        )
        self.deps.store.save_supervisor(self.record)
        return report

    async def _check_moved_by_hand(self, key: str, status_id: str, report: PollReport) -> None:
        """A ticket of ours in an "agent working" status with no session behind it.

        Only the coordinator should make the start move, but nothing in Jira stops a human doing
        it (for example dragging the card to the Agent working column). If the ticket came
        straight from the stage's ready status and no run of ours consumed that ready entry,
        evaluate it exactly as if it were still ready and, if valid, take it over without
        repeating the start transition. Anything else is left alone and explained.
        """
        if key in self.sessions or key in self.corrupt:
            return
        status = self.cfg.status_by_id().get(status_id)
        sd = stage_for_active(status) if status else None
        if status is None or sd is None:
            return
        local = [e.record for e in self.deps.store.runs_for_ticket(key) if e.record]
        if any(r.state not in TERMINAL for r in local):
            return  # an interrupted run of ours: startup recovery and `delivery recover` own it
        try:
            ctx = await load_context(self.deps.jira, self.cfg, key)
        except (IntegrationError, RecordCorrupt) as exc:
            report.skipped.append({"ticket": key, "reason": str(exc)})
            return
        came_in = ctx.latest_entry(status_id)
        ready_entry = ctx.latest_entry(self.cfg.status_id(sd.ready))
        if came_in is None or came_in.from_id != self.cfg.status_id(sd.ready) or ready_entry is None:
            self._note_once(
                f"{key}:{came_in.history_id if came_in else '-'}",
                f"{key} is in {STATUS_NAMES[status]} but did not arrive from "
                f"{STATUS_NAMES[sd.ready]}; leaving it alone (`delivery inspect {key}` explains)",
            )
            report.skipped.append({"ticket": key, "reason": "in an agent-working status without a run"})
            return
        if any(r.stage is sd.stage and r.entry_history_id == ready_entry.history_id for r in local):
            return  # this coordinator made that move; its run has already finished
        self._note_once(
            f"{key}:{came_in.history_id}",
            f"{key} was moved into {STATUS_NAMES[status]} by hand; checking it as if it were in "
            f"{STATUS_NAMES[sd.ready]}",
        )
        await self.consider(ctx, sd.stage, report, adopt=True)

    def _note_once(self, marker: str, message: str) -> None:
        if marker not in self._noted:
            self._noted.add(marker)
            self.emit(console.line(message))

    async def consider(
        self, ctx: TicketContext, stage: Stage, report: PollReport, adopt: bool = False
    ) -> None:
        key = ctx.key
        foreign = (
            ctx.record.current_state in ACTIVE_RUN_STATES
            and ctx.record.worker_id != self.cfg.identity.worker_id
        )
        if foreign:
            report.skipped.append(
                {
                    "ticket": key,
                    "reason": f"active run marker from worker "
                    f"{ctx.record.worker_id}; refusing a second worker",
                }
            )
            return
        try:
            intake = await self.evaluator.evaluate(ctx, stage)
        except IntegrationError as exc:
            report.skipped.append({"ticket": key, "reason": f"intake failed: {exc}"})
            return
        if intake.kind is IntakeKind.WAIT:
            report.waiting.append({"ticket": key, "reason": intake.reason})
            if not self.dry_run:
                await self._explain_wait(ctx, intake)
            return
        executor = self.executor
        brief_digest = digest(brief_text(ctx.issue))
        attempt = f"{key}:{stage.value}:{digest(intake.material(brief_digest))}"
        existing = self.deps.store.find_attempt(attempt)
        if existing and existing.record:
            report.skipped.append(
                {
                    "ticket": key,
                    "reason": f"attempt already recorded as "
                    f"{existing.record.state.value} ({existing.run_id})",
                }
            )
            return
        if self.dry_run:
            report.started.append(key)
            verb = "take over" if adopt else "start"
            self.emit(f"[dry-run] would {verb} {stage.value} for {key}: {intake.kind.value} {intake.reason}")
            return
        if key in self.busy:
            report.skipped.append({"ticket": key, "reason": "a follow-up change is being published"})
            return
        self.busy.add(key)
        try:
            rc = executor.create_run(ctx, intake, adopted=adopt)
            if not self.claims.claim(key, rc.run_id):
                rc.record = rc.record.model_copy(
                    update={"state": RunState.FAILED, "reason": "ticket already claimed"}
                )
                rc.save("claim_refused")
                report.skipped.append({"ticket": key, "reason": "ticket already claimed by another run"})
                return
            if self.open:
                await self.open.close_for_run(key, stage)
            self._launch(rc)
        finally:
            self.busy.discard(key)
        report.started.append(key)

    def _launch(self, rc: RunContext, resume: bool = False, publish_only: bool = False) -> None:
        async def runner() -> RunRecord:
            try:
                if publish_only:
                    record = await self.executor.resume_publication(rc)
                else:
                    record = await self.executor.execute(rc)
                await self._surface_changes(rc, record)
                return record
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("run %s crashed", rc.run_id)
                rc.record = rc.record.model_copy(
                    update={
                        "state": RunState.INTERRUPTED,
                        "reason": f"internal error: {exc}",
                        "held": True,
                        "hold_reason": "internal error",
                    }
                )
                rc.save("internal_error", error=str(exc))
                return rc.record
            finally:
                self.claims.release(rc.key, rc.run_id)
                self.sessions.pop(rc.key, None)
                self.emit(
                    console.session_finished(
                        self.cfg, rc.record, rc.ticket.issue.view.summary, rc.journal.dir / "logs"
                    )
                )

        task = asyncio.create_task(runner(), name=rc.run_id)
        self.sessions[rc.key] = Session(rc.key, rc, task)
        self.emit(
            console.session_started(
                self.cfg, rc.record, rc.ticket.issue.view.summary, resumed=resume, adopted=rc.record.adopted
            )
        )

    async def _surface_changes(self, rc: RunContext, record: RunRecord) -> None:
        """A run that actioned change requests from Jira has published: bring its open Claude
        session up, where Claude has listed what it did and asked for anything further."""
        if self.open is None or record.state not in (RunState.AWAITING_HUMAN, RunState.COMPLETED):
            return
        ids = change_ids(rc.record.outputs.get("feedback_items") or rc.intake.feedback_items)
        if not ids:
            return
        text = (
            f"{rc.key}: the changes requested in Jira ({', '.join(ids)}) are done and published. "
            "Anything further? Type it here."
        )
        try:
            if await self.open.surface(rc.run_id, text):
                self.emit(
                    console.line(
                        f"{rc.key}: changes {', '.join(ids)} actioned; its Claude session is open for "
                        f"anything further: delivery attach {rc.key}"
                    )
                )
        except Exception as exc:  # a window must never fail a finished run
            log.warning("could not bring up the session of %s: %s", rc.run_id, exc)

    def _on_child(self, key: str, proc: ChildHandle | None) -> None:
        if key in self.sessions:
            self.sessions[key].process = proc

    async def _explain_wait(self, ctx: TicketContext, intake: Intake) -> None:
        """Post one explanation per (entry, reason). Nothing starts until the input exists."""
        if intake.entry is None:
            return
        journal = RunJournal(self.cfg.runtime.state_dir / "intake" / ctx.key)
        pub = Publisher(self.cfg, self.deps.jira, None, None, journal, f"intake-{intake.entry.history_id}")
        try:
            await pub.comment(
                ctx.key,
                "waiting",
                comment_text.waiting(intake.stage.value, intake.reason, intake.next_action),
                revision=digest(intake.reason)[:12],
            )
        except Exception as exc:
            log.warning("could not post wait explanation on %s: %s", ctx.key, exc)

    # ------------------------------------------------------------------ recovery
    async def reconcile(self) -> list[str]:
        """Reconcile every unfinished run independently. Corrupt records block their ticket."""
        actions: list[str] = []
        for entry in self.deps.store.unfinished():
            if entry.error is not None:
                self.corrupt[entry.ticket_key] = f"corrupt local record {entry.run_id}: {entry.error.detail}"
                actions.append(f"{entry.ticket_key}: blocked locally ({entry.error.detail})")
                continue
            rec = entry.record
            assert rec is not None
            if rec.ticket_key in self.sessions:
                continue
            if rec.child and pid_alive(rec.child.pid):
                marker = process_start_marker(rec.child.pid)
                if rec.child.process_start is None or marker == rec.child.process_start:
                    signal_group(rec.child.pgid, signal.SIGTERM)
                    actions.append(f"{rec.ticket_key}: stopped orphaned child {rec.child.pid}")
            if rec.held:
                actions.append(f"{rec.ticket_key}: held ({rec.hold_reason}); `delivery recover --resume`")
                continue
            try:
                action = await self._recover_run(entry.journal, rec)
            except Exception as exc:
                action = f"recovery deferred: {exc}"
            actions.append(f"{rec.ticket_key}: {action}")
        for a in actions:
            self.emit(console.line(f"reconcile {a}"))
        return actions

    async def _recover_run(self, journal: RunJournal, rec: RunRecord, force: bool = False) -> str:
        ctx = await load_context(self.deps.jira, self.cfg, rec.ticket_key)
        if ctx.issue.view.assignee_account_id != self.cfg.identity.developer_jira_account_id:
            rec = rec.model_copy(
                update={
                    "state": RunState.INTERRUPTED,
                    "held": True,
                    "hold_reason": "assignee changed; handover needed",
                }
            )
            journal.save(rec, "recovery_held")
            return "held: ticket reassigned; no publication"
        sd = STAGES[rec.stage]
        intake = Intake.restore(rec.outputs.get("intake", {"stage": rec.stage.value}), ctx)
        rc = RunContext(
            self.deps,
            ctx,
            intake,
            rec,
            journal,
            intake.record or ctx.record,
            self._on_child,
            {k: Path(v) for k, v in rec.worktrees.items()},
        )
        if not self.claims.claim(rec.ticket_key, rec.run_id):
            return "already claimed"
        if rec.state is RunState.PUBLISHING or (journal.pending_ops() and rec.outputs.get("decision")):
            self._launch(rc, resume=True, publish_only=True)
            return "reconciling publication"
        if rec.state in (RunState.DISCOVERED, RunState.STARTING) and ctx.status is sd.ready:
            if self.open:
                await self.open.close_for_run(rec.ticket_key, rec.stage)
            self._launch(rc, resume=True)
            return "restarting prepared attempt"
        if ctx.status is sd.active and rec.state in (*ACTIVE_RUN_STATES, RunState.INTERRUPTED):
            rc.record = rec.model_copy(update={"state": RunState.RUNNING, "held": False, "hold_reason": ""})
            rc.save("resumed")
            if self.open:
                await self.open.close_for_run(rec.ticket_key, rec.stage)
            self._launch(rc, resume=True)
            return "resuming interrupted work with a fresh session"
        self.claims.release(rec.ticket_key, rec.run_id)
        status = ctx.status.value if ctx.status else "unmapped"
        if ctx.status is Status.CANCELLED:
            journal.save(
                rec.model_copy(update={"state": RunState.FAILED, "reason": "cancelled"}),
                "cancelled",
            )
            return "cancelled in Jira; closed locally"
        journal.save(rec.model_copy(update={"held": True, "hold_reason": f"ticket is now {status}"}), "held")
        return f"held: ticket is in {status}"

    # ------------------------------------------------------------------ control
    async def stop_ticket(self, key: str, reason: str, hold: bool = True) -> dict[str, Any]:
        s = self.sessions.get(key)
        if s is None:
            return {"ok": False, "error": f"no active session for {key}"}
        s.rc.stop_reason, s.rc.stop_hold = reason, hold
        s.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({s.task}, timeout=60)
        return {
            "ok": True,
            "ticket": key,
            "run_id": s.rc.run_id,
            "state": s.rc.record.state.value,
            "other_sessions": sorted(k for k in self.sessions if k != key),
        }

    async def handover(self, key: str) -> dict[str, Any]:
        stopped = await self.stop_ticket(key, "handover", hold=True) if key in self.sessions else None
        entries = self.deps.store.runs_for_ticket(key)
        latest = entries[-1] if entries else None
        ctx = await load_context(self.deps.jira, self.cfg, key)
        artefacts = dict(ctx.record.artefacts)
        state = ctx.status.value if ctx.status else "unmapped"
        stage = ctx.record.current_stage or (latest.record.stage if latest and latest.record else None)
        if (
            latest
            and latest.record
            and ctx.status is not None
            and stage
            and ctx.status is STAGES[stage].active
        ):
            rec = latest.record
            rc = RunContext(
                self.deps,
                ctx,
                Intake.restore(rec.outputs.get("intake", {"stage": rec.stage.value}), ctx),
                rec,
                latest.journal,
                ctx.record,
            )
            from delivery.runtime import Decision
            from delivery.stages import STRATEGIES

            strat = STRATEGIES[rec.stage](rc)
            await strat.publish_block(
                Decision(
                    outcome="blocked",
                    reason="handover requested by the operator; work checkpointed",
                    action="Reassign the ticket, then the new assignee chooses Resume.",
                    blocker_kind="handover",
                ),
                STAGES[rec.stage].active,
            )
            rc.record = rc.record.model_copy(
                update={
                    "state": RunState.BLOCKED,
                    "held": False,
                    "next_action": "Reassign, then Resume.",
                }
            )
            rc.save("handover")
            await strat.cleanup()
            state = "blocked for handover"
        pub = Publisher(
            self.cfg,
            self.deps.jira,
            None,
            None,
            RunJournal(self.cfg.runtime.state_dir / "intake" / key),
            f"handover-{key}",
        )
        await pub.comment(
            key,
            "handover",
            comment_text.handover(
                stage.value if stage else "-",
                state,
                artefacts,
                "Safe to reassign. The next owner's supervisor reconstructs inputs from Jira and Git.",
            ),
            revision=utcnow().isoformat(),
        )
        return {
            "ok": True,
            "ticket": key,
            "stopped": stopped,
            "state": state,
            "ready_for_reassignment": True,
        }

    async def recover(self, key: str, resume: bool) -> dict[str, Any]:
        if key in self.sessions:
            return {"ok": False, "error": f"{key} has an active session"}
        actions = []
        for e in self.deps.store.runs_for_ticket(key):
            if e.error is not None:
                actions.append(f"{e.run_id}: corrupt record ({e.error.detail}); not modified")
                continue
            rec = e.record
            assert rec is not None
            pending = e.journal.pending_ops()
            if rec.state in TERMINAL and not pending:
                continue
            if rec.held and not resume:
                actions.append(f"{e.run_id}: held ({rec.hold_reason}); pass --resume to continue")
                continue
            rec = rec.model_copy(update={"held": False, "hold_reason": ""})
            e.journal.save(rec, "recover_requested", resume=resume)
            actions.append(f"{e.run_id}: {await self._recover_run(e.journal, rec, force=True)}")
        if resume:
            self.corrupt.pop(key, None)
        return {"ok": True, "ticket": key, "actions": actions or ["nothing to recover"]}

    def session_rows(self) -> list[dict[str, Any]]:
        rows = []
        for s in self.sessions.values():
            r = s.rc.record
            rows.append(
                {
                    "ticket": s.key,
                    "stage": r.stage.value,
                    "run_id": r.run_id,
                    "session": r.session_label,
                    "worker": r.worker_id,
                    "state": r.state.value,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "child_pid": s.process.pid if s.process else None,
                    "next_action": r.next_action or "running",
                }
            )
        return sorted(rows, key=lambda x: x["ticket"])

    async def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        if cmd == "status":
            return {
                "ok": True,
                "sessions": self.session_rows(),
                "open_sessions": [
                    {"ticket": r.ticket_key, "procedure": r.procedure, "tmux": r.name, "held": r.held}
                    for r in (self.open.registry.all() if self.open else [])
                ],
                "dispatch_paused": self.record.dispatch_paused,
                "corrupt": self.corrupt,
                "worker_id": self.cfg.identity.worker_id,
            }
        if cmd in ("pause", "resume"):
            paused = cmd == "pause"
            self.record = self.record.model_copy(
                update={"dispatch_paused": paused, "pause_reason": str(req.get("reason", ""))}
            )
            self.deps.store.save_supervisor(self.record, f"dispatch_{cmd}")
            return {"ok": True, "dispatch_paused": paused, "running_sessions": len(self.sessions)}
        key = str(req.get("ticket", ""))
        if cmd == "stop":
            return await self.stop_ticket(key, "operator stop", hold=True)
        if cmd == "handover":
            return await self.handover(key)
        if cmd == "recover":
            return await self.recover(key, bool(req.get("resume")))
        if cmd == "poll":
            rep = await self.poll_once()
            return {"ok": True, "started": rep.started, "waiting": rep.waiting}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


TERMINAL = frozenset({RunState.AWAITING_HUMAN, RunState.COMPLETED, RunState.FAILED, RunState.BLOCKED})
OPEN_SESSION_TICK_SECONDS = 3.0

__all__ = ["JournalCorrupt", "LockHeld", "PollReport", "Supervisor"]
