"""Interactive sessions left open after hand-off: questions, follow-up changes, closing.

With ``[claude.interactive] keep_open``, a session that has handed a valid result to the
coordinator stays open in tmux (delivery.interactive) so the developer can keep asking Claude
about the work. The coordinator carries on with the ticket meanwhile, except that it does not
verify a candidate while its development session is open (the supervisor holds the ticket in
Ready for verification): every change asked for there is a new candidate, and verifying each
one would repeat the whole review. This module tracks those sessions in
``<state_dir>/open-sessions`` (so they survive a coordinator restart) and:

* publishes follow-up changes from development sessions. When Claude finishes a reply and the
  feature worktree differs from the published candidate, the coordinator (never Claude, which
  has no Git credentials) commits and pushes the change as the next candidate, supersedes the
  code and later approvals, comments on the ticket and moves it back to Ready for verification.
  It only does this while the ticket is in Ready for verification, Code review, Acceptance
  review or Changes requested and no run is working on it; otherwise the change waits. When the
  session is ended, a reply not yet published is published before it closes, so the candidate
  verified next includes it;
* publishes follow-up changes from specification and plan sessions. When
  Claude finishes a reply and the document in its output directory has changed, the
  coordinator publishes it as the next revision for review, through the stage's own
  publication: the new gate supersedes the one under review and the ticket stays in its review
  status. It only does this while the ticket is in that review status with this session's
  revision under review; otherwise the change waits;
* runs the app from a development session's worktree once its run has ended, so the developer
  can try the change in a browser (delivery.preview);
* closes a session when it is ended in its window (``/exit``), after ``idle_close_hours`` with
  nothing happening, when a new run of the same stage starts for the ticket (a new development
  run needs the feature branch back), or when the ticket is done or cancelled. Closing keeps
  the conversation in the run's logs, keeps unpublished changes as a patch (or as the
  unfinished work the next development run continues from) and removes the worktree.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import Field

from delivery import comments, console
from delivery.gates import current_gate, gate_token
from delivery.git import BranchDiverged, GitError, commit_url
from delivery.intake import Intake, TicketContext, load_context
from delivery.interactive import discard_transcript, open_window
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.models import (
    ACTIVE_RUN_STATES,
    GateKind,
    GateRecord,
    GateState,
    Model,
    RunState,
    digest,
    utcnow,
)
from delivery.ports import IntegrationError
from delivery.preview import Previews, PreviewState
from delivery.publication import PublicationError, PublicationUncertain, Publisher, TicketMoved
from delivery.runtime import Decision, Deps, RunContext
from delivery.session_hook import read_events
from delivery.tmux import for_config as tmux_for
from delivery.transcript import write_transcript
from delivery.workflow import (
    FOLLOW_UP_STATUSES,
    STATUS_NAMES,
    TERMINAL_STATUSES,
    Action,
    Stage,
    Status,
)

log = logging.getLogger("delivery")
REGISTRY = "open-sessions"


@dataclasses.dataclass(frozen=True)
class Document:
    """A document a stage publishes for review. Edits to it in the stage's open session are
    published as its next revision."""

    procedure: str
    title: str
    filename: str  # in the procedure's output directory
    gate: GateKind
    review: Status
    counter: str  # the shared record's field holding its latest revision number


DOCUMENTS: dict[Stage, Document] = {
    Stage.REFINEMENT: Document(
        "refine-ticket",
        "specification",
        "specification.md",
        GateKind.SPEC,
        Status.SPECIFICATION_REVIEW,
        "spec_revision",
    ),
    Stage.PLANNING: Document(
        "plan-ticket", "plan", "plan.md", GateKind.PLAN, Status.PLAN_REVIEW, "plan_revision"
    ),
}


# A spike's planning stage writes findings instead of a plan; they are reviewed the same way.
FINDINGS = Document(
    "investigate-ticket", "findings", "findings.md", GateKind.PLAN, Status.PLAN_REVIEW, "plan_revision"
)


def document_for(stage: Stage, procedure: str | None = None) -> Document | None:
    """The document a stage's procedure publishes for review (None: it publishes none)."""
    if procedure == FINDINGS.procedure:
        return FINDINGS
    return DOCUMENTS.get(stage)


