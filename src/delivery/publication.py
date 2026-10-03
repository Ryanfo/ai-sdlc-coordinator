"""Reconcilable publication of external side effects.

Each operation has a stable ID derived from the run, the operation type and the
revision. The journal records the intent before the request and the confirmed result
after it. An uncertain operation (intent without result) is reconciled by querying the
remote system for the operation marker before anything is sent again: comment and PR
creation and transitions never receive blind retries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from delivery.adf import markdown_to_adf
from delivery.config import Config
from delivery.feedback import MARKER_PREFIX
from delivery.git import BranchDiverged, ManagedRepo, markers
from delivery.journal import OpStatus, RunJournal
from delivery.models import PROPERTY_KEY, SharedExecutionRecord
from delivery.ports import (
    GitHubPort,
    IntegrationError,
    JiraPort,
    PullRequest,
    UncertainResult,
)
from delivery.workflow import Action, IllegalTransition, Status, coordinator_route


@dataclass(frozen=True)
class Posted:
    """A published comment with Jira's own creation time (never the local clock)."""

    id: str
    created: datetime


def op_id(run_id: str, op_type: str, revision: str = "") -> str:
    return hashlib.sha256(f"{run_id}|{op_type}|{revision}".encode()).hexdigest()[:20]


def marker_line(oid: str, run_id: str) -> str:
    return f"`{MARKER_PREFIX} {oid} · run {run_id}`"


class PublicationError(Exception):
    """A definite failure: nothing was published, or the remote refused it."""


class PublicationUncertain(Exception):
    """The remote may or may not have applied the operation. Reconcile before retrying."""


class TicketMoved(Exception):
    """The ticket is no longer in the status the coordinator expected (human action)."""

    def __init__(self, key: str, expected: Status, actual: Status | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{key} is in {actual.value if actual else 'an unmapped status'}, expected {expected.value}"
        )


