"""The supervisor: one per Jira site + developer identity, any number of sessions.

Each poll discovers every eligible ticket and dispatches each one as its own task. A
slow, blocked, failed or human-gated ticket never holds up another. There is no
session-count limit; claims and dedup are per ticket and per attempt. Dispatch can be
paused manually while existing sessions continue.

When Claude cannot be used (login expired, usage limit), no ticket is blocked for it: the
runs that hit it wait, no new work starts, and a tiny Claude request every few minutes
finds when it works again; then the waiting runs continue where they stopped.

Runs stopped for a reason that passes by itself (Claude's API or the network down for a while,
Jira or GitHub unreachable, the ticket edited while Claude worked) are resumed by the poll once
their retry time comes. New sessions wait while the machine is short of disk or memory, or while
a changed Claude Code has not passed the sandbox probe (delivery.claude_version). An unexpected
error in a poll is logged and alerted, and the supervisor carries on after a pause instead of
stopping.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from delivery import __version__, cancel_cleanup, cleanup, console
from delivery import comments as comment_text
from delivery.acceptance import Acceptance
from delivery.alerts import Alerts
from delivery.claude import ChildHandle, claude_works
from delivery.claude_version import VersionGuard
from delivery.codestamp import code_mtime
from delivery.control import ControlServer, socket_path
from delivery.coordinator import RETRY, WAITING_FOR_CLAUDE, StageExecutor
from delivery.gates import approved_gate
from delivery.git import GitError, blob_url
from delivery.guidance import Guidance
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
from delivery.models import ACTIVE_RUN_STATES, GateKind, RunRecord, RunState, digest, utcnow
from delivery.open_sessions import OpenSessions
from delivery.ownership import (
    FileLock,
    LockHeld,
    TicketClaims,
    active_jql,
    evaluate_eligibility,
    mine_jql,
    ready_jql,
    release_jql,
    supervisor_lock,
)
from delivery.ports import IntegrationError
from delivery.proc import ProcessStartError, pid_alive, process_start_marker, signal_group
from delivery.proposals import Proposals
from delivery.proposals import load as load_proposals
from delivery.publication import PublicationError, PublicationUncertain, Publisher, TicketMoved
from delivery.reminders import Reminders
from delivery.resources import KeepAwake, short_of_room
from delivery.runtime import Deps, RunContext
from delivery.stages import change_ids
from delivery.staleness import Staleness
from delivery.workflow import STAGES, STATUS_NAMES, Action, Stage, Status, stage_for_active

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
        if deps.alerts is None:
            deps.alerts = Alerts(self.cfg)
        self.alerts = deps.alerts
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
        self.waiting_for_repo = False
        # Tickets being started or having a follow-up published: never both at once.
        self.busy: set[str] = set()
        self.open: OpenSessions | None = (
            OpenSessions(deps, self.emit, is_running=lambda k: k in self.sessions, busy=self.busy)
            if self.cfg.claude.interactive.enabled
            else None
        )
        self._open_task: asyncio.Task[None] | None = None
        # Tickets in Acceptance review: how to try the candidate, and the app (delivery.acceptance).
        self.acceptance = Acceptance(deps, self.emit)
        self._acceptance_task: asyncio.Task[None] | None = None
        # `FOR CLAUDE project` notes on the developer's tickets (delivery.guidance).
        self.guidance = Guidance(self.cfg, deps.jira, deps.github, deps.repo)
        # CREATE TICKETS comments: tickets Claude proposed, created when asked (delivery.proposals).
        self.proposals = Proposals(self.cfg, deps.jira, deps.repo)
        self._comments_seen: dict[str, str] = {}
        self._spikes_done: set[str] = set()
        # Candidates under review that the base branch has moved past (delivery.staleness).
        self.staleness = Staleness(self.cfg, deps.jira, deps.repo, self.emit)
        # Tickets that have waited long for a person (delivery.reminders).
        self.reminders = Reminders(self.cfg, deps.jira, self.emit)
        self._code_noted = False
        # Claude unavailable: seconds between probes (doubling) and when the next one is due.
        self._claude_wait = 0.0
        self._claude_check_at = 0.0
        # Claude Code updated itself: new sessions wait until its sandbox probe passes.
        self.version_guard = VersionGuard(self.cfg, self.alerts, self.emit)
        if dry_run:
            self.version_guard.enabled = False
        # Disk nearly full or critical memory pressure: why new sessions wait, if they do.
        self.room: str | None = None
        self.keep_awake = KeepAwake(self.cfg.runtime.keep_awake and not dry_run)
        # When old local logs and worktrees are next removed (runtime.retention_days).
        self._clean_at = 0.0
        # An unexpected error in the loop: the pause before carrying on (doubling).
        self._error_backoff = 0.0
        self._background: set[asyncio.Task[bool]] = set()

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
        unexpected = (
            previous is not None
            and previous.started_at is not None
            and previous.stopped_at is None
            and not self.dry_run
        )
        self.record = self.record.model_copy(
            update={
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": utcnow(),
                "heartbeat_at": utcnow(),
                "stopped_at": None,
                "version": __version__,
                "code_mtime": code_mtime(),
                # Re-checked by the runs themselves: anything waiting resumes on start.
                "claude_unavailable": None,
            }
        )
        if not self.dry_run:
            path = socket_path(self.cfg.runtime.state_dir, self.cfg.identity_key)
            self.control = ControlServer(path, self.handle)
            await self.control.start()
            self.record = self.record.model_copy(update={"control_socket": str(path)})
        self.deps.store.save_supervisor(self.record, "supervisor_started", pid=os.getpid())
        if unexpected and previous is not None:
            last = previous.heartbeat_at or previous.started_at
            when = last.astimezone().strftime("%a %d %b %H:%M") if last else "?"
            text = (
                f"The coordinator stopped unexpectedly (last seen {when}: the Mac restarted or shut down, "
                "or the process was killed). It has started again; interrupted work resumes now."
            )
            self.emit(console.line(text))
            await self.alerts.send("restarted", "restarted after an unexpected stop", text)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.shutdown()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        # SIGHUP: the terminal window was closed. Stop as cleanly as Ctrl-C.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_event.set)

    async def shutdown(self) -> None:
        """Stop and checkpoint every owned child, then release the identity lock.

        Sessions left open for questions keep running in tmux; they are watched again on restart.
        """
        for watcher in (self._open_task, self._acceptance_task):
            if watcher:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
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
        self.keep_awake.stop()
        await self.version_guard.stop()
        await self.deps.jira.close()

    async def run(self, once: bool = False) -> None:
        if not self.dry_run:
            if once:
                await self.deps.repo.ensure()
            elif not await self._wait_for_repo():
                return
        await self.reconcile()
        if self.open and not self.dry_run and not once:
            self._open_task = asyncio.create_task(self._watch_open(), name="open-sessions")
        if not self.dry_run and not once:
            self._acceptance_task = asyncio.create_task(self._watch_acceptance(), name="acceptance")
        while not self.stop_event.is_set():
            delay = self.cfg.runtime.poll_seconds + random.uniform(0, self.cfg.runtime.poll_jitter_seconds)
            try:
                await self._tick(once)
                self._error_backoff = 0.0
            except Exception as exc:
                if once:
                    raise
                delay = await self._loop_error(exc)
            if once:
                if self.sessions:
                    await asyncio.wait([s.task for s in self.sessions.values()])
                break
            delay = max(delay, self.backoff_until - asyncio.get_running_loop().time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)

    async def _tick(self, once: bool) -> None:
        self._check_code()
        self.keep_awake.update(bool(self.sessions))
        await self.version_guard.tick()
        if self.record.claude_unavailable is not None:
            await self._check_claude()
        elif not self.record.dispatch_paused:
            await self.poll_once()
        elif not once:
            self.emit(console.line("dispatch paused; existing sessions continue"))
        if not self.dry_run and not once:
            await self._clean_expired()

    async def _loop_error(self, exc: Exception) -> float:
        """An unexpected error in a poll: log and alert it, then carry on after a pause.

        Running sessions are separate tasks and are not affected. Stopping here would leave
        every ticket of this developer waiting until someone noticed and restarted it.
        """
        log.exception("the coordinator hit an internal error; carrying on")
        self._error_backoff = _backoff(self._error_backoff)
        what = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
        self.emit(
            console.line(
                f"internal error ({what}); carrying on in {self._error_backoff:.0f}s. "
                "`coordinator logs` shows the details"
            )
        )
        await self.alerts.send(
            f"loop-error:{type(exc).__name__}",
            "internal error",
            f"{what}. It carries on in {self._error_backoff:.0f}s; `coordinator logs` shows the details.",
        )
        return self._error_backoff

    async def _clean_expired(self) -> None:
        """Once a day, remove local logs and worktrees older than runtime.retention_days."""
        days = self.cfg.runtime.retention_days
        loop = asyncio.get_running_loop()
        if not days or loop.time() < self._clean_at:
            return
        self._clean_at = loop.time() + CLEAN_EVERY_SECONDS
        keep = set(self.sessions) | set(self.busy)
        try:
            removed = await cleanup.clean_expired(self.cfg, self.deps.repo, days, keep, log.info)
        except Exception:
            log.exception("removing old local logs failed; trying again tomorrow")
            return
        if removed:
            self.emit(
                console.line(
                    f"removed local logs and worktrees of {removed} finished more than {days} days ago "
                    "(runtime.retention_days)"
                )
            )

    async def _wait_for_repo(self) -> bool:
        """Clone or fetch the application repository, retrying with the poll backoff until it works.

        Until then nothing is reconciled, dispatched or watched; the control socket still answers.
        False if the supervisor was asked to stop first.
        """
        self.waiting_for_repo = True
        delay = 0.0
        while not self.stop_event.is_set():
            ensure = asyncio.create_task(self.deps.repo.ensure(), name="repository")
            stopping = asyncio.create_task(self.stop_event.wait())
            try:
                await asyncio.wait({ensure, stopping}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopping.cancel()
                if not ensure.done():  # stopping mid-clone or mid-fetch ends its git process group
                    ensure.cancel()
                    await asyncio.wait({ensure})
            if ensure.cancelled():
                break
            try:
                ensure.result()
            except (GitError, IntegrationError, ProcessStartError, OSError) as exc:
                delay = _backoff(delay)
                reason = " ".join(str(exc).split())
                self.emit(
                    console.line(
                        f"could not reach the application repository ({reason}); retrying in {delay:.0f}s"
                    )
                )
            else:
                self.waiting_for_repo = False
                return True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        return False

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

    async def _watch_acceptance(self) -> None:
        """Tickets in Acceptance review: Jira every poll, the running apps every few seconds."""
        while not self.stop_event.is_set():
            try:
                await self.acceptance.tick()
            except Exception:
                log.exception("acceptance check failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=OPEN_SESSION_TICK_SECONDS)

    # ------------------------------------------------------------------ discovery
    async def poll_once(self) -> PollReport:
        report = PollReport()
        loop = asyncio.get_running_loop()
        if loop.time() < self.backoff_until:
            return report
        if not self.dry_run:
            # First, so that runs starting in this poll already read the new guidance. None of
            # these may ever stop the poll.
            for name, check in (
                ("cancelled tickets", self._close_cancelled),
                ("cancelled tickets' leftovers", self._tidy_cancelled),
                ("comments", self._sweep_comments),
                ("out-of-date candidates", self.staleness.tick),
                ("reminders", self.reminders.tick),
            ):
                try:
                    await check()
                except Exception:
                    log.exception("%s check failed", name)
        try:
            # First, so that a release recorded now starts its verification in this same poll.
            await self._record_merged_releases(report)
            issues = await self.deps.jira.search(ready_jql(self.cfg))
            working = await self.deps.jira.search(active_jql(self.cfg))
        except IntegrationError as exc:
            # Back off the Jira request stream only; running sessions are unaffected.
            self.backoff_seconds = _backoff(self.backoff_seconds)
            self.backoff_until = loop.time() + self.backoff_seconds
            report.error = str(exc)
            self.record = self.record.model_copy(update={"last_poll_error": str(exc)[:500]})
            self.deps.store.save_supervisor(self.record)
            self.emit(console.line(f"poll failed ({exc}); retrying in {self.backoff_seconds:.0f}s"))
            return report
        self.backoff_seconds = 0.0
        await self._update_room()
        if not self.dry_run:
            await self._resume_due(report)
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

    async def _record_merged_releases(self, report: PollReport) -> None:
        """Tickets in Ready for release whose PR has been merged: record the release.

        The human merge is the pilot release. The coordinator only reads it from GitHub (it never
        merges or deploys) and chooses Record release, which a human would otherwise do after
        copying the merge commit into a comment. Release verification then validates the release
        approval and provenance as usual. A RECORD RELEASE comment, if a human left one, wins.
        """
        for issue in await self.deps.jira.search(release_jql(self.cfg)):
            key = issue.key
            if key in self.corrupt or key in self.busy:
                continue
            try:
                ctx = await load_context(self.deps.jira, self.cfg, key)
                intake = await self.evaluator.merged_release(ctx)
                if intake.kind is not IntakeKind.READY or intake.release_record is None:
                    report.waiting.append({"ticket": key, "reason": intake.reason})
                    self._note_once(f"{key}:release:{intake.reason}", f"{key}: {intake.reason}")
                    continue
                release = intake.release_record
                what = f"PR #{release['merged_pr']} merged as {str(release['commit'])[:12]}"
                if self.dry_run:
                    self.emit(f"[dry-run] would record the release of {key}: {what}")
                    continue
                entry = intake.entry.history_id if intake.entry else str(release["commit"])[:12]
                journal = RunJournal(self.cfg.runtime.state_dir / "intake" / key)
                pub = Publisher(self.cfg, self.deps.jira, None, None, journal, f"release-{entry}")
                await pub.transition(key, Status.READY_RELEASE, Action.RECORD_RELEASE)
                self.emit(console.line(f"{key}: {what}; recorded the release, verifying it"))
            except (
                IntegrationError,
                RecordCorrupt,
                PublicationError,
                PublicationUncertain,
                TicketMoved,
            ) as exc:
                report.skipped.append({"ticket": key, "reason": f"release not recorded: {exc}"})
                self._note_once(
                    f"{key}:release-error:{exc}",
                    f"{key}: could not record the release ({exc}); retrying on the next poll, or "
                    "choose Record release in Jira",
                )

    async def _sweep_comments(self) -> None:
        """Comments on the developer's tickets that ask for something outside any stage:
        `FOR CLAUDE project` notes go to the project guidance (delivery.guidance) and
        `CREATE TICKETS` creates proposed tickets (delivery.proposals).

        Only tickets changed since the last look are read again. A failure is retried on the
        next poll and never holds anything else up.
        """
        try:
            issues = await self.deps.jira.search(mine_jql(self.cfg))
        except IntegrationError:
            return
        notes_from = {self.cfg.identity.developer_jira_account_id, *self.cfg.approvals.jira_account_ids}
        deciders = self.cfg.approvals.approvers() | {self.cfg.identity.developer_jira_account_id}
        recent = utcnow() - timedelta(days=COMMENTS_DONE_DAYS)
        for issue in issues:
            stamp = issue.updated.isoformat() if issue.updated else ""
            if stamp and self._comments_seen.get(issue.key) == stamp:
                continue
            done = self.cfg.status_by_id().get(issue.view.status_id) is Status.DONE
            if done and (issue.updated is None or issue.updated < recent):
                continue
            try:
                found = await self.deps.jira.comments(issue.key)
                added = await self.guidance.collect(issue.key, found, notes_from)
                created = await self.proposals.collect(issue, found, deciders)
            except Exception as exc:  # never let these hold up the poll
                log.warning("comments on %s not acted on: %s", issue.key, exc)
                self._note_once(
                    f"{issue.key}:comments:{exc}",
                    f"{issue.key}: a FOR CLAUDE project or CREATE TICKETS comment was not acted on yet "
                    f"({exc}); retrying",
                )
                continue
            self._comments_seen[issue.key] = stamp
            if added:
                self.emit(
                    console.line(
                        f"{issue.key}: added {len(added)} FOR CLAUDE project note"
                        f"{'s' if len(added) != 1 else ''} to the project guidance; every session reads it"
                    )
                )
            if created:
                self.emit(console.line(f"{issue.key}: created {', '.join(created)} in Backlog, as asked"))

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

    @property
    def launch_hold(self) -> str | None:
        """Why new sessions wait for now (running ones carry on), or None."""
        return self.room or self.version_guard.hold

    async def _update_room(self) -> None:
        rt = self.cfg.runtime
        root = self.cfg.repository.worktree_root
        room = short_of_room(root, rt.min_free_disk_gb, rt.hold_on_memory_pressure)
        if room and room != self.room:
            self.emit(console.line(f"new sessions wait: {room}; running sessions carry on"))
            await self.alerts.send("room", "short of room", f"New sessions wait: {room}.")
        elif self.room and not room:
            self.emit(console.line("there is room again; new sessions start"))
        self.room = room

    def _latest_runs(self) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for e in self.deps.store.iter_runs():
            r = e.record
            if e.error is not None or r is None:
                continue
            prev = latest.get(r.ticket_key)
            if prev is None or r.created_at >= prev.record.created_at:
                latest[r.ticket_key] = e
        return latest

    async def _tidy_cancelled(self) -> None:
        """Remove what cancelled tickets left in the repository (see delivery.cancel_cleanup).

        Runs for every cancelled run not yet tidied, however it was cancelled. Jira is asked again
        first, so a ticket moved back out of Cancelled is left alone. A failure is logged and
        tried again on the next poll.
        """
        todo = {
            key: e
            for key, e in self._latest_runs().items()
            if e.record.state is RunState.CANCELLED
            and not e.record.tidied
            and key not in self.sessions
            and key not in self.busy
        }
        cancelled = self.cfg.workflow.statuses.get(Status.CANCELLED)
        if not todo or not cancelled:
            return
        keys = sorted(todo)
        for i in range(0, len(keys), 50):
            chunk = keys[i : i + 50]
            for issue in await self.deps.jira.search(f"key in ({', '.join(chunk)}) AND status = {cancelled}"):
                entry = todo[issue.key]
                try:
                    what = await cancel_cleanup.tidy(self.deps, entry, self.emit)
                except Exception as exc:
                    log.warning("tidying cancelled %s failed; will try again: %s", issue.key, exc)
                    continue
                rec = entry.record
                entry.journal.save(rec.model_copy(update={"tidied": True}), "cancel_tidied", what=what)
                self.emit(console.line(f"{issue.key}: cancelled; {what}"))

    async def _close_cancelled(self) -> None:
        """Close the runs of tickets that were cancelled in Jira after the run stopped.

        A run that finished waiting for a person (a review, answers, a blocker) is left that way
        in the journal, and so in the office, when someone cancels the ticket afterwards: nothing
        publishes, so nothing records it. Ask Jira about those tickets and close their latest run.
        A ticket with a session working on it is left to that session, which sees the cancel.
        """
        latest = self._latest_runs()
        waiting = {
            key: e
            for key, e in latest.items()
            if (
                e.record.state in WAITING_STATES or (e.record.state is RunState.INTERRUPTED and e.record.held)
            )
            and key not in self.sessions
            and key not in self.busy
        }
        cancelled = self.cfg.workflow.statuses.get(Status.CANCELLED)
        if not waiting or not cancelled:
            return
        keys = sorted(waiting)
        for i in range(0, len(keys), 50):
            chunk = keys[i : i + 50]
            found = await self.deps.jira.search(f"key in ({', '.join(chunk)}) AND status = {cancelled}")
            for issue in found:
                entry = waiting[issue.key]
                rec = entry.record
                assert rec is not None
                if self.open:
                    for session in self.open.registry.for_ticket(issue.key):
                        await self.open.close(session, "the ticket was cancelled")
                self.claims.release(issue.key, rec.run_id)
                entry.journal.save(
                    rec.model_copy(
                        update={
                            "state": RunState.CANCELLED,
                            "held": False,
                            "hold_reason": "",
                            "reason": "cancelled in Jira",
                            "next_action": "None (cancelled).",
                            "ended_at": rec.ended_at or utcnow(),
                        }
                    ),
                    "cancelled",
                )
                self.emit(
                    console.line(f"{issue.key}: cancelled in Jira; its {rec.stage.value} run is closed")
                )

    async def _resume_due(self, report: PollReport) -> None:
        """Resume runs that stopped for a reason that passes by itself, once their time comes.

        Claude's API or the network was down, Jira or GitHub could not be reached, a publication's
        outcome was uncertain, or the ticket was edited while Claude worked (``RETRY``). Runs that
        are held, wait for Claude's login or usage limit, or are corrupt are left alone.
        """
        now = utcnow()
        for entry in self.deps.store.unfinished():
            rec = entry.record
            if entry.error is not None or rec is None or rec.held:
                continue
            key = rec.ticket_key
            if key in self.sessions or key in self.busy or key in self.corrupt:
                continue
            if rec.outputs.get(WAITING_FOR_CLAUDE) or rec.state not in RETRYABLE:
                continue
            due = (rec.outputs.get(RETRY) or {}).get("due")
            if due and datetime.fromisoformat(due) > now:
                report.waiting.append({"ticket": key, "reason": rec.reason or "retrying later"})
                continue
            publishing = rec.state is RunState.PUBLISHING or bool(entry.journal.pending_ops())
            if self.launch_hold and not publishing:
                report.waiting.append({"ticket": key, "reason": f"new sessions wait: {self.launch_hold}"})
                continue
            try:
                action = await self._recover_run(entry.journal, rec)
            except Exception as exc:
                action = f"could not resume yet: {exc}"
            self.emit(console.line(f"{key}: {action}"))

    def _note_once(self, marker: str, message: str) -> None:
        if marker not in self._noted:
            self._noted.add(marker)
            self.emit(console.line(message))

    async def consider(
        self, ctx: TicketContext, stage: Stage, report: PollReport, adopt: bool = False
    ) -> None:
        key = ctx.key
        dev = self.open.registry.development(key) if self.open and stage is Stage.VERIFICATION else None
        if dev is not None:
            # Each change asked for in that session is pushed as a new candidate. Verify once,
            # after the developer has finished with it, rather than once per change.
            report.waiting.append({"ticket": key, "reason": VERIFY_AFTER_CLOSE})
            self._note_once(
                f"{key}:{dev.name}:verification",
                f"{key}: verification and review wait until you close its development session "
                f"(type /exit in it, or `delivery close {key}`)",
            )
            return
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
        if (
            stage is Stage.DEVELOPMENT
            and intake.kind is IntakeKind.READY
            and self.cfg.flow.kind_of(ctx.issue.view.issue_type) == "spike"
        ):
            await self._complete_spike(ctx, intake, report)
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
        if (hold := self.launch_hold) is not None:
            report.waiting.append({"ticket": key, "reason": f"new sessions wait: {hold}"})
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
                # A resolution works in the blocked stage's working copy: close that stage's session.
                held = intake.resume_stage if stage is Stage.RESOLUTION and intake.resume_stage else stage
                await self.open.close_for_run(key, held)
            self._launch(rc)
        finally:
            self.busy.discard(key)
        report.started.append(key)

    async def _complete_spike(self, ctx: TicketContext, intake: Intake, report: PollReport) -> None:
        """A spike's findings were accepted: there is nothing to build, so close it (Complete spike)
        instead of starting development. Without that transition in Jira, say so once."""
        key = ctx.key
        entry = intake.entry.history_id if intake.entry else "-"
        if self.dry_run:
            report.started.append(key)
            self.emit(f"[dry-run] would complete spike {key}: {intake.reason}")
            return
        if f"{key}:{entry}" in self._spikes_done:
            return
        rec = intake.record or ctx.record
        plan = approved_gate(rec.gates, GateKind.PLAN)
        if plan is None:
            return
        path, _, commit = rec.artefacts.get("plan", "").rpartition("@")
        url = blob_url(self.cfg.repository.url, commit, path) if path else self.cfg.repository.url
        journal = RunJournal(self.cfg.runtime.state_dir / "intake" / key)
        pub = Publisher(self.cfg, self.deps.jira, None, None, journal, f"spike-{entry}")
        try:
            name = self.cfg.workflow.action_name(Action.COMPLETE_SPIKE)
            done = self.cfg.status_id(Status.DONE)
            offered = any(
                t.name == name and t.to_status_id == done for t in await self.deps.jira.transitions(key)
            )
            proposals = await load_proposals(self.deps.repo, key, plan.token)
            shared = rec.model_copy(
                update={
                    "current_state": RunState.COMPLETED,
                    "current_stage": Stage.PLANNING,
                    "updated_at": utcnow(),
                }
            )
            await pub.save_record(key, shared, "spike-done")
            await pub.comment(
                key,
                "spike-done",
                comment_text.spike_done(plan.revision, url, plan.token, proposals, offered),
                plan.token,
            )
            if offered:
                await pub.transition(key, Status.READY_DEVELOPMENT, Action.COMPLETE_SPIKE)
        except (IntegrationError, PublicationError, PublicationUncertain, TicketMoved) as exc:
            report.skipped.append({"ticket": key, "reason": f"spike not completed yet: {exc}"})
            return
        self._spikes_done.add(f"{key}:{entry}")
        report.started.append(key)
        self.emit(
            console.line(
                f"{key}: spike complete (findings v{plan.revision:03d} accepted)"
                + ("" if offered else "; move it to Done by hand (no Complete spike transition in Jira)")
            )
        )

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
                        "next_action": f"`coordinator recover {rc.key} --resume` continues it; "
                        "`coordinator logs` shows the error.",
                    }
                )
                rc.save("internal_error", error=str(exc), traceback=traceback.format_exc()[-6000:])
                await self.executor.notice(
                    rc,
                    "internal-error",
                    comment_text.internal_error(
                        rc.record.stage.value, rc.run_id, self.cfg.identity.worker_id, rc.key
                    ),
                    operational=True,
                )
                await self.alerts.send(
                    f"internal-error:{rc.run_id}",
                    f"{rc.key} stopped: internal error",
                    f"{rc.key} {rc.record.stage.value}: {exc}"[:300]
                    + f". `coordinator recover {rc.key} --resume` continues it.",
                )
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
        task.add_done_callback(self._after_run)
        self.sessions[rc.key] = Session(rc.key, rc, task)
        self.keep_awake.update(True)
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

    def _after_run(self, task: asyncio.Task[RunRecord]) -> None:
        self.keep_awake.update(bool(self.sessions))
        if task.cancelled() or task.exception() is not None:
            return
        rec = task.result()
        waiting = rec.outputs.get(WAITING_FOR_CLAUDE)
        if rec.state is RunState.INTERRUPTED and not rec.held and waiting:
            self._claude_down(rec.ticket_key, str(waiting.get("kind")), str(waiting.get("detail")))

    # ------------------------------------------------------------------ Claude unavailable
    def _claude_down(self, key: str, kind: str, detail: str) -> None:
        first = self.record.claude_unavailable is None
        if first:
            self._claude_wait = 60.0 if kind == "auth" else 300.0
            loop = asyncio.get_running_loop()
            self._claude_check_at = loop.time() + self._claude_wait
            self.record = self.record.model_copy(
                update={
                    "claude_unavailable": {
                        "kind": kind,
                        "detail": detail[:300],
                        "since": utcnow().isoformat(),
                        "next_check": (utcnow() + timedelta(seconds=self._claude_wait)).isoformat(),
                    }
                }
            )
            self.deps.store.save_supervisor(self.record, "claude_unavailable", kind=kind, ticket=key)
            self.emit(console.claude_unavailable(self.cfg, kind, detail, key, int(self._claude_wait)))
            why = "Claude Code is not signed in" if kind == "auth" else "the Claude usage limit is reached"
            alert = loop.create_task(
                self.alerts.send(
                    f"claude:{kind}",
                    "waiting for Claude",
                    f"{why} ({key}). Work waits and continues by itself once Claude works again.",
                )
            )
            self._background.add(alert)
            alert.add_done_callback(self._background.discard)
        else:
            self.emit(console.line(f"{key} also waits for Claude; it continues when Claude works again"))

    def waiting_for_claude(self) -> list[tuple[RunJournal, RunRecord]]:
        out = []
        for e in self.deps.store.unfinished():
            r = e.record
            if r and r.state is RunState.INTERRUPTED and not r.held and r.outputs.get(WAITING_FOR_CLAUDE):
                out.append((e.journal, r))
        return out

    async def _check_claude(self) -> None:
        loop = asyncio.get_running_loop()
        if loop.time() < self._claude_check_at:
            return
        ok, detail = await claude_works(self.cfg.claude.executable, self.cfg.claude.model)
        if not ok:
            self._claude_wait = min(self._claude_wait * 2 or 60.0, 900.0)
            self._claude_check_at = loop.time() + self._claude_wait
            info = dict(self.record.claude_unavailable or {})
            info["next_check"] = (utcnow() + timedelta(seconds=self._claude_wait)).isoformat()
            self.record = self.record.model_copy(update={"claude_unavailable": info})
            self.deps.store.save_supervisor(self.record)
            self.emit(
                console.line(
                    f"Claude still unavailable ({detail[:120]}); "
                    f"next check in {int(self._claude_wait // 60)} min"
                )
            )
            return
        waiting = self.waiting_for_claude()
        self.record = self.record.model_copy(update={"claude_unavailable": None})
        self.deps.store.save_supervisor(self.record, "claude_available", resuming=len(waiting))
        self.emit(console.claude_back([r.ticket_key for _, r in waiting]))
        for journal, rec in waiting:
            if rec.ticket_key in self.sessions:
                continue
            try:
                action = await self._recover_run(journal, rec)
            except Exception as exc:
                action = f"could not resume yet: {exc}"
            self.emit(console.line(f"{rec.ticket_key}: {action}"))

    def _check_code(self) -> None:
        """Say once when the code on disk is newer than the code this supervisor runs."""
        started = self.record.code_mtime
        if self._code_noted or started is None or code_mtime() <= started + 1:
            return
        self._code_noted = True
        self.emit(console.code_changed())

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
            # The fresh session reads the ticket as it is now (it may have been edited meanwhile),
            # so the run's input identity follows it.
            brief = digest(brief_text(ctx.issue))
            rc.record = rec.model_copy(
                update={
                    "state": RunState.RUNNING,
                    "held": False,
                    "hold_reason": "",
                    "brief_digest": brief,
                    "input_revision": digest(intake.material(brief)),
                    "selected_comment_ids": [c.id for c in intake.selected],
                }
            )
            rc.record.outputs.pop(WAITING_FOR_CLAUDE, None)
            rc.save("resumed")
            if self.open:
                await self.open.close_for_run(rec.ticket_key, rec.stage)
            self._launch(rc, resume=True)
            return "resuming interrupted work with a fresh session"
        self.claims.release(rec.ticket_key, rec.run_id)
        status = ctx.status.value if ctx.status else "unmapped"
        if ctx.status is Status.CANCELLED:
            journal.save(
                rec.model_copy(
                    update={"state": RunState.CANCELLED, "reason": "cancelled in Jira", "held": False}
                ),
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
                "new_sessions_wait": self.launch_hold,
                "claude_unavailable": self.record.claude_unavailable,
                "corrupt": self.corrupt,
                "worker_id": self.cfg.identity.worker_id,
            }
        if cmd == "shutdown":
            self.emit(console.line("stopping (asked by `coordinator stop`)"))
            self.stop_event.set()
            return {"ok": True, "running_sessions": len(self.sessions)}
        if cmd in ("pause", "resume"):
            paused = cmd == "pause"
            self.record = self.record.model_copy(
                update={"dispatch_paused": paused, "pause_reason": str(req.get("reason", ""))}
            )
            self.deps.store.save_supervisor(self.record, f"dispatch_{cmd}")
            return {"ok": True, "dispatch_paused": paused, "running_sessions": len(self.sessions)}
        if cmd in ("poll", "recover") and self.waiting_for_repo:
            return {"ok": False, "error": "waiting for the application repository; nothing starts until then"}
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


def _backoff(seconds: float) -> float:
    """The wait after another failed Jira poll or repository fetch: 15s, doubling to 10 minutes."""
    return min(max(seconds * 2, BACKOFF_MIN_SECONDS), BACKOFF_MAX_SECONDS)


TERMINAL = frozenset(
    {RunState.AWAITING_HUMAN, RunState.COMPLETED, RunState.FAILED, RunState.BLOCKED, RunState.CANCELLED}
)
# Runs that wait for a person (or have stopped for good) and so can outlive their ticket.
WAITING_STATES = frozenset({RunState.AWAITING_HUMAN, RunState.BLOCKED, RunState.FAILED})
OPEN_SESSION_TICK_SECONDS = 3.0
# Done tickets changed within this many days are still read for CREATE TICKETS comments.
COMMENTS_DONE_DAYS = 30
VERIFY_AFTER_CLOSE = "its development session is still open; verification starts once it is closed"
BACKOFF_MIN_SECONDS = 15.0
BACKOFF_MAX_SECONDS = 600.0
CLEAN_EVERY_SECONDS = 24 * 3600.0
# States of a run that the poll resumes once its retry time comes.
RETRYABLE = frozenset({RunState.INTERRUPTED, RunState.PUBLISHING})

__all__ = ["JournalCorrupt", "LockHeld", "PollReport", "Supervisor"]
