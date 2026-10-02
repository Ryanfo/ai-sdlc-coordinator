"""Integration boundaries. Real adapters and test fakes implement these protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from delivery.ownership import IssueView


class IntegrationError(Exception):
    """Base error for an external system."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AuthError(IntegrationError):
    """Authentication or permission failure. Surfaced promptly, never retried blindly."""


class NotFound(IntegrationError):
    pass


class UncertainResult(IntegrationError):
    """The request may or may not have taken effect (timeout, reset after send).

    The caller must reconcile against the remote system before any retry.
    """


# --------------------------------------------------------------------------- Jira


@dataclass(frozen=True)
class JiraUser:
    account_id: str
    display_name: str
    active: bool = True
    account_type: str = "atlassian"


@dataclass(frozen=True)
class JiraComment:
    id: str
    author_account_id: str
    created: datetime
    updated: datetime
    body_text: str
    author_name: str = ""
    body_adf: dict[str, Any] | None = None

    @property
    def edited(self) -> bool:
        return self.updated > self.created


@dataclass(frozen=True)
class StatusChange:
    history_id: str
    author_account_id: str | None
    created: datetime
    from_id: str
    to_id: str
    from_name: str = ""
    to_name: str = ""


@dataclass(frozen=True)
class JiraTransition:
    id: str
    name: str
    to_status_id: str
    to_status_name: str = ""


@dataclass(frozen=True)
class IssueLink:
    link_type: str
    direction: str  # "inward" or "outward" relative to this issue
    description: str  # e.g. "is blocked by", "blocks"
    other_key: str


@dataclass(frozen=True)
class Attachment:
    """Metadata of a file attached to a Jira issue (images pasted into a description too)."""

    id: str
    filename: str
    mime_type: str
    size: int
    created: datetime | None = None
    author_account_id: str | None = None


@dataclass(frozen=True)
class JiraIssue:
    view: IssueView
    description_text: str = ""
    created: datetime | None = None
    updated: datetime | None = None
    links: tuple[IssueLink, ...] = ()
    resolution: str | None = None
    assignee_name: str = ""
    attachments: tuple[Attachment, ...] = ()

    @property
    def key(self) -> str:
        return self.view.key


@dataclass(frozen=True)
class JiraStatusInfo:
    id: str
    name: str
    category: str


@dataclass(frozen=True)
class JiraFieldInfo:
    id: str
    name: str
    custom: bool
    schema_type: str = ""


class JiraPort(Protocol):
    async def myself(self) -> JiraUser: ...
    async def search(self, jql: str) -> list[JiraIssue]: ...
    async def get_issue(self, key: str) -> JiraIssue: ...
    async def comments(self, key: str) -> list[JiraComment]: ...
    async def status_changes(self, key: str) -> list[StatusChange]: ...
    async def transitions(self, key: str) -> list[JiraTransition]: ...
    async def do_transition(
        self, key: str, transition_id: str, fields: dict[str, Any] | None = None
    ) -> None: ...
    async def add_comment(self, key: str, adf: dict[str, Any]) -> JiraComment: ...
    async def get_property(self, key: str, name: str) -> dict[str, Any] | None: ...
    async def set_property(self, key: str, name: str, value: dict[str, Any]) -> None: ...
    async def set_fields(self, key: str, fields: dict[str, Any]) -> None: ...
    async def download_attachment(self, attachment_id: str, dest: Path, max_bytes: int) -> int: ...
    async def project_statuses(self, project_key: str) -> list[JiraStatusInfo]: ...
    async def fields(self) -> list[JiraFieldInfo]: ...
    async def user(self, account_id: str) -> JiraUser | None: ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- GitHub


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    visibility: str
    default_branch: str
    can_push: bool
    can_admin: bool
    allow_merge_commit: bool = True
    allow_squash_merge: bool = True
    allow_rebase_merge: bool = True


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str  # open / closed
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    base_sha: str
    author_login: str
    title: str = ""
    body: str = ""
    merge_commit_sha: str | None = None
    merged_at: datetime | None = None
    merged_by: str | None = None


@dataclass(frozen=True)
class Review:
    id: int
    user_login: str
    state: str  # APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED / PENDING
    commit_id: str
    submitted_at: datetime | None
    user_type: str = "User"


@dataclass(frozen=True)
class CheckRun:
    id: int
    name: str
    head_sha: str
    status: str  # queued / in_progress / completed
    conclusion: str | None
    app_slug: str
    html_url: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class CommitStatus:
    id: int
    context: str
    state: str  # error / failure / pending / success
    creator_login: str
    target_url: str = ""
    description: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class BranchProtection:
    required_approving_reviews: int = 0
    dismiss_stale_reviews: bool = False
    require_last_push_approval: bool = False
    required_checks: tuple[str, ...] = ()
    strict_up_to_date: bool = False
    enforce_admins: bool = False
    allow_force_pushes: bool = True
    allow_deletions: bool = True
    source: str = "branch_protection"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    parents: tuple[str, ...]
    tree_sha: str
    message: str = ""


class GitHubPort(Protocol):
    async def viewer_login(self) -> str: ...
    async def repo(self) -> RepoInfo: ...
    async def find_prs(self, head_branch: str, state: str = "all") -> list[PullRequest]: ...
    async def create_pr(self, head: str, base: str, title: str, body: str) -> PullRequest: ...
    async def update_pr(self, number: int, title: str, body: str) -> PullRequest: ...
    async def get_pr(self, number: int) -> PullRequest: ...
    async def reviews(self, number: int) -> list[Review]: ...
    async def check_runs(self, sha: str) -> list[CheckRun]: ...
    async def statuses(self, sha: str) -> list[CommitStatus]: ...
    async def branch_protection(self, branch: str) -> BranchProtection | None: ...
    async def commit(self, sha: str) -> CommitInfo: ...
    async def prs_for_commit(self, sha: str) -> list[PullRequest]: ...
