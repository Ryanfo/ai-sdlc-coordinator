"""Acceptance review: the code-approved candidate, ready to try.

Acceptance is the product decision: does the change do what the brief asked for? When one of
the developer's tickets enters Acceptance review, the coordinator:

* posts a "Ready for acceptance" comment: how to try the change locally (the app running on
  this machine, ``delivery try <ticket>`` on anyone else's) and a link to the acceptance guide
  verification wrote (how to check each criterion by hand). Once per entry into Acceptance review;
* with ``[preview]`` configured (``acceptance = true``, the default) and tmux installed, runs the
  app from the exact candidate that was code-approved, in a worktree of its own
  (``<worktree_root>/<ticket>/acceptance-c<n>/app``) and a tmux session of its own
  (``<ticket>-acceptance``), and opens the browser on it (delivery.preview).

The app stops and its worktree goes once the ticket leaves Acceptance review (accepted, changes
requested or moved). State lives in ``<state_dir>/acceptance`` so a restarted coordinator picks
the running app up again. Nothing here makes a decision or moves a ticket.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import Field

from delivery import comments, console
from delivery.git import GitError, blob_url
from delivery.intake import RecordCorrupt, TicketContext, load_context
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.models import Model, SharedExecutionRecord, utcnow
from delivery.ownership import acceptance_jql
from delivery.ports import IntegrationError
from delivery.preview import ACCEPTANCE, Previews, PreviewState, open_url
from delivery.publication import PublicationError, PublicationUncertain, Publisher
from delivery.runtime import Deps
from delivery.workflow import Status

log = logging.getLogger("delivery")
FOLDER = "acceptance"


class AcceptanceState(Model):
    ticket_key: str
    entry: str  # the status change that brought the ticket into Acceptance review
    candidate_sha: str
    candidate_number: int
    session_dir: str  # the app's folder: launcher script, log, restart requests
    worktree: str
    announced: bool = False
    opened_at: datetime = Field(default_factory=utcnow)
    preview: PreviewState | None = None


class AcceptanceStore:
    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir / FOLDER

    def load(self, key: str) -> AcceptanceState | None:
        try:
            return AcceptanceState.model_validate_json((self.dir / f"{key}.json").read_text())
        except (OSError, ValueError):
            return None

    def save(self, st: AcceptanceState) -> None:
        ensure_private_dir(self.dir)
        atomic_write_json(self.dir / f"{st.ticket_key}.json", st)

    def remove(self, key: str) -> None:
        (self.dir / f"{key}.json").unlink(missing_ok=True)

    def all(self) -> list[AcceptanceState]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(AcceptanceState.model_validate_json(p.read_text()))
            except (OSError, ValueError) as exc:
                log.warning("unreadable acceptance record %s: %s", p, exc)
        return out


class Acceptance:
    """Watches the developer's tickets in Acceptance review (see the module docstring)."""

    def __init__(
        self,
        deps: Deps,
        emit: Callable[[str], None],
        opener: Callable[[str], Awaitable[str | None]] = open_url,
    ) -> None:
        self.deps = deps
        self.cfg = deps.cfg
        self.emit = emit
        self.store = AcceptanceStore(self.cfg.runtime.state_dir)
        self.apps = Previews(deps, emit, opener, procedure=ACCEPTANCE)
        self._noted: set[str] = set()
        self._last_full: datetime | None = None

    @property
    def runs_app(self) -> bool:
        pc = self.cfg.preview
        return pc.enabled and pc.acceptance and self.apps.tmux.available()

    def _note(self, marker: str, message: str) -> None:
        if marker not in self._noted:
            self._noted.add(marker)
            self.emit(console.line(message))

    async def tick(self, *, full: bool | None = None) -> None:
        """Check Jira every poll interval; watch the running apps on every call."""
        now = utcnow()
        if full is None:
            full = self._last_full is None or now - self._last_full >= timedelta(
                seconds=self.cfg.runtime.poll_seconds
            )
        if full:
            self._last_full = now
            try:
                issues = await self.deps.jira.search(acceptance_jql(self.cfg))
            except IntegrationError as exc:
                log.warning("acceptance check skipped: %s", exc)
                issues = None
            if issues is not None:
                here = {i.key for i in issues}
                for st in self.store.all():
                    if st.ticket_key not in here:
                        await self.finish(st, "it left Acceptance review")
                for issue in issues:
                    try:
                        await self._review(issue.key)
                    except (
                        IntegrationError,
                        RecordCorrupt,
                        PublicationError,
                        PublicationUncertain,
                        GitError,
                    ) as exc:
                        self._note(
                            f"{issue.key}:{exc}",
                            f"{issue.key}: acceptance review not prepared yet ({exc}); retrying",
                        )
        if self.runs_app:
            for st in self.store.all():
                try:
                    if await self.apps.tick(st, lambda: True):
                        self.store.save(st)
                except Exception as exc:  # an app must never stop the coordinator
                    log.exception("acceptance app of %s", st.ticket_key)
                    self._note(f"{st.ticket_key}:app:{exc}", f"{st.ticket_key}: the app failed: {exc}")

    async def _review(self, key: str) -> None:
        ctx = await load_context(self.deps.jira, self.cfg, key)
        rec = ctx.record
        entry = ctx.latest_entry(self.cfg.status_id(Status.ACCEPTANCE_REVIEW))
        if entry is None or not rec.candidate_sha:
            return
        st = self.store.load(key)
        if st and (st.entry != entry.history_id or st.candidate_sha != rec.candidate_sha):
            await self.finish(st, "a new acceptance review started")
            st = None
        if st is None:
            st = AcceptanceState(
                ticket_key=key,
                entry=entry.history_id,
                candidate_sha=rec.candidate_sha,
                candidate_number=rec.candidate_number,
                session_dir=str(self.store.dir / key),
                worktree=str(
                    self.cfg.repository.worktree_root / key / f"acceptance-c{rec.candidate_number}" / "app"
                ),
            )
            self.store.save(st)
        if not st.announced:
            await self._announce(ctx, st)
            st.announced = True
            self.store.save(st)
        if self.runs_app and st.preview is None and not Path(st.worktree).is_dir():
            await self.deps.repo.fetch()
            await self.deps.repo.add_worktree(Path(st.worktree), start=st.candidate_sha)

    async def _announce(self, ctx: TicketContext, st: AcceptanceState) -> None:
        key, rec = ctx.key, ctx.record
        repo_url = self.cfg.repository.url.removesuffix(".git")
        guide_url = await self._guide_url(rec)
        journal = RunJournal(self.cfg.runtime.state_dir / "intake" / key)
        pub = Publisher(self.cfg, self.deps.jira, None, None, journal, f"acceptance-{st.entry}")
        await pub.comment(
            key,
            "acceptance",
            comments.acceptance_ready(
                key,
                rec.candidate_number,
                f"{repo_url}/pull/{rec.pr_number}" if rec.pr_number else repo_url,
                worker_id=self.cfg.identity.worker_id,
                local_app=self.runs_app,
                try_command=self.cfg.preview.enabled,
                guide_url=guide_url,
                proposal=self.cfg.release.proposal,
            ),
            f"c{rec.candidate_number}",
        )
        self.emit(
            console.line(
                f"{key}: in Acceptance review; posted how to try candidate c{rec.candidate_number}"
                + (" and starting the app" if self.runs_app else "")
            )
        )

    async def _guide_url(self, rec: SharedExecutionRecord) -> str | None:
        ref = rec.artefacts.get("acceptance_guide")
        if not ref or "@" not in ref:
            return None
        path, commit = ref.rsplit("@", 1)
        if await self.deps.repo.show_file(commit, path) is None:
            return None
        return blob_url(self.cfg.repository.url, commit, path)

    async def finish(self, st: AcceptanceState, reason: str) -> None:
        """Stop the app and remove its worktree."""
        running = st.preview is not None and st.preview.state != "stopped"
        await self.apps.stop(st)
        wt = Path(st.worktree)
        if wt.exists():
            try:
                await self.deps.repo.remove_worktree(wt)
                if wt.parent.is_dir() and not any(wt.parent.iterdir()):
                    wt.parent.rmdir()
            except (GitError, OSError) as exc:
                log.warning("could not remove %s: %s", wt, exc)
        self.store.remove(st.ticket_key)
        if running:
            self.emit(console.line(f"{st.ticket_key}: stopped the acceptance app ({reason})"))


__all__ = ["Acceptance", "AcceptanceState", "AcceptanceStore"]
