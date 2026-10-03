"""In-memory Jira that enforces the setup-document workflow and models real failure modes.

* Transitions are offered per current status exactly as the setup instructions define,
  with the configured names; anything else is rejected like a real workflow would.
* Comments, changelog and search are paginated internally (callers get full lists, as
  the real adapter does), and IDs increase like Jira's.
* Failure injection: lose the response after applying a write (``lose_next``), fail
  before applying (``fail_next``), or make every call raise (``offline``).
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from delivery.adf import adf_to_text, markdown_to_adf
from delivery.ownership import IssueView
from delivery.ports import (
    Attachment,
    IntegrationError,
    IssueLink,
    JiraComment,
    JiraFieldInfo,
    JiraIssue,
    JiraStatusInfo,
    JiraTransition,
    JiraUser,
    NotFound,
    StatusChange,
    UncertainResult,
)
from delivery.workflow import (
    DEFAULT_ACTION_NAMES,
    ROUTES,
    STATUS_CATEGORIES,
    STATUS_NAMES,
    Action,
    Status,
)


@dataclass
class FakeIssue:
    key: str
    summary: str
    description: str
    status: Status
    assignee: str | None
    issue_type: str = "Story"
    labels: tuple[str, ...] = ()
    links: list[IssueLink] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolution: str | None = None
    files: dict[str, tuple[Attachment, bytes]] = field(default_factory=dict)


class FakeJira:
    def __init__(
        self,
        status_ids: dict[Status, str],
        *,
        me: str,
        project: str = "PILOT",
        actor_names: dict[Status, str] | None = None,
    ) -> None:
        self.status_ids = status_ids
        self.by_id = {v: k for k, v in status_ids.items()}
        self.me = me
        self.project = project
        self.issues: dict[str, FakeIssue] = {}
        self.comments_by_key: dict[str, list[JiraComment]] = defaultdict(list)
        self.changes_by_key: dict[str, list[StatusChange]] = defaultdict(list)
        self.ids = itertools.count(1000)
        self._clock = [datetime(2026, 10, 1, 9, 0, tzinfo=UTC)]
        self.fail_next: dict[str, Exception] = {}
        self.lose_next: set[str] = set()
        self.offline = False
        self.calls: list[tuple[str, str]] = []
        self.action_names = dict(DEFAULT_ACTION_NAMES)
        self.drop_routes: set[tuple[Status, Status]] = set()
        self.type_statuses: dict[str, set[str]] = {
            t: set(status_ids.values()) for t in ("Story", "Task", "Bug")
        }
        self.people: list[JiraUser] = []  # found by find_users

    # ------------------------------------------------------------------ test helpers
    @property
    def clock(self) -> datetime:
        return self._clock[0]

    def tick(self, seconds: int = 1) -> datetime:
        self._clock[0] += timedelta(seconds=seconds)
        return self._clock[0]

    def as_user(self, account: str) -> FakeJira:
        """Another identity's view of the same Jira site (shared state, different author)."""
        import copy

        other = copy.copy(self)
        other.me = account
        other.fail_next = {}
        other.lose_next = set()
        return other

    def create(
        self,
        key: str,
        summary: str,
        description: str,
        assignee: str | None,
        status: Status = Status.BACKLOG,
        **kw: Any,
    ) -> FakeIssue:
        issue = FakeIssue(key, summary, description, status, assignee, **kw)
        self.issues[key] = issue
        return issue

    def attach(
        self, key: str, filename: str, data: bytes, mime: str = "", author: str | None = None
    ) -> Attachment:
        """A human attaches a file (or pastes an image into the description)."""
        issue = self.issues[key]
        att = Attachment(str(next(self.ids)), filename, mime, len(data), self.tick(), author)
        issue.files[att.id] = (att, data)
        issue.updated = self.clock
        return att

    def human_comment(self, key: str, author: str, text: str) -> JiraComment:
        t = self.tick()
        c = JiraComment(str(next(self.ids)), author, t, t, text, author, markdown_to_adf(text))
        self.comments_by_key[key].append(c)
        return c

    def edit_comment(self, key: str, cid: str, text: str) -> None:
        lst = self.comments_by_key[key]
        for i, c in enumerate(lst):
            if c.id == cid:
                lst[i] = JiraComment(c.id, c.author_account_id, c.created, self.tick(), text)

    def human_move(self, key: str, target: Status, author: str) -> None:
        """A human performs a transition; only routes offered by the workflow are allowed."""
        issue = self.issues[key]
        names = [t.name for t in self._available(issue) if self.by_id[t.to_status_id] is target]
        if not names:
            raise AssertionError(f"workflow offers no transition {issue.status} -> {target}")
        self._apply(issue, target, author)

    def _apply(self, issue: FakeIssue, target: Status, author: str | None) -> None:
        src = issue.status
        issue.status = target
        issue.updated = self.tick()
        issue.resolution = "Done" if target in (Status.DONE, Status.CANCELLED) else None
        self.changes_by_key[issue.key].append(
            StatusChange(
                str(next(self.ids)),
                author,
                issue.updated,
                self.status_ids[src],
                self.status_ids[target],
                STATUS_NAMES[src],
                STATUS_NAMES[target],
            )
        )

    def _available(self, issue: FakeIssue) -> list[JiraTransition]:
        out = []
        seen: set[tuple[Action, Status]] = set()
        for r in ROUTES:
            if r.source is issue.status and (r.source, r.target) not in self.drop_routes:
                # One Jira transition serves a route both humans and the coordinator may take.
                if (r.action, r.target) in seen:
                    continue
                seen.add((r.action, r.target))
                if r.resume_stage is not None and issue.fields.get("customfield_10050"):
                    want = issue.fields["customfield_10050"]
                    if isinstance(want, dict) and want.get("value") != r.resume_stage.value:
                        continue
                out.append(
                    JiraTransition(
                        f"{r.source.value}->{r.action.value}",
                        self.action_names[r.action],
                        self.status_ids[r.target],
                        STATUS_NAMES[r.target],
                    )
                )
        return out

    def status_of(self, key: str) -> Status:
        return self.issues[key].status

    def _guard(self, op: str, key: str = "") -> None:
        self.calls.append((op, key))
        if self.offline:
            raise IntegrationError("Jira unreachable", retryable=True)
        if op in self.fail_next:
            raise self.fail_next.pop(op)

    def _issue(self, key: str) -> FakeIssue:
        if key not in self.issues:
            raise NotFound(f"{key} not found", status=404)
        return self.issues[key]

    def _view(self, i: FakeIssue) -> JiraIssue:
        return JiraIssue(
            IssueView(
                i.key,
                i.key.split("-")[0],
                i.issue_type,
                self.status_ids[i.status],
                STATUS_NAMES[i.status],
                i.assignee,
                i.labels,
                (i.fields.get("customfield_10050") or {}).get("value")
                if isinstance(i.fields.get("customfield_10050"), dict)
                else None,
                i.summary,
            ),
            i.description,
            i.created,
            i.updated,
            tuple(i.links),
            i.resolution,
            assignee_name=f"user:{i.assignee}" if i.assignee else "",
            attachments=tuple(a for a, _ in i.files.values()),
        )

    # ------------------------------------------------------------------ JiraPort
    async def myself(self) -> JiraUser:
        self._guard("myself")
        return JiraUser(self.me, "Me")

    async def search(self, jql: str) -> list[JiraIssue]:
        self._guard("search")
        import re

        ids: set[str] = set()
        if "status in" in jql:
            ids = set(re.findall(r"\d{5}", jql.split("status in", 1)[1].split(")")[0]))
        elif m := re.search(r"status = (\d+)", jql):
            ids = {m.group(1)}
        assignee = re.search(r'assignee = "([^"]+)"', jql)
        label = re.search(r'labels = "([^"]+)"', jql)
        out = []
        for i in sorted(self.issues.values(), key=lambda x: x.key):
            if ids and self.status_ids[i.status] not in ids:
                continue
            if assignee and i.assignee != assignee.group(1):
                continue
            if label and label.group(1) not in i.labels:
                continue
            out.append(self._view(i))
        return out

    async def get_issue(self, key: str) -> JiraIssue:
        self._guard("get_issue", key)
        return self._view(self._issue(key))

    async def comments(self, key: str) -> list[JiraComment]:
        self._guard("comments", key)
        return list(self.comments_by_key[key])

    async def status_changes(self, key: str) -> list[StatusChange]:
        self._guard("status_changes", key)
        return list(self.changes_by_key[key])

    async def transitions(self, key: str) -> list[JiraTransition]:
        self._guard("transitions", key)
        return self._available(self._issue(key))

    async def do_transition(self, key: str, transition_id: str, fields: dict[str, Any] | None = None) -> None:
        self._guard("do_transition", key)
        issue = self._issue(key)
        match = [t for t in self._available(issue) if t.id == transition_id]
        if not match:
            raise IntegrationError(f"transition {transition_id} not valid from {issue.status}", status=400)
        self._apply(issue, self.by_id[match[0].to_status_id], self.me)
        if "do_transition" in self.lose_next:
            self.lose_next.discard("do_transition")
            raise UncertainResult("response lost after transition applied")

    async def create_issue(
        self, project: str, issue_type: str, summary: str, description: dict[str, Any], labels: list[str]
    ) -> str:
        self._guard("create_issue")
        key = f"{project}-{next(self.ids)}"
        self.create(key, summary, adf_to_text(description), None, issue_type=issue_type, labels=tuple(labels))
        return key

    async def add_comment(self, key: str, adf: dict[str, Any]) -> JiraComment:
        self._guard("add_comment", key)
        self._issue(key)
        t = self.tick()
        c = JiraComment(str(next(self.ids)), self.me, t, t, adf_to_text(adf), "Me", adf)
        self.comments_by_key[key].append(c)
        if "add_comment" in self.lose_next:
            self.lose_next.discard("add_comment")
            raise UncertainResult("response lost after comment created")
        return c

    async def get_property(self, key: str, name: str) -> dict[str, Any] | None:
        self._guard("get_property", key)
        return self._issue(key).properties.get(name)

    async def set_property(self, key: str, name: str, value: dict[str, Any]) -> None:
        self._guard("set_property", key)
        import json

        if len(json.dumps(value).encode()) > 32768:
            raise IntegrationError("property too large", status=400)
        self._issue(key).properties[name] = value

    async def set_fields(self, key: str, fields: dict[str, Any]) -> None:
        self._guard("set_fields", key)
        self._issue(key).fields.update(fields)

    async def download_attachment(self, attachment_id: str, dest: Path, max_bytes: int) -> int:
        self._guard("download_attachment", attachment_id)
        for issue in self.issues.values():
            if attachment_id in issue.files:
                data = issue.files[attachment_id][1]
                if len(data) > max_bytes:
                    raise IntegrationError(f"attachment {attachment_id} exceeds {max_bytes} bytes")
                dest.write_bytes(data)
                return len(data)
        raise NotFound(f"attachment {attachment_id} not found", status=404)

    async def project_statuses(self, project_key: str) -> list[JiraStatusInfo]:
        self._guard("project_statuses")
        return [
            JiraStatusInfo(sid, STATUS_NAMES[s], STATUS_CATEGORIES[s].value)
            for s, sid in self.status_ids.items()
        ]

    async def issue_type_statuses(self, project_key: str) -> dict[str, set[str]]:
        return {k: set(v) for k, v in self.type_statuses.items()}

    async def fields(self) -> list[JiraFieldInfo]:
        return [JiraFieldInfo("customfield_10050", "Delivery resume stage", True, "option")]

    async def user(self, account_id: str) -> JiraUser | None:
        return JiraUser(account_id, f"user:{account_id}")

    async def find_users(self, query: str) -> list[JiraUser]:
        return [u for u in self.people if query.lower() in u.display_name.lower()]

    async def projects(self) -> list[tuple[str, str]]:
        return [(self.project, f"{self.project} project")]

    async def close(self) -> None:
        return None
