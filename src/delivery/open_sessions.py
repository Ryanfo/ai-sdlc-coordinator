"""Interactive sessions left open after hand-off: questions, follow-up changes, closing.

With ``[claude.interactive] keep_open``, a session that has handed a valid result to the
coordinator stays open in tmux (delivery.interactive) so the developer can keep asking Claude
about the work. The coordinator carries on with the ticket meanwhile. This module tracks those
sessions in ``<state_dir>/open-sessions`` (so they survive a coordinator restart) and:

* publishes follow-up changes from development sessions. When Claude finishes a reply and the
  feature worktree differs from the published candidate, the coordinator (never Claude, which
  has no Git credentials) commits and pushes the change as the next candidate, supersedes the
  code and later approvals, comments on the ticket and moves it back to Ready for verification.
  It only does this while the ticket is in Ready for verification, Code review, Acceptance
  review or Changes requested and no run is working on it; otherwise the change waits;
* closes a session when it is ended in its window (``/exit``), after ``idle_close_hours`` with
  nothing happening, when a new run of the same stage starts for the ticket (a new development
  run needs the feature branch back), or when the ticket is done or cancelled. Closing keeps
  the conversation in the run's logs, keeps unpublished changes as a patch (or as the
  unfinished work the next development run continues from) and removes the worktree.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import Field

from delivery import comments, console
from delivery.git import BranchDiverged, GitError, commit_url
from delivery.intake import load_context
from delivery.interactive import discard_transcript
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.models import GateKind, Model, RunState, utcnow
from delivery.ports import IntegrationError
from delivery.publication import PublicationError, PublicationUncertain, Publisher, TicketMoved
from delivery.runtime import Deps
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


class SessionRegistry:
    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir / REGISTRY

    def save(self, rec: OpenRecord) -> None:
        ensure_private_dir(self.dir)
        atomic_write_json(self.dir / f"{rec.name}.json", rec)

    def remove(self, name: str) -> None:
        (self.dir / f"{name}.json").unlink(missing_ok=True)

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

    def held_worktrees(self, run_id: str) -> set[str]:
        return {w for r in self.all() if r.run_id == run_id for w in r.worktrees}

    def published(self, run_id: str, sha: str, candidate_number: int) -> None:
        """The run published its candidate: follow-ups in its open session start from it."""
        for r in self.all():
            if r.run_id == run_id and r.stage is Stage.DEVELOPMENT:
                self.save(r.model_copy(update={"base_sha": sha, "candidate_number": candidate_number}))


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
        if not await self.tmux.alive(rec.name):
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
        if (
            rec.stage is Stage.DEVELOPMENT
            and self.cfg.claude.interactive.follow_ups
            and (rec.unchecked_stops or (rec.held and full))
        ):
            await self.follow_up(rec)

    # ------------------------------------------------------------------ follow-ups
    async def follow_up(self, rec: OpenRecord) -> None:
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
            await self._publish(rec, ctx, base, head, changed)
        except BranchDiverged:
            self._note(rec, f"feature/{key} changed on GitHub; reconcile the branch by hand (no force push)")
        except (TicketMoved, PublicationUncertain, PublicationError, IntegrationError, GitError) as exc:
            self._note(rec, f"publishing failed, retried on the next poll ({exc})")
        finally:
            self.busy.discard(key)

    async def _publish(self, rec: OpenRecord, ctx: Any, base: str, head: str, changed: list[str]) -> None:
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
            key, "followup", comments.follow_up(n, sha, url, prompts, STATUS_NAMES[status], moved), f"c{n}"
        )
        if moved:
            await pub.transition(key, status, Action.SUBMIT_FOLLOW_UP, revision=f"c{n}")
        rec.followups.append({"candidate": n, "sha": sha, "files": len(changed), "at": utcnow().isoformat()})
        rec.base_sha, rec.candidate_number = sha, n
        rec.pending_prompts, rec.unchecked_stops, rec.held = [], 0, ""
        self.registry.save(rec)
        self.emit(
            console.follow_up_published(self.cfg, key, n, sha, len(changed), STATUS_NAMES[status], prompts)
        )
        await self.tmux.message(rec.name, f"Pushed as c{n}; {key} is Ready for verification again")

    # ------------------------------------------------------------------ closing
    async def close_for_run(self, key: str, stage: Stage) -> None:
        for rec in self.registry.for_ticket(key):
            if rec.stage is stage:
                await self.close(rec, f"a new {stage.value.replace('_', ' ')} run is starting")

    async def close(self, rec: OpenRecord, reason: str) -> None:
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
