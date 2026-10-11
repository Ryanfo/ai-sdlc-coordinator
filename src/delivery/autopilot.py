"""Auto mode (`coordinator auto`): nobody reviews, the coordinator works through the board.

Every move a person would make, the coordinator makes itself with the developer's Jira account,
so intake validates it exactly like a person's: it approves the specification and the plan,
sends questions back as "assume and record it", submits the fixes a failed verification asks
for, approves the code (Claude's review in verification, ponytail's, is the code review) and
accepts the delivery. Then it merges the PR; the existing release check moves the ticket to
Done. Once nothing of the developer's is under way, it submits the next Backlog ticket, top of
the board first, so each ticket starts from a base branch that has the previous one.

One ticket at a time: moves that cost no Claude time (approving code, accepting, merging) are
made at once, but a move that starts Claude (approving a specification or plan, submitting
fixes, answering, resuming, taking from Backlog) is made for one ticket only, and only while no
Claude work of the developer's is under way. The ticket furthest along goes first (one another
ticket waits for before it), then the board's order. The rest wait in their review status, which
costs nothing, rather than all being planned at once against a base branch about to change.

A ticket that depends on another one is not given up on: when a Backlog ticket "is blocked by"
an unfinished ticket, that one is submitted first, and when a stage blocks naming another
ticket (a link, or its key in the blocker), the ticket waits, the other one is delivered, and
the blocked stage is resumed. Anything auto mode cannot finish (fix rounds used up, questions
again after an "assume" answer, a blocker that does not clear, a PR that will not merge) is
left where it is with a comment, and auto mode moves on. It picks the ticket up again once a
person moves it on.

Claude's usage limit needs nothing here: the supervisor stops polling while Claude is
unavailable and carries on when it works again.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Container
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delivery import comments as text
from delivery import console
from delivery.config import Config
from delivery.gates import current_gate, evaluate_ci
from delivery.intake import TicketContext, brief_is_usable, load_context
from delivery.journal import RunJournal, atomic_write_json
from delivery.models import GateKind, GateState
from delivery.ownership import auto_jql, backlog_jql, keys_jql
from delivery.ports import GitHubPort, IntegrationError, JiraIssue, JiraPort
from delivery.publication import PublicationError, PublicationUncertain, Publisher, TicketMoved
from delivery.workflow import ACTIVE_STATUSES, READY_STATUSES, STAGES, Action, Stage, Status

log = logging.getLogger("delivery")

# How each review status is approved, and the gate whose pending revision the move decides.
APPROVALS: dict[Status, tuple[GateKind, Action, str]] = {
    Status.SPECIFICATION_REVIEW: (GateKind.SPEC, Action.APPROVE_SPECIFICATION, "specification approved"),
    Status.PLAN_REVIEW: (GateKind.PLAN, Action.APPROVE_PLAN, "plan approved"),
    Status.CODE_REVIEW: (GateKind.CODE, Action.APPROVE_CODE, "code approved"),
    Status.ACCEPTANCE_REVIEW: (GateKind.ACCEPT, Action.ACCEPT_DELIVERY, "delivery accepted"),
}
# Jira link descriptions (seen from the dependent ticket) that mean "needs that one first".
BLOCKED_BY = ("is blocked by", "depends on")
FINISHED = frozenset({Status.DONE, Status.CANCELLED})
# Statuses whose next move starts Claude, furthest along first: one such move at a time.
STARTS = (
    Status.CHANGES_REQUESTED,
    Status.BLOCKED,
    Status.NEEDS_CLARIFICATION,
    Status.PLAN_REVIEW,
    Status.SPECIFICATION_REVIEW,
)
# Claude is working on the ticket, or is about to.
CLAUDE_WORKING = READY_STATUSES | ACTIVE_STATUSES


@dataclass
class AutoState:
    """What auto mode remembers between polls and restarts (<state_dir>/autopilot.json)."""

    # Tickets left for a person: {"reason", "status"}. Cleared once the ticket's status changes.
    stuck: dict[str, dict[str, str]] = field(default_factory=dict)
    # Tickets waiting for others to be delivered first: key -> the keys it waits on.
    waiting_on: dict[str, list[str]] = field(default_factory=dict)
    # Counters per ticket: "answers", "retries", "merges".
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def count(self, key: str, what: str) -> int:
        return self.counts.get(key, {}).get(what, 0)

    def bump(self, key: str, what: str) -> None:
        self.counts.setdefault(key, {})[what] = self.count(key, what) + 1


class Autopilot:
    def __init__(
        self,
        cfg: Config,
        jira: JiraPort,
        github: GitHubPort | None,
        emit: Callable[[str], None],
        *,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.jira = jira
        self.github = github
        self.emit = emit
        self.dry_run = dry_run
        self.by_id = cfg.status_by_id()
        self.path = cfg.runtime.state_dir / "autopilot.json"
        self.state = self._load()
        self._noted: set[str] = set()
        self._protection_checked = False

    # ------------------------------------------------------------------ state
    def _load(self) -> AutoState:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return AutoState()
        return AutoState(
            stuck=dict(data.get("stuck") or {}),
            waiting_on={k: list(v) for k, v in (data.get("waiting_on") or {}).items()},
            counts={k: dict(v) for k, v in (data.get("counts") or {}).items()},
        )

    def _save(self) -> None:
        if self.dry_run:
            return
        atomic_write_json(
            self.path,
            {"stuck": self.state.stuck, "waiting_on": self.state.waiting_on, "counts": self.state.counts},
        )

    def _note(self, marker: str, message: str) -> None:
        if marker not in self._noted:
            self._noted.add(marker)
            self.emit(console.line(message))

    def _status(self, issue: JiraIssue) -> Status | None:
        return self.by_id.get(issue.view.status_id)

    def _publisher(self, key: str) -> Publisher:
        journal = RunJournal(self.cfg.runtime.state_dir / "intake" / key)
        return Publisher(self.cfg, self.jira, None, None, journal, f"auto-{key}")

    # ------------------------------------------------------------------ the poll
    async def tick(self, skip: Container[str]) -> None:
        """Make the moves people would make on the developer's tickets under way.

        ``skip``: tickets a session is working on or publishing for right now.
        """
        await self._check_protection()
        mine = await self.jira.search(auto_jql(self.cfg))
        busy = any(k in skip for k in (i.key for i in mine)) or any(
            self._status(i) in CLAUDE_WORKING for i in mine
        )
        wanted = {k for keys in self.state.waiting_on.values() for k in keys}
        ready: list[tuple[tuple[bool, int, int], JiraIssue, Status]] = []
        for rank, issue in enumerate(mine):
            key = issue.key
            status = self._status(issue)
            if status is None or key in skip:
                continue
            left = self.state.stuck.get(key)
            if left is not None:
                if left.get("status") == status.value:
                    continue
                self.state.stuck.pop(key)  # a person moved it on: auto mode takes it up again
                self.state.counts.pop(key, None)
                self._save()
            # Everything that costs no Claude time now; whether it would start Claude, noted.
            if await self._guarded(issue, status, start=False):
                # Work another ticket waits for first, then the furthest along, then board order.
                ready.append(((key not in wanted, STARTS.index(status), rank), issue, status))
        if not ready:
            return
        if busy:
            self._note(
                f"queue:{sorted(i.key for _, i, _ in ready)}",
                f"auto mode: {len(ready)} ticket(s) wait their turn; Claude works on one at a time",
            )
            return
        _, issue, status = min(ready, key=lambda r: r[0])
        await self._guarded(issue, status, start=True)

    async def _guarded(self, issue: JiraIssue, status: Status, *, start: bool) -> bool:
        try:
            return await self._act(issue, status, start=start)
        except (IntegrationError, PublicationError, PublicationUncertain, TicketMoved) as exc:
            self._note(
                f"{issue.key}:{status.value}:{exc}",
                f"{issue.key}: auto mode could not act yet ({exc}); retrying",
            )
            return False

    async def _check_protection(self) -> None:
        """Say once if GitHub will refuse auto mode's merges: a required approving review cannot
        come from the PR's author, and nobody else reviews in auto mode."""
        if self._protection_checked or self.github is None:
            return
        base = self.cfg.repository.base_branch
        protection = await self.github.branch_protection(base)
        self._protection_checked = True
        if protection and protection.required_approving_reviews:
            self.emit(
                console.line(
                    f"auto mode: {base} requires {protection.required_approving_reviews} approving "
                    "review(s) on GitHub, so its merges will be refused. Turn that off in the branch "
                    "protection (keep the required checks) or merge each PR yourself."
                )
            )

    async def next_ticket(self) -> str | None:
        """Submit the next Backlog ticket once nothing of the developer's is under way.

        A ticket that depends on unfinished work is preceded by that work: blockers of waiting
        tickets come first, then the board's order, each one's own blockers before it.
        """
        mine = await self.jira.search(auto_jql(self.cfg))
        under_way = [
            i.key
            for i in mine
            if i.key not in self.state.stuck
            and i.key not in self.state.waiting_on
            and self._status(i) not in FINISHED
        ]
        if under_way:
            return None
        backlog = await self.jira.search(backlog_jql(self.cfg))
        me = self.cfg.identity.developer_jira_account_id
        open_ = {
            i.key: i
            for i in backlog
            if i.view.assignee_account_id in (None, me) and i.key not in self.state.stuck
        }
        wanted = [k for keys in self.state.waiting_on.values() for k in keys if k in open_]
        for key in [*dict.fromkeys(wanted), *open_]:
            chosen = await self._first_unblocked(key, open_, set())
            if chosen is None:
                continue
            issue = open_[chosen]
            if not brief_is_usable(issue):
                # Not left for good: it is picked up once someone fills in the description.
                self._note(
                    f"brief:{chosen}", f"{chosen}: its description is too short to start from; skipped"
                )
                continue
            await self._submit(issue)
            return chosen
        self._note(f"empty:{len(open_)}", "auto mode: nothing left in Backlog to start")
        return None

    async def _first_unblocked(self, key: str, open_: dict[str, JiraIssue], seen: set[str]) -> str | None:
        """``key`` itself, or the first of its unfinished blockers to deliver before it.

        None when it waits on work auto mode cannot do (another person's ticket under way, a
        ticket outside the Backlog it may take, a cycle).
        """
        if key in seen:
            self._note(f"cycle:{key}", f"{key}: its 'is blocked by' links go round in a circle; skipped")
            return None
        seen.add(key)
        blockers = _linked_blockers(open_[key])
        if not blockers:
            return key
        found = {i.key: i for i in await self.jira.search(keys_jql(blockers)) if i.key in blockers}
        for other in blockers:
            issue = found.get(other)
            status = self._status(issue) if issue else None
            if status in FINISHED:
                continue
            if other in open_:
                return await self._first_unblocked(other, open_, seen)
            if other in self.state.stuck:
                self._note(
                    f"blocked:{key}:{other}", f"{key}: waits for {other}, which auto mode left for you"
                )
                return None
            self._note(
                f"blocked:{key}:{other}",
                f"{key}: waits for {other} ({status.value if status else 'not found'}), which auto mode "
                "is not working on; skipped for now",
            )
            return None
        return key

    # ------------------------------------------------------------------ one ticket
    async def _act(self, issue: JiraIssue, status: Status, *, start: bool) -> bool:
        """Act on one ticket. A move that starts Claude is made only when ``start``; True when
        the ticket has (or, without ``start``, wants) such a move."""
        key = issue.key
        if status in APPROVALS:
            return await self._approve(key, status, start)
        if status is Status.CHANGES_REQUESTED:
            return await self._submit_changes(key, start)
        if status is Status.NEEDS_CLARIFICATION:
            return await self._answer(key, start)
        if status is Status.BLOCKED:
            return await self._unblock(issue, start)
        if status is Status.READY_RELEASE:
            await self._merge(key)
        elif status in (Status.READY_RESOLUTION, Status.RESOLVING):
            self._leave_quietly(key, status, "a person asked to resolve its blocker with Claude")
        elif key in self.state.waiting_on:
            # Moved on by a person while it waited: it is under way again.
            self.state.waiting_on.pop(key)
            self._save()
        return False

    async def _approve(self, key: str, status: Status, start: bool) -> bool:
        kind, action, what = APPROVALS[status]
        ctx = await load_context(self.jira, self.cfg, key)
        gate = current_gate(ctx.record.gates, kind)
        if gate is None or gate.state is not GateState.PENDING:
            self._note(
                f"{key}:nogate:{status.value}", f"{key}: no {kind.value} revision waits for a decision"
            )
            return False
        starts_claude = status in STARTS  # approving code or accepting starts no session
        if starts_claude and not start:
            return True
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would choose {action.value} on {key} ({gate.token})")
            return starts_claude
        pub = self._publisher(key)
        await pub.comment(key, f"auto-{action.value}", text.auto_moved(f"{what} ({gate.token})"), gate.token)
        await pub.transition(key, status, action, gate.token, as_person=True)
        self.emit(console.line(f"{key}: auto mode: {what} ({gate.token})"))
        return starts_claude

    def _entry_id(self, ctx: TicketContext, status: Status) -> str:
        entry = ctx.latest_entry(self.cfg.status_id(status))
        return entry.history_id if entry else "-"

    async def _submit_changes(self, key: str, start: bool) -> bool:
        ctx = await load_context(self.jira, self.cfg, key)
        into = self.cfg.status_id(Status.CHANGES_REQUESTED)
        rounds = sum(1 for c in ctx.changes if c.to_id == into)
        limit = self.cfg.auto.review_rounds
        if rounds > limit:
            await self._leave(
                ctx,
                Status.CHANGES_REQUESTED,
                f"Verification or code review still fails after {limit} automatic fix round"
                f"{'s' if limit != 1 else ''}; the latest findings are above.",
            )
            return False
        if not start:
            return True
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would submit implementation changes on {key}")
            return True
        await self._publisher(key).transition(
            key,
            Status.CHANGES_REQUESTED,
            Action.SUBMIT_IMPLEMENTATION_CHANGES,
            self._entry_id(ctx, Status.CHANGES_REQUESTED),
            as_person=True,
        )
        self.emit(
            console.line(f"{key}: auto mode: fixing what verification found (round {rounds} of {limit})")
        )
        return True

    async def _answer(self, key: str, start: bool) -> bool:
        ctx = await load_context(self.jira, self.cfg, key)
        pause = ctx.record.pause
        if pause is None or pause.kind != "clarification":
            self._leave_quietly(key, Status.NEEDS_CLARIFICATION, "it has no questions recorded")
            return False
        counter = f"answers:{pause.resume_stage.value}"
        if self.state.count(key, counter) >= self.cfg.auto.answer_rounds:
            await self._leave(
                ctx, Status.NEEDS_CLARIFICATION, "Claude still has questions it cannot settle by assuming."
            )
            return False
        if not start:
            return True
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would send {key}'s questions back to be assumed")
            return True
        revision = pause.round_token or self._entry_id(ctx, Status.NEEDS_CLARIFICATION)
        pub = self._publisher(key)
        await pub.comment(key, "auto-answers", text.auto_answers(pause.question_ids), revision)
        action = STAGES[pause.resume_stage].answers_action
        await pub.transition(key, Status.NEEDS_CLARIFICATION, action, revision, as_person=True)
        self.state.bump(key, counter)
        self._save()
        self.emit(console.line(f"{key}: auto mode: questions sent back, Claude assumes and records it"))
        return True

    async def _unblock(self, issue: JiraIssue, start: bool) -> bool:
        key = issue.key
        ctx = await load_context(self.jira, self.cfg, key)
        pause = ctx.record.pause
        if pause is None or pause.kind != "blocker":
            self._leave_quietly(key, Status.BLOCKED, "it was blocked without a recorded reason")
            return False
        needs = _blocking_keys(issue, pause.reason, self.cfg.jira.project_key)
        revision = self._entry_id(ctx, Status.BLOCKED)
        pub = self._publisher(key)
        if needs:
            found = {
                i.key: self._status(i) for i in await self.jira.search(keys_jql(needs)) if i.key in needs
            }
            gone = [k for k in needs if found.get(k) is Status.CANCELLED or k not in found]
            if gone:
                await self._leave(
                    ctx, Status.BLOCKED, f"It needs {', '.join(gone)}, which is cancelled or missing."
                )
                return False
            open_ = [k for k in needs if found.get(k) is not Status.DONE]
            if left := [k for k in open_ if k in self.state.stuck]:
                await self._leave(
                    ctx, Status.BLOCKED, f"It needs {', '.join(left)}, which auto mode left for you."
                )
                return False
            if open_:
                if self.state.waiting_on.get(key) != open_:
                    self.state.waiting_on[key] = open_
                    self._save()
                    if not self.dry_run:
                        await pub.comment(key, "auto-waiting", text.auto_waiting_on(open_), revision)
                    self.emit(
                        console.line(f"{key}: auto mode: waits for {', '.join(open_)}, delivered first")
                    )
                return False
            if not start:
                return True
            if self.dry_run:
                self.emit(f"[dry-run] auto mode would resume {key}: {', '.join(needs)} done")
                return True
            await pub.comment(key, "auto-dependency-done", text.auto_dependency_done(needs), revision)
            await self._resume(key, pause.resume_stage, revision)
            self.state.waiting_on.pop(key, None)
            self._save()
            self.emit(console.line(f"{key}: auto mode: {', '.join(needs)} delivered; resuming"))
            return True
        if self.state.count(key, "retries") >= self.cfg.auto.blocker_retries:
            await self._leave(
                ctx, Status.BLOCKED, "The blocker above did not clear when the stage was retried."
            )
            return False
        if not start:
            return True
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would resume {key} to try again")
            return True
        await pub.comment(key, "auto-retry", text.auto_retry(pause.reason), revision)
        await self._resume(key, pause.resume_stage, revision)
        self.state.bump(key, "retries")
        self._save()
        self.emit(console.line(f"{key}: auto mode: blocked ({pause.reason[:80]}); trying once more"))
        return True

    async def _resume(self, key: str, stage: Stage, revision: str) -> None:
        action = STAGES[stage].resume_action
        await self._publisher(key).transition(key, Status.BLOCKED, action, revision, as_person=True)

    async def _merge(self, key: str) -> None:
        ctx = await load_context(self.jira, self.cfg, key)
        rec = ctx.record
        if self.github is None or rec.pr_number is None or not rec.candidate_sha:
            await self._leave(ctx, Status.READY_RELEASE, "No pull request is recorded to merge.")
            return
        pr = await self.github.get_pr(rec.pr_number)
        if pr.merged:
            return  # the supervisor completes the release
        if pr.state != "open":
            await self._leave(ctx, Status.READY_RELEASE, f"PR #{pr.number} was closed without merging.")
            return
        if pr.head_sha != rec.candidate_sha:
            await self._leave(
                ctx,
                Status.READY_RELEASE,
                f"PR #{pr.number} changed after it was verified (head {pr.head_sha[:12]}, verified "
                f"{rec.candidate_sha[:12]}).",
            )
            return
        ci_cfg = self.cfg.checks.ci
        ci = evaluate_ci(
            pr.head_sha,
            required_names=ci_cfg.required_names,
            expected_producer=ci_cfg.expected_producer,
            check_runs=await self.github.check_runs(pr.head_sha),
            statuses=await self.github.statuses(pr.head_sha),
            allow_neutral=ci_cfg.allow_neutral,
            allow_skipped=ci_cfg.allow_skipped,
        )
        if ci.pending:
            self._note(f"{key}:ci:{pr.head_sha}", f"{key}: auto mode merges PR #{pr.number} once CI finishes")
            return
        if not ci.ok:
            await self._leave(
                ctx, Status.READY_RELEASE, f"CI fails on PR #{pr.number}: {'; '.join(ci.problems[:3])}."
            )
            return
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would merge PR #{pr.number} for {key}")
            return
        method = self.cfg.auto.merge_method
        try:
            commit = await self.github.merge_pr(pr.number, method, pr.head_sha)
        except IntegrationError as exc:
            self.state.bump(key, "merges")
            self._save()
            if self.state.count(key, "merges") >= self.cfg.auto.merge_attempts:
                await self._leave(ctx, Status.READY_RELEASE, f"GitHub would not merge PR #{pr.number}: {exc}")
            else:
                self.emit(console.line(f"{key}: auto mode could not merge PR #{pr.number} ({exc}); retrying"))
            return
        self.emit(console.line(f"{key}: auto mode: merged PR #{pr.number} ({method}) as {commit[:12]}"))

    async def _submit(self, issue: JiraIssue) -> None:
        key = issue.key
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would submit {key} for refinement")
            return
        me = self.cfg.identity.developer_jira_account_id
        if issue.view.assignee_account_id != me:
            await self.jira.set_fields(key, {"assignee": {"accountId": me}})
        ctx = await load_context(self.jira, self.cfg, key)
        await self._publisher(key).transition(
            key,
            Status.BACKLOG,
            Action.SUBMIT_REFINEMENT,
            self._entry_id(ctx, Status.BACKLOG) + f":{len(ctx.changes)}",
            as_person=True,
        )
        self.emit(console.line(f"{key}: auto mode: next on the board, submitted for refinement"))

    # ------------------------------------------------------------------ giving up on one
    async def _leave(self, ctx: TicketContext, status: Status, reason: str) -> None:
        key = ctx.key
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would leave {key} for you: {reason}")
            return
        await self._publisher(key).comment(
            key, "auto-left", text.auto_left(reason), f"{status.value}:{self._entry_id(ctx, status)}"
        )
        self._leave_quietly(key, status, reason)

    def _leave_quietly(self, key: str, status: Status, reason: str) -> None:
        if self.dry_run:
            self.emit(f"[dry-run] auto mode would leave {key} for you: {reason}")
            return
        self.state.stuck[key] = {"status": status.value, "reason": reason}
        self.state.waiting_on.pop(key, None)
        self._save()
        self.emit(console.line(f"{key}: auto mode left it for you ({reason}); moving on"))
        # Tickets waiting on this one cannot go on either.
        for other, keys in list(self.state.waiting_on.items()):
            if key in keys:
                self.state.stuck[other] = {"status": Status.BLOCKED.value, "reason": f"it needs {key}"}
                self.state.waiting_on.pop(other)
                self._save()
                self.emit(console.line(f"{other}: auto mode left it for you too (it needs {key})"))


def _linked_blockers(issue: JiraIssue) -> list[str]:
    return [
        link.other_key
        for link in issue.links
        if link.direction == "inward" and link.description.strip().lower() in BLOCKED_BY
    ]


def _blocking_keys(issue: JiraIssue, reason: str, project: str) -> list[str]:
    """Tickets a blocked stage waits on: its 'is blocked by' links and the keys its reason names."""
    named = re.findall(rf"\b{re.escape(project)}-\d+\b", reason)
    keys = [*_linked_blockers(issue), *named]
    return [k for k in dict.fromkeys(keys) if k != issue.key]


def summary(state_dir: Path) -> dict[str, Any]:
    """What auto mode left for a person and what waits for other tickets (`coordinator status`)."""
    try:
        data = json.loads((state_dir / "autopilot.json").read_text())
    except (OSError, ValueError):
        return {"left_for_you": {}, "waiting_on": {}}
    return {
        "left_for_you": {k: v.get("reason", "") for k, v in (data.get("stuck") or {}).items()},
        "waiting_on": dict(data.get("waiting_on") or {}),
    }