def document_digest(out_dir: Path, stage: Stage, procedure: str | None = None) -> str:
    """Fingerprint of the stage's document in an output directory ("" if there is none)."""
    doc = document_for(stage, procedure)
    if doc is None:
        return ""
    try:
        return digest((out_dir / doc.filename).read_bytes())
    except OSError:
        return ""


@dataclasses.dataclass(frozen=True)
class FollowUp:
    """A document edited in its stage's open session, published as the next revision."""

    prompts: list[str]
    replaces: str  # the gate token of the revision it supersedes


class FollowUpRefused(Exception):
    """The edited document cannot be published as things stand (the reason says why)."""


class OpenRecord(Model):
    name: str
    ticket_key: str
    run_id: str
    stage: Stage
    procedure: str
    worktree: str
    # Worktrees kept for this session; removed when it closes.
    worktrees: list[str] = Field(default_factory=list)
    journal_dir: str
    session_dir: str
    transcript: str = ""
    # Transcript lines already in the run log (claude-<procedure>.jsonl) at hand-off.
    mirrored_lines: int = 0
    opened_at: datetime = Field(default_factory=utcnow)
    last_activity_at: datetime = Field(default_factory=utcnow)
    events_seen: int = 0
    # What the developer asked for since the last published follow-up (for the commit message).
    pending_prompts: list[str] = Field(default_factory=list)
    unchecked_stops: int = 0
    base_sha: str | None = None
    candidate_number: int | None = None
    followups: list[dict[str, Any]] = Field(default_factory=list)
    held: str = ""
    # Document stages: the procedure's output directory, the fingerprint of its document as last
    # published, and the revision of it this session has under review.
    out_dir: str = ""
    document: str = ""
    revision: int | None = None
    # Development: the app running from this session's worktree (delivery.preview).
    preview: PreviewState | None = None


class SessionRegistry:
    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir / REGISTRY

    def save(self, rec: OpenRecord) -> None:
        ensure_private_dir(self.dir)
        atomic_write_json(self.dir / f"{rec.name}.json", rec)

    def remove(self, name: str) -> None:
        (self.dir / f"{name}.json").unlink(missing_ok=True)

    def load(self, name: str) -> OpenRecord | None:
        try:
            return OpenRecord.model_validate_json((self.dir / f"{name}.json").read_text())
        except (OSError, ValueError):
            return None

    def all(self) -> list[OpenRecord]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(OpenRecord.model_validate_json(p.read_text()))
            except (OSError, ValueError) as exc:
                log.warning("unreadable open-session record %s: %s", p, exc)
        return out

    def for_ticket(self, key: str) -> list[OpenRecord]:
        return [r for r in self.all() if r.ticket_key == key]

    def development(self, key: str) -> OpenRecord | None:
        """The ticket's open development session: verification waits until it has closed."""
        return next((r for r in self.for_ticket(key) if r.stage is Stage.DEVELOPMENT), None)

    def held_worktrees(self, run_id: str) -> set[str]:
        return {w for r in self.all() if r.run_id == run_id for w in r.worktrees}

    def published(
        self,
        run_id: str,
        *,
        sha: str | None = None,
        candidate_number: int | None = None,
        revision: int | None = None,
    ) -> None:
        """The run published its candidate (development) or its document for review: follow-ups
        in its open session start from it."""
        for r in self.all():
            if r.run_id != run_id:
                continue
            if r.stage is Stage.DEVELOPMENT and sha is not None:
                self.save(r.model_copy(update={"base_sha": sha, "candidate_number": candidate_number}))
            elif r.stage is not Stage.DEVELOPMENT and revision is not None:
                self.save(r.model_copy(update={"revision": revision}))