@dataclass
class Publisher:
    cfg: Config
    jira: JiraPort
    github: GitHubPort | None
    repo: ManagedRepo | None
    journal: RunJournal
    run_id: str

    # ------------------------------------------------------------------ generic
    def _state(self, oid: str) -> OpStatus:
        return self.journal.op_state(oid).status

    def _result(self, oid: str) -> dict[str, Any]:
        return self.journal.op_state(oid).result or {}

    # ------------------------------------------------------------------ Jira comments
    async def _find_comment(self, key: str, oid: str) -> Posted | None:
        for c in await self.jira.comments(key):
            if f"{MARKER_PREFIX} {oid}" in c.body_text:
                return Posted(c.id, c.created)
        return None

    async def comment(self, key: str, op_type: str, markdown: str, revision: str = "") -> Posted:
        oid = op_id(self.run_id, f"comment:{op_type}", revision)
        st = self._state(oid)
        if st is OpStatus.CONFIRMED:
            res = self._result(oid)
            return Posted(str(res.get("comment_id")), datetime.fromisoformat(str(res.get("created"))))
        if st is OpStatus.INTENDED:
            found = await self._find_comment(key, oid)
            if found:
                self.journal.confirm(
                    oid, {"comment_id": found.id, "created": found.created.isoformat(), "reconciled": True}
                )
                return found
        body = markdown_to_adf(f"{markdown}\n\n{marker_line(oid, self.run_id)}")
        self.journal.intend(oid, "jira_comment", {"key": key, "op_type": op_type})
        try:
            created = await self.jira.add_comment(key, body)
        except UncertainResult as exc:
            self.journal.fail(oid, str(exc), definite=False)
            raise PublicationUncertain(f"comment {op_type} on {key}: {exc}") from exc
        except IntegrationError as exc:
            if exc.retryable:
                self.journal.fail(oid, str(exc), definite=False)
                raise PublicationUncertain(f"comment {op_type} on {key}: {exc}") from exc
            self.journal.fail(oid, str(exc), definite=True)
            raise PublicationError(f"comment {op_type} on {key}: {exc}") from exc
        self.journal.confirm(oid, {"comment_id": created.id, "created": created.created.isoformat()})
        return Posted(created.id, created.created)

    # ------------------------------------------------------------------ Jira transitions
    async def transition(
        self,
        key: str,
        source: Status,
        action: Action,
        revision: str = "",
        fields: dict[str, Any] | None = None,
    ) -> None:
        route = coordinator_route(source, action)  # raises IllegalTransition
        target = route.target
        oid = op_id(self.run_id, f"transition:{action.value}", revision)
        if self._state(oid) is OpStatus.CONFIRMED:
            return
        by_id = self.cfg.status_by_id()
        issue = await self.jira.get_issue(key)
        current = by_id.get(issue.view.status_id)
        if current is target and self._state(oid) is OpStatus.INTENDED:
            self.journal.confirm(oid, {"reconciled": True, "status": target.value})
            return
        if current is not source:
            raise TicketMoved(key, source, current)
        target_id = self.cfg.status_id(target)
        name = self.cfg.workflow.action_name(action)
        options = [
            t for t in await self.jira.transitions(key) if t.to_status_id == target_id and t.name == name
        ]
        if not options:
            options = [t for t in await self.jira.transitions(key) if t.to_status_id == target_id]
            if len(options) != 1:
                raise PublicationError(
                    f"{key}: no unambiguous transition {name!r} to {target.value} is available "
                    f"(found {len(options)}); check the workflow with `delivery workflow inspect`"
                )
        if len(options) > 1:
            raise PublicationError(f"{key}: transition {name!r} to {target.value} is ambiguous")
        self.journal.intend(
            oid,
            "jira_transition",
            {
                "key": key,
                "from": source.value,
                "to": target.value,
                "transition_id": options[0].id,
            },
        )
        try:
            await self.jira.do_transition(key, options[0].id, fields)
        except (UncertainResult, IntegrationError) as exc:
            definite = (
                isinstance(exc, IntegrationError)
                and not isinstance(exc, UncertainResult)
                and not exc.retryable
            )
            # Refetch: the transition may have happened even though the response was lost.
            try:
                now = by_id.get((await self.jira.get_issue(key)).view.status_id)
            except IntegrationError:
                now = None
            if now is target:
                self.journal.confirm(oid, {"reconciled": True, "status": target.value})
                return
            self.journal.fail(oid, str(exc), definite=definite and now is source)
            if definite and now is source:
                raise PublicationError(f"transition {action.value} on {key}: {exc}") from exc
            raise PublicationUncertain(f"transition {action.value} on {key}: {exc}") from exc
        self.journal.confirm(oid, {"status": target.value})

    # ------------------------------------------------------------------ idempotent writes
    async def save_record(self, key: str, record: SharedExecutionRecord, label: str) -> None:
        """PUT is idempotent, so a retry after uncertainty is safe; still journaled."""
        oid = op_id(self.run_id, f"property:{label}")
        compact = record.compacted()
        self.journal.intend(oid, "jira_property", {"key": key, "label": label})
        try:
            await self.jira.set_property(key, PROPERTY_KEY, compact.model_dump(mode="json"))
        except UncertainResult as exc:
            self.journal.fail(oid, str(exc), definite=False)
            raise PublicationUncertain(f"property {label} on {key}: {exc}") from exc
        except IntegrationError as exc:
            self.journal.fail(oid, str(exc), definite=not exc.retryable)
            raise PublicationError(f"property {label} on {key}: {exc}") from exc
        self.journal.confirm(oid, {"bytes": len(compact.encoded())})

    async def set_resume_field(self, key: str, value: str | None, label: str) -> None:
        field_id = self.cfg.jira.fields.resume_stage
        if not field_id:
            return
        oid = op_id(self.run_id, f"field:{label}")
        self.journal.intend(oid, "jira_field", {"key": key, "field": field_id, "value": value})
        try:
            await self.jira.set_fields(key, {field_id: {"value": value} if value else None})
        except UncertainResult as exc:
            self.journal.fail(oid, str(exc), definite=False)
            raise PublicationUncertain(f"resume field on {key}: {exc}") from exc
        except IntegrationError as exc:
            self.journal.fail(oid, str(exc), definite=not exc.retryable)
            raise PublicationError(f"resume field on {key}: {exc}") from exc
        self.journal.confirm(oid, {"value": value})

    # ------------------------------------------------------------------ Git
    async def commit_and_push(
        self,
        worktree: Path,
        branch: str,
        op_type: str,
        message: str,
        revision: str = "",
        paths: list[str] | None = None,
    ) -> str | None:
        """Commit with an operation marker and fast-forward push. Returns the commit SHA."""
        assert self.repo is not None
        oid = op_id(self.run_id, f"git:{op_type}", revision)
        marker = f"Delivery-Op: {oid}"
        st = self._state(oid)
        if st is OpStatus.CONFIRMED:
            sha = self._result(oid).get("sha")
            return str(sha) if sha else None
        if st is OpStatus.INTENDED:
            await self.repo.fetch()
            found = await self.repo.find_commit_with_marker(branch, marker)
            if found:
                self.journal.confirm(oid, {"sha": found, "reconciled": True})
                return found
            head = await self.repo.worktree_head(worktree)
            details = await self.repo.commit_details(head)
            if marker in details.message:
                # Committed locally but not on the remote: pushing the same commit is safe.
                await self._push(worktree, branch, oid)
                self.journal.confirm(oid, {"sha": head, "reconciled": True})
                return head
        self.journal.intend(oid, "git_push", {"branch": branch, "op_type": op_type})
        sha = await self.repo.commit(worktree, f"{message}\n\n{markers(self.run_id, oid)}", paths=paths)
        if sha is None:
            self.journal.confirm(oid, {"sha": None, "empty": True})
            return None
        await self._push(worktree, branch, oid)
        self.journal.confirm(oid, {"sha": sha})
        return sha

    async def push_head(self, worktree: Path, branch: str, op_type: str, revision: str = "") -> str:
        """Fast-forward push of commits already in the worktree (a merge of the base branch)."""
        assert self.repo is not None
        head = await self.repo.worktree_head(worktree)
        if await self.repo.ls_remote(branch) == head:
            return head
        oid = op_id(self.run_id, f"git:{op_type}", revision)
        if self._state(oid) is OpStatus.CONFIRMED:
            return str(self._result(oid).get("sha") or head)
        self.journal.intend(oid, "git_push", {"branch": branch, "op_type": op_type})
        await self._push(worktree, branch, oid)
        self.journal.confirm(oid, {"sha": head})
        return head

    async def _push(self, worktree: Path, branch: str, oid: str) -> None:
        assert self.repo is not None
        try:
            await self.repo.push(worktree, branch)
        except BranchDiverged as exc:
            self.journal.fail(oid, str(exc), definite=True)
            raise
        except UncertainResult as exc:
            self.journal.fail(oid, str(exc), definite=False)
            raise PublicationUncertain(f"push of {branch}: {exc}") from exc

    # ------------------------------------------------------------------ GitHub PR
    async def ensure_pr(self, head: str, base: str, title: str, body: str, revision: str) -> PullRequest:
        assert self.github is not None
        oid = op_id(self.run_id, "github:pr", revision)
        marked_body = f"{body}\n\n<!-- {MARKER_PREFIX} {oid} run {self.run_id} -->"
        st = self._state(oid)
        existing = [
            p
            for p in await self.github.find_prs(head, state="all")
            if p.head_ref == head and p.base_ref == base
        ]
        open_prs = [p for p in existing if p.state == "open"]
        if st is OpStatus.CONFIRMED and open_prs:
            return open_prs[0]
        if open_prs:
            pr = open_prs[0]
            if pr.title != title or f"{MARKER_PREFIX} {oid}" not in pr.body:
                pr = await self.github.update_pr(pr.number, title, marked_body)
            self.journal.confirm(oid, {"number": pr.number, "url": pr.url, "attached": True})
            return pr
        if any(p.merged for p in existing):
            raise PublicationError(f"a PR from {head} was already merged; open a new ticket")
        self.journal.intend(oid, "github_pr", {"head": head, "base": base})
        try:
            pr = await self.github.create_pr(head, base, title, marked_body)
        except UncertainResult as exc:
            self.journal.fail(oid, str(exc), definite=False)
            raise PublicationUncertain(f"PR for {head}: {exc}") from exc
        except IntegrationError as exc:
            self.journal.fail(oid, str(exc), definite=not exc.retryable)
            raise PublicationError(f"PR for {head}: {exc}") from exc
        self.journal.confirm(oid, {"number": pr.number, "url": pr.url})
        return pr


__all__ = [
    "IllegalTransition",
    "PublicationError",
    "PublicationUncertain",
    "Publisher",
    "TicketMoved",
    "marker_line",
    "op_id",
]
