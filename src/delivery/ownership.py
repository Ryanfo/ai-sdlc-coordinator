"""Ownership: supervisor exclusivity, per-ticket claims, repository locks, eligibility.

* One OS-backed supervisor lock per Jira site + developer identity (all projects).
* One claim per ticket while a state-mutating stage owns it. This is a correctness
  rule, not a session limit: any number of different tickets can be claimed at once.
* A short repository-operation lock protects shared Git metadata (worktree creation,
  ref updates, commits, pushes). It never spans a Claude execution.

None of these are distributed locks. A second machine running the same identity is an
unsupported configuration that doctor reports when it can see the conflicting marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import socket
from collections.abc import AsyncIterator, Container, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delivery.config import Config
from delivery.journal import ensure_private_dir
from delivery.models import utcnow
from delivery.workflow import Stage, Status, stage_for_ready


class LockHeld(Exception):
    def __init__(self, path: Path, holder: dict[str, Any]) -> None:
        self.path = path
        self.holder = holder
        super().__init__(f"lock {path.name} is held by {holder or 'another process'}")


class FileLock:
    """Non-reentrant exclusive ``flock`` with holder metadata written into the file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def read_holder(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text() or "{}")
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def acquire(self, metadata: dict[str, Any] | None = None, blocking: bool = False) -> None:
        if self._fd is not None:
            raise RuntimeError(f"{self.path} already held by this object")
        ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            raise LockHeld(self.path, self.read_holder()) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": utcnow().isoformat(),
            **(metadata or {}),
        }
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(payload, sort_keys=True).encode(), 0)
        os.fsync(fd)

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.ftruncate(fd, 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextlib.contextmanager
    def hold(self, metadata: dict[str, Any] | None = None) -> Iterator[FileLock]:
        self.acquire(metadata)
        try:
            yield self
        finally:
            self.release()


def supervisor_lock(cfg: Config) -> FileLock:
    return FileLock(cfg.runtime.state_dir / "locks" / f"supervisor-{cfg.identity_key}.lock")


@dataclass
class TicketClaims:
    """Per-ticket claims held by this supervisor for the lifetime of a run."""

    locks_dir: Path
    _held: dict[str, tuple[str, FileLock]] = field(default_factory=dict)

    def claim(self, ticket_key: str, run_id: str) -> bool:
        if ticket_key in self._held:
            return False
        lock = FileLock(self.locks_dir / "tickets" / f"{ticket_key}.lock")
        try:
            lock.acquire({"ticket": ticket_key, "run_id": run_id})
        except LockHeld:
            return False
        self._held[ticket_key] = (run_id, lock)
        return True

    def release(self, ticket_key: str, run_id: str) -> None:
        held = self._held.get(ticket_key)
        if held and held[0] == run_id:
            held[1].release()
            del self._held[ticket_key]

    def holder(self, ticket_key: str) -> str | None:
        held = self._held.get(ticket_key)
        return held[0] if held else None

    def claimed(self) -> dict[str, str]:
        return {k: v[0] for k, v in self._held.items()}


class RepoLocks:
    """Short critical-section locks for shared repository metadata.

    In-process tasks queue on an asyncio lock; other processes on this machine (for
    example a second identity's supervisor sharing a clone) are excluded by ``flock``.
    """

    def __init__(self, locks_dir: Path) -> None:
        self.locks_dir = locks_dir
        self._async: dict[str, asyncio.Lock] = {}
        self.acquisitions = 0

    @contextlib.asynccontextmanager
    async def hold(self, repo_key: str, operation: str) -> AsyncIterator[None]:
        lock = self._async.setdefault(repo_key, asyncio.Lock())
        async with lock:
            flock = FileLock(self.locks_dir / "repos" / f"{repo_key}.lock")
            await asyncio.to_thread(flock.acquire, {"operation": operation}, True)
            self.acquisitions += 1
            try:
                yield
            finally:
                flock.release()


# --------------------------------------------------------------------------- eligibility


@dataclass(frozen=True)
class IssueView:
    key: str
    project_key: str
    issue_type: str
    status_id: str
    status_name: str
    assignee_account_id: str | None
    labels: tuple[str, ...] = ()
    resume_stage: str | None = None
    summary: str = ""


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    status: Status | None
    stage: Stage | None
    reasons: tuple[str, ...]


def evaluate_eligibility(issue: IssueView, cfg: Config, active_tickets: Container[str] = ()) -> Eligibility:
    """Cheap, deterministic filter. Inputs and approvals are validated separately."""
    reasons: list[str] = []
    status = cfg.status_by_id().get(issue.status_id)
    stage_def = stage_for_ready(status) if status else None
    if issue.project_key != cfg.jira.project_key:
        reasons.append(f"project {issue.project_key} is not {cfg.jira.project_key}")
    if issue.issue_type not in cfg.jira.supported_issue_types:
        reasons.append(f"issue type {issue.issue_type!r} is not supported")
    if issue.assignee_account_id != cfg.identity.developer_jira_account_id:
        reasons.append("assigned to another account" if issue.assignee_account_id else "unassigned")
    if cfg.jira.required_label and cfg.jira.required_label not in issue.labels:
        reasons.append(f"missing opt-in label {cfg.jira.required_label!r}")
    if status is None:
        reasons.append(f"status {issue.status_name!r} ({issue.status_id}) is not mapped")
    elif stage_def is None:
        reasons.append(f"status {status.value} is not a ready status")
    if issue.key in active_tickets:
        reasons.append("an execution for this ticket is already active")
    return Eligibility(
        eligible=not reasons,
        status=status,
        stage=stage_def.stage if stage_def else None,
        reasons=tuple(reasons),
    )


def _jql_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _assigned_jql(cfg: Config, statuses: Iterable[Status]) -> str:
    ids = sorted(cfg.workflow.statuses[s] for s in statuses if s in cfg.workflow.statuses)
    parts = [
        f"project = {_jql_str(cfg.jira.project_key)}",
        f"assignee = {_jql_str(cfg.identity.developer_jira_account_id)}",
        f"status in ({', '.join(ids)})",
        "issuetype in (" + ", ".join(_jql_str(t) for t in cfg.jira.supported_issue_types) + ")",
    ]
    if cfg.jira.required_label:
        parts.append(f"labels = {_jql_str(cfg.jira.required_label)}")
    return " AND ".join(parts) + " ORDER BY key ASC"


def ready_jql(cfg: Config) -> str:
    from delivery.workflow import READY_STATUSES

    return _assigned_jql(cfg, READY_STATUSES)


def active_jql(cfg: Config) -> str:
    """The developer's tickets in "agent working" statuses (to find ones moved there by hand)."""
    from delivery.workflow import ACTIVE_STATUSES

    return _assigned_jql(cfg, ACTIVE_STATUSES)


def release_jql(cfg: Config) -> str:
    """The developer's tickets in Ready for release (waiting for the human merge)."""
    return _assigned_jql(cfg, (Status.READY_RELEASE,))


def mine_jql(cfg: Config) -> str:
    """Every ticket assigned to the developer that is not cancelled (Done ones too: a spike's
    follow-up tickets can be created after it is done)."""
    return _assigned_jql(cfg, [s for s in Status if s is not Status.CANCELLED])


def waiting_jql(cfg: Config) -> str:
    """The developer's tickets waiting for a person: a review or decision, answers, a blocker
    to be resolved, or the merge."""
    from delivery.workflow import HUMAN_REVIEW_STATUSES, PAUSED_STATUSES

    return _assigned_jql(cfg, [*HUMAN_REVIEW_STATUSES, *PAUSED_STATUSES, Status.READY_RELEASE])


def review_jql(cfg: Config) -> str:
    """The developer's tickets whose candidate is under review (Code or Acceptance review)."""
    return _assigned_jql(cfg, (Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW))


def acceptance_jql(cfg: Config) -> str:
    """The developer's tickets in Acceptance review (the candidate is there to try)."""
    return _assigned_jql(cfg, (Status.ACCEPTANCE_REVIEW,))


def coordination_jql(cfg: Config) -> str:
    """All in-flight tickets in the project, any assignee, for overlap detection."""
    from delivery.workflow import TERMINAL_STATUSES

    excluded = {Status.BACKLOG, *TERMINAL_STATUSES}
    ids = sorted(sid for s, sid in cfg.workflow.statuses.items() if s not in excluded)
    return f"project = {_jql_str(cfg.jira.project_key)} AND status in ({', '.join(ids)}) ORDER BY key ASC"