def save_conversation(rec: OpenRecord) -> Path | None:
    """Keep what was said after the hand-off with the run's logs (claude-<procedure>-after)."""
    if not rec.transcript or not Path(rec.transcript).is_file():
        return None
    lines = [ln for ln in Path(rec.transcript).read_text().splitlines() if ln.strip()]
    after = lines[rec.mirrored_lines :]
    if not after:
        return None
    out = Path(rec.journal_dir) / "logs" / f"claude-{rec.procedure}-after.jsonl"
    ensure_private_dir(out.parent)
    out.write_text("\n".join(after) + "\n")
    out.chmod(0o600)
    write_transcript(out)
    return out


class OpenSessions:
    """Watches the open sessions on behalf of the supervisor (see the module docstring)."""

    def __init__(
        self,
        deps: Deps,
        emit: Callable[[str], None],
        *,
        is_running: Callable[[str], bool],
        busy: set[str],
    ) -> None:
        self.deps = deps
        self.cfg = deps.cfg
        self.emit = emit
        self.is_running = is_running
        self.busy = busy
        self.registry = SessionRegistry(self.cfg.runtime.state_dir)
        self.tmux = tmux_for(self.cfg.claude.interactive, self.cfg.runtime.state_dir)
        self.previews = Previews(deps, emit)
        # Opens a terminal window on a session (None: never open windows).
        self.opener: Callable[[str, Path, str, list[str]], Any] | None = (
            open_window if self.cfg.claude.interactive.window != "none" else None
        )
        self.attach_wait = 5.0  # seconds for a newly opened window to attach
        self._noted: set[str] = set()
        self._last_full: datetime | None = None

    def _note(self, rec: OpenRecord, reason: str) -> None:
        if rec.held != reason:
            rec.held = reason
            self.registry.save(rec)
        marker = f"{rec.name}:{reason}"
        if marker not in self._noted:
            self._noted.add(marker)
            self.emit(
                console.line(f"{rec.ticket_key}: changes in the open {rec.procedure} session wait: {reason}")
            )

    async def surface(self, run_id: str, text: str) -> bool:
        """Put a run's open session in front of the developer: open a terminal window on it
        unless one is already attached, then show ``text`` on its status line."""
        shown = False
        for rec in self.registry.all():
            if rec.run_id != run_id or not await self.tmux.alive(rec.name):
                continue
            if self.opener is not None and not await self.tmux.has_clients(rec.name):
                title = f"{rec.ticket_key} {rec.procedure}"
                problem = await self.opener(
                    self.cfg.claude.interactive.window,
                    Path(rec.session_dir),
                    title,
                    self.tmux.attach_argv(rec.name),
                )
                if problem:
                    log.warning("could not open a window on %s: %s", rec.name, problem)
                for _ in range(int(self.attach_wait * 10)):  # it attaches a moment after opening
                    if await self.tmux.has_clients(rec.name):
                        break
                    await asyncio.sleep(0.1)
            await self.tmux.message(rec.name, text)
            shown = True
        return shown

    # ------------------------------------------------------------------ polling
    async def tick(self) -> None:
        now = utcnow()
        full = self._last_full is None or now - self._last_full >= timedelta(
            seconds=self.cfg.runtime.poll_seconds
        )
        if full:
            self._last_full = now
        for rec in self.registry.all():
            try:
                await self._tick_one(rec, full)
            except Exception as exc:
                log.exception("open session %s", rec.name)
                self._note(rec, f"internal error: {exc}")

    async def _tick_one(self, rec: OpenRecord, full: bool) -> None:
        # Read it again: a run publishing meanwhile may have recorded its candidate or revision.
        fresh = self.registry.load(rec.name)
        if fresh is None:
            return
        rec = fresh
        events = read_events(Path(rec.session_dir))
        new = events[rec.events_seen :]
        for ev in new:
            if ev.get("at"):
                rec.last_activity_at = datetime.fromisoformat(str(ev["at"]))
            if ev.get("event") == "prompt" and ev.get("prompt"):
                rec.pending_prompts.append(str(ev["prompt"]))
            if ev.get("event") == "stop":
                rec.unchecked_stops += 1
        if new:
            rec.events_seen = len(events)
            self.registry.save(rec)
        follow_ups = self.cfg.claude.interactive.follow_ups
        if not await self.tmux.alive(rec.name):
            # Publish what Claude finished since the last check before closing: the candidate
            # verified once a development session closes must include it.
            if follow_ups and (rec.unchecked_stops or rec.held):
                if self.is_running(rec.ticket_key) or rec.ticket_key in self.busy:
                    return
                await self._follow_up(rec, ended=True)
            await self.close(rec, "the session was ended")
            return
        limit = timedelta(hours=self.cfg.claude.interactive.idle_close_hours)
        if utcnow() - rec.last_activity_at > limit:
            await self.close(
                rec, f"nothing happened in it for {self.cfg.claude.interactive.idle_close_hours}h"
            )
            return
        if full:
            try:
                issue = await self.deps.jira.get_issue(rec.ticket_key)
            except IntegrationError:
                issue = None
            status = self.cfg.status_by_id().get(issue.view.status_id) if issue else None
            if status in TERMINAL_STATUSES:
                await self.close(rec, f"{rec.ticket_key} is {STATUS_NAMES[status]}")
                return
        if follow_ups and (rec.unchecked_stops or (rec.held and full)):
            await self._follow_up(rec)
        if (
            rec.stage is Stage.DEVELOPMENT
            and self.cfg.preview.enabled
            and await self.previews.tick(
                rec, lambda: self._run_ended(rec), lambda text: self.tmux.message(rec.name, text)
            )
        ):
            self.registry.save(rec)

    def _run_ended(self, rec: OpenRecord) -> bool:
        """The run that opened this session has finished (published, blocked or stopped)."""
        for e in self.deps.store.runs_for_ticket(rec.ticket_key):
            if e.run_id == rec.run_id:
                return e.record is None or e.record.state not in ACTIVE_RUN_STATES
        return True

    # ------------------------------------------------------------------ follow-ups
    async def _follow_up(self, rec: OpenRecord, ended: bool = False) -> None:
        if rec.stage is Stage.DEVELOPMENT:
            await self.follow_up(rec, ended)
        elif document_for(rec.stage, rec.procedure) is not None:
            await self.follow_up_document(rec)

    async def follow_up(self, rec: OpenRecord, ended: bool = False) -> None:
        """Push the session's changes as the next candidate (``ended``: the session has just
        closed, so verification of it starts now)."""
        key = rec.ticket_key
        if self.is_running(key) or key in self.busy:
            return  # checked again on the next tick, once that run has published
        wt = Path(rec.worktree)
        if not wt.is_dir():
            self._note(rec, f"its worktree {wt} no longer exists")
            return
        repo = self.deps.repo
        self.busy.add(key)
        try:
            if rec.base_sha is None:
                self._note(
                    rec,
                    "this session's run did not publish a candidate; resume development in Jira and "
                    "the next run continues from these changes",
                )
                return
            base = rec.base_sha
            head = await repo.worktree_head(wt)
            changed = await repo.changed_paths(wt, base)
            if not changed:
                rec.unchecked_stops, rec.pending_prompts, rec.held = 0, [], ""
                self.registry.save(rec)
                return
            ctx = await load_context(self.deps.jira, self.cfg, key)
            status = ctx.status
            if status not in FOLLOW_UP_STATUSES:
                where = STATUS_NAMES[status] if status else "an unmapped status"
                self._note(
                    rec,
                    f"{key} is in {where}; they are pushed once it is in Ready for verification, "
                    "Code review, Acceptance review or Changes requested",
                )
                return
            if ctx.issue.view.assignee_account_id != self.cfg.identity.developer_jira_account_id:
                self._note(rec, f"{key} is no longer assigned to you")
                return
            if ctx.record.candidate_sha != base:
                self._note(
                    rec,
                    f"{key}'s candidate is now c{ctx.record.candidate_number}, not this session's work; "
                    "close the session and work from the new candidate",
                )
                return
            from delivery.stages import is_protected

            protected = [p for p in changed if is_protected(p)]
            if protected:
                self._note(rec, f"protected paths cannot change in a feature ticket: {protected[:5]}")
                return
            await self._publish(rec, ctx, base, head, changed, ended)
        except BranchDiverged:
            self._note(rec, f"feature/{key} changed on GitHub; reconcile the branch by hand (no force push)")
        except (TicketMoved, PublicationUncertain, PublicationError, IntegrationError, GitError) as exc:
            self._note(rec, f"publishing failed, retried on the next poll ({exc})")
        finally:
            self.busy.discard(key)

    async def _publish(
        self, rec: OpenRecord, ctx: Any, base: str, head: str, changed: list[str], ended: bool
    ) -> None:
        from delivery.gates import gate_token, supersede_for_new_revision

        key, repo, wt = rec.ticket_key, self.deps.repo, Path(rec.worktree)
        status: Status = ctx.status
        n = ctx.record.candidate_number + 1
        if head != base:
            # Claude committed locally; the coordinator commits everything itself.
            await repo.git("reset", "--soft", base, cwd=wt)
        prompts = [" ".join(p.split())[:300] for p in rec.pending_prompts] or ["(no request recorded)"]
        journal = RunJournal(self.cfg.runtime.state_dir / "followups" / key / f"c{n}")
        ensure_private_dir(journal.dir)
        pub = Publisher(self.cfg, self.deps.jira, self.deps.github, repo, journal, f"{rec.run_id}-followup")
        message = f"{key}: follow-up c{n}\n\nAsked for in the open Claude session ({rec.procedure}):\n" + (
            "\n".join(f"- {p}" for p in prompts)
        )
        sha = await pub.commit_and_push(wt, f"feature/{key}", "feature", message, revision=f"c{n}")
        if sha is None:
            sha = await repo.worktree_head(wt)
        shared = ctx.record
        fp_ref = dict(shared.footprint_ref or {})
        fp_ref.update(
            {
                "actual_paths": sorted(set(fp_ref.get("actual_paths") or []) | set(changed))[:200],
                "candidate_sha": sha,
            }
        )
        shared = shared.model_copy(
            update={
                "candidate_number": n,
                "candidate_sha": sha,
                "gates": supersede_for_new_revision(
                    shared.gates, GateKind.CODE, gate_token(key, GateKind.CODE, n)
                ),
                "pending_feedback": [],
                "pause": None,
                "footprint_ref": fp_ref,
                "current_run_id": rec.run_id,
                "current_stage": Stage.DEVELOPMENT,
                "current_state": RunState.COMPLETED,
                "updated_at": utcnow(),
                "history": [
                    *shared.history[-30:],
                    {
                        "run": rec.run_id,
                        "stage": "development",
                        "followup": f"c{n}",
                        "at": utcnow().isoformat(),
                    },
                ],
            }
        )
        await pub.save_record(key, shared, f"followup-c{n}")
        url = commit_url(self.cfg.repository.url, sha)
        moved = status is not Status.READY_VERIFICATION
        await pub.comment(
            key,
            "followup",
            comments.follow_up(n, sha, url, prompts, STATUS_NAMES[status], moved, ended=ended),
            f"c{n}",
        )
        if moved:
            await pub.transition(key, status, Action.SUBMIT_FOLLOW_UP, revision=f"c{n}")
        rec.followups.append({"candidate": n, "sha": sha, "files": len(changed), "at": utcnow().isoformat()})
        rec.base_sha, rec.candidate_number = sha, n
        rec.pending_prompts, rec.unchecked_stops, rec.held = [], 0, ""
        self.registry.save(rec)
        self.emit(
            console.follow_up_published(
                self.cfg, key, n, sha, len(changed), STATUS_NAMES[status], prompts, ended=ended
            )
        )
        await self.tmux.message(rec.name, f"Pushed as c{n}; verification starts when you /exit this session")

    # ------------------------------------------------------------------ document follow-ups
    async def follow_up_document(self, rec: OpenRecord) -> None:
        """Publish the session's edited specification or plan for review."""
        key = rec.ticket_key
        if self.is_running(key) or key in self.busy:
            return
        doc = document_for(rec.stage, rec.procedure)
        assert doc is not None
        current = document_digest(Path(rec.out_dir), rec.stage, rec.procedure)
        if not current or current == rec.document:
            rec.unchecked_stops, rec.pending_prompts, rec.held = 0, [], ""
            self.registry.save(rec)
            return
        self.busy.add(key)
        try:
            if rec.revision is None:
                self._note(
                    rec,
                    f"this session's run did not publish a {doc.title} for review, so there is no "
                    "revision to follow up",
                )
                return
            ctx = await load_context(self.deps.jira, self.cfg, key)
            if ctx.status is not doc.review:
                where = STATUS_NAMES[ctx.status] if ctx.status else "an unmapped status"
                self._note(
                    rec,
                    f"{key} is in {where}; a changed {doc.title} is published only while the ticket is in "
                    f"{STATUS_NAMES[doc.review]}",
                )
                return
            if ctx.issue.view.assignee_account_id != self.cfg.identity.developer_jira_account_id:
                self._note(rec, f"{key} is no longer assigned to you")
                return
            gate = current_gate(ctx.record.gates, doc.gate)
            if gate is None or gate.revision != rec.revision or gate.state is not GateState.PENDING:
                self._note(
                    rec,
                    f"{key}'s {doc.title} under review is no longer v{rec.revision:03d} from this session; "
                    "close the session and request changes in Jira instead",
                )
                return
            await self._publish_document(rec, ctx, gate)
        except FollowUpRefused as exc:
            self._note(rec, str(exc))
        except BranchDiverged:
            self._note(rec, f"delivery/{key} changed on GitHub; reconcile the branch by hand (no force push)")
        except (TicketMoved, PublicationUncertain, PublicationError, IntegrationError, GitError) as exc:
            self._note(rec, f"publishing failed, retried on the next poll ({exc})")
        finally:
            self.busy.discard(key)

    async def _publish_document(self, rec: OpenRecord, ctx: TicketContext, gate: GateRecord) -> None:
        """Publish through the stage's own publication, as a follow-up of the session's run.

        The follow-up has its own journal (``followups/<ticket>/<stage>-v<rev>``) and run ID
        (``<run>-followup-v<rev>``), a copy of the session's output directory, and a worktree of
        its own on the delivery branch, removed afterwards. Until it is recorded in Jira the
        revision number stays the same, so a retry after a failure repeats the same operations.
        """
        from delivery.stages import STRATEGIES

        key, doc = rec.ticket_key, document_for(rec.stage, rec.procedure)
        assert doc is not None
        entry = next((e for e in self.deps.store.runs_for_ticket(key) if e.run_id == rec.run_id), None)
        raw = entry.record.outputs.get("decision") if entry and entry.record else None
        if entry is None or entry.record is None or not raw:
            raise FollowUpRefused(f"the record of run {rec.run_id} is not available")
        decision = Decision.model_validate(raw)
        prompts = [" ".join(p.split())[:300] for p in rec.pending_prompts] or ["(no request recorded)"]
        rev = max(int(getattr(ctx.record, doc.counter)), gate.revision) + 1
        fid = f"{rec.run_id}-followup-v{rev}"
        journal = RunJournal(self.cfg.runtime.state_dir / "followups" / key / f"{rec.stage.value}-v{rev:03d}")
        ensure_private_dir(journal.dir)
        out = journal.dir / "output" / doc.procedure
        shutil.copytree(rec.out_dir, out, dirs_exist_ok=True)
        current = document_digest(out, rec.stage, rec.procedure)
        record = entry.record.model_copy(update={"run_id": fid, "worktrees": {}, "outputs": {}})
        intake = Intake.restore(entry.record.outputs.get("intake") or {"stage": rec.stage.value}, ctx)
        rc = RunContext(self.deps, ctx, intake, record, journal, ctx.record)
        strategy = STRATEGIES[rec.stage](rc)
        strategy.follow_up = FollowUp(prompts, gate.token)
        try:
            await strategy.publish(await strategy.follow_up_decision(decision, rev))
        finally:
            await strategy.cleanup()
        token = gate_token(key, doc.gate, rev)
        rec.followups.append({"revision": rev, "token": token, "at": utcnow().isoformat()})
        rec.revision, rec.document = rev, current
        rec.pending_prompts, rec.unchecked_stops, rec.held = [], 0, ""
        self.registry.save(rec)
        self.emit(
            console.document_follow_up_published(
                self.cfg, key, rec.stage, doc.title, rev, token, gate.token, prompts
            )
        )
        await self.tmux.message(rec.name, f"Published as {doc.title} v{rev:03d} ({token}) for review")

    # ------------------------------------------------------------------ closing
    async def close_for_run(self, key: str, stage: Stage) -> None:
        for rec in self.registry.for_ticket(key):
            if rec.stage is stage:
                await self.close(rec, f"a new {stage.value.replace('_', ' ')} run is starting")

    async def close(self, rec: OpenRecord, reason: str) -> None:
        await self.previews.stop(rec)
        await self.tmux.kill(rec.name)
        try:
            save_conversation(rec)
        except OSError as exc:
            log.warning("could not keep the conversation of %s: %s", rec.name, exc)
        discard_transcript(rec.transcript)
        kept = ""
        if rec.stage is Stage.DEVELOPMENT:
            try:
                kept = await self._keep_changes(rec)
            except (GitError, OSError) as exc:
                kept = f"unpublished changes could not be saved: {exc}"
        for path in rec.worktrees:
            try:
                await self.deps.repo.remove_worktree(Path(path))
            except (GitError, OSError) as exc:
                log.warning("could not remove worktree %s: %s", path, exc)
        self.registry.remove(rec.name)
        self.emit(console.open_session_closed(rec, reason, kept))

    async def _keep_changes(self, rec: OpenRecord) -> str:
        """Save changes that were never published, so closing never loses work."""
        wt = Path(rec.worktree)
        if not wt.is_dir():
            return ""
        repo = self.deps.repo
        entry = next(
            (e for e in self.deps.store.runs_for_ticket(rec.ticket_key) if e.run_id == rec.run_id), None
        )
        wip = entry.record.outputs.get("wip") if entry and entry.record else None
        if rec.base_sha is None and wip and wip.get("start_sha"):
            # The run stopped before publishing (blocked or questions): refresh its unfinished
            # work so that the next development run continues from what you have now.
            assert entry is not None
            assert entry.record is not None
            start = str(wip["start_sha"])
            files = await repo.changed_paths(wt, start)
            if not files:
                return ""
            await repo.git("add", "-A", cwd=wt)
            patch = entry.journal.dir / "wip" / "changes.patch"
            ensure_private_dir(patch.parent)
            await repo.git("diff", "--cached", "--binary", f"--output={patch}", start, cwd=wt)
            patch.chmod(0o600)
            record = entry.record
            record.outputs["wip"] = {**wip, "files": files}
            entry.journal.save(record, "wip_refreshed", files=len(files), source="open session")
            return f"{len(files)} changed files kept; the next development run continues from them"
        base = rec.base_sha or await repo.worktree_head(wt)
        files = await repo.changed_paths(wt, base)
        if not files:
            return ""
        await repo.git("add", "-A", cwd=wt)
        patch = Path(rec.session_dir) / "unpublished.patch"
        await repo.git("diff", "--cached", "--binary", f"--output={patch}", base, cwd=wt)
        patch.chmod(0o600)
        (Path(rec.session_dir) / "unpublished.json").write_text(json.dumps({"base": base, "files": files}))
        return f"{len(files)} unpublished changed files saved to {patch}"
