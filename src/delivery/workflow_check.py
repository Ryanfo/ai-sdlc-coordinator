"""``delivery workflow verify``: prove the live Jira workflow matches the agreed routes.

``workflow inspect`` can only sample transitions from tickets that already sit in each
status. This check creates its own labelled test tickets and walks them through every status,
comparing what Jira offers at each one with the route table in :mod:`delivery.workflow`. It
also checks the resume field (set, read back, clear, and whether Jira hides the wrong resume
actions), issue properties, comments and changelog authorship.

The test tickets are **unassigned**, so no supervisor ever picks them up, and carry the label
``delivery-workflow-check``. They end in Done and Cancelled; delete them by hand afterwards.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from delivery.adf import markdown_to_adf
from delivery.config import Config
from delivery.ports import IntegrationError, JiraPort, JiraTransition
from delivery.workflow import (
    FOLLOW_UP_ROUTES,
    OPTIONAL_ROUTES,
    OPTIONAL_STATUSES,
    PAUSED_STATUSES,
    STATUS_NAMES,
    TERMINAL_STATUSES,
    Action,
    Stage,
    Status,
    required_statuses,
    routes_for,
)

CHECK_LABEL = "delivery-workflow-check"
PROPERTY = "delivery.workflow-check"

# Named routes the setup instructions allow in addition to the route table. The coordinator
# itself uses Block stage for these; the extra name only helps humans reading the history.
OPTIONAL: dict[tuple[Status, Status], set[str]] = {}


class CreatesIssues(JiraPort, Protocol):
    async def create_issue(
        self, project: str, issue_type: str, summary: str, description: dict[str, Any], labels: list[str]
    ) -> str: ...

    async def issue_type_statuses(self, project_key: str) -> dict[str, set[str]]: ...


# One walk that visits every status. A stage name before an action means "set the resume field
# to this stage first" (as the coordinator does before pausing); "clear" clears it (as the
# coordinator does when a stage starts).
_R = Stage.REFINEMENT
WALK: tuple[tuple[Action, Stage | str | None], ...] = (
    (Action.SUBMIT_REFINEMENT, None),
    (Action.START_REFINEMENT, "clear"),
    (Action.ASK_QUESTIONS, _R),
    (Action.SUBMIT_REFINEMENT_ANSWERS, None),
    (Action.START_REFINEMENT, "clear"),
    (Action.BLOCK_STAGE, _R),
    (Action.RESUME_REFINEMENT, None),
    (Action.START_REFINEMENT, "clear"),
    (Action.COMPLETE_REFINEMENT, None),
    (Action.APPROVE_SPECIFICATION, None),
    (Action.START_PLANNING, None),
    (Action.COMPLETE_PLANNING, None),
    (Action.APPROVE_PLAN, None),
    (Action.START_DEVELOPMENT, None),
    (Action.COMPLETE_DEVELOPMENT, None),
    (Action.START_VERIFICATION, None),
    (Action.VERIFICATION_FAILED, None),
    (Action.SUBMIT_IMPLEMENTATION_CHANGES, None),
    (Action.START_DEVELOPMENT, None),
    (Action.COMPLETE_DEVELOPMENT, None),
    (Action.START_VERIFICATION, None),
    (Action.COMPLETE_VERIFICATION, None),
    (Action.APPROVE_CODE, None),
    (Action.ACCEPT_DELIVERY, None),
    (Action.RECORD_RELEASE, None),
)


Walk = tuple[tuple[Action, Stage | str | None], ...]


def _with_proposal(walk: Walk) -> Walk:
    """The walk plus the release proposal (``[release] proposal``): accepting the delivery leads
    to release preparation and an approved proposal, not straight to Ready for release."""
    out: list[tuple[Action, Stage | str | None]] = []
    for action, resume in walk:
        out.append((action, resume))
        if action is Action.ACCEPT_DELIVERY:
            out += [
                (Action.START_RELEASE_PREPARATION, None),
                (Action.COMPLETE_RELEASE_PREPARATION, None),
                (Action.APPROVE_RELEASE, None),
            ]
    return tuple(out)


def _with_resolution(walk: Walk) -> Walk:
    """The walk plus resolution. A blocker that is not resolved goes back to Blocked; one that is
    goes back to the stage that blocked. (The other five Resolved: actions are checked for
    existence at Resolving, as the resume actions are at Blocked.)"""
    request = ((Action.REQUEST_RESOLUTION, None), (Action.START_RESOLUTION, None))
    out: list[tuple[Action, Stage | str | None]] = []
    for action, resume in walk:
        if action is Action.RESUME_REFINEMENT:
            out += [*request, (Action.BLOCK_STAGE, None)]  # unresolved: Blocked again, resumed by hand
        if action is Action.COMPLETE_REFINEMENT:
            out += [
                (Action.BLOCK_STAGE, _R),
                *request,
                (Action.RESOLVED_REFINEMENT, None),
                (Action.START_REFINEMENT, "clear"),
            ]
        out.append((action, resume))
    return tuple(out)


@dataclass
class StatusFinding:
    status: Status
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)


@dataclass
class VerifyReport:
    tickets: list[str] = field(default_factory=list)
    findings: dict[str, StatusFinding] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    transitions_done: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tickets": self.tickets,
            "transitions_done": self.transitions_done,
            "statuses": {
                k: {"missing": v.missing, "unexpected": v.unexpected} for k, v in self.findings.items()
            },
            "problems": self.problems,
            "warnings": self.warnings,
            "info": self.info,
        }


class WalkAborted(Exception):
    pass


class _Walker:
    def __init__(self, cfg: Config, jira: CreatesIssues, report: VerifyReport, emit: Callable[[str], None]):
        self.cfg, self.jira, self.report, self.emit = cfg, jira, report, emit
        self.by_id = cfg.status_by_id()
        self.field = cfg.jira.fields.resume_stage
        self.routes = routes_for(cfg.release.proposal)

    def _label(self, t: JiraTransition) -> str:
        target = self.by_id.get(t.to_status_id)
        return f"{t.name} -> {target.value if target else t.to_status_name or t.to_status_id}"

    def _expected(self, status: Status) -> set[tuple[str, Status]]:
        follow_ups = self.cfg.claude.interactive.follow_ups
        return {
            (self.cfg.workflow.action_name(r.action), r.target)
            for r in self.routes
            if r.source is status
            and r.resume_stage is None
            and (follow_ups or r.action is not Action.SUBMIT_FOLLOW_UP)
            and r not in OPTIONAL_ROUTES
        }

    async def status_of(self, key: str) -> Status | None:
        issue = await self.jira.get_issue(key)
        return self.by_id.get(issue.view.status_id)

    async def compare(self, key: str, status: Status) -> list[JiraTransition]:
        """Record missing and unexpected transitions at this status (first visit only)."""
        offered = await self.jira.transitions(key)
        if status.value in self.report.findings:
            return offered
        have = {(t.name, self.by_id.get(t.to_status_id)) for t in offered}
        want = self._expected(status)
        finding = StatusFinding(status)
        finding.missing = sorted(f"{n} -> {t.value}" for n, t in want - have)
        resume_names = {
            self.cfg.workflow.action_name(r.action)
            for r in self.routes
            if r.source is status and r.resume_stage
        }
        optional = {(n, dst) for (src, dst), names in OPTIONAL.items() if src is status for n in names}
        # Follow-up transitions are only required with interactive sessions kept open.
        optional |= {
            (self.cfg.workflow.action_name(r.action), r.target)
            for r in (*FOLLOW_UP_ROUTES, *OPTIONAL_ROUTES)
            if r.source is status
        }
        for t in offered:
            if (t.name, self.by_id.get(t.to_status_id)) in optional:
                self.report.info.append(f"{STATUS_NAMES[status]}: optional route present: {self._label(t)}")
        finding.unexpected = sorted(
            self._label(t)
            for t in offered
            if (t.name, self.by_id.get(t.to_status_id)) not in want | optional and t.name not in resume_names
        )
        self.report.findings[status.value] = finding
        for m in finding.missing:
            self.report.problems.append(f"{STATUS_NAMES[status]}: missing transition {m}")
        for u in finding.unexpected:
            self.report.warnings.append(f"{STATUS_NAMES[status]}: transition not in the agreed workflow: {u}")
        if status in PAUSED_STATUSES or status is Status.RESOLVING:
            await self.check_resume(key, status)
        return offered

    async def set_resume(self, key: str, value: str | None) -> None:
        if not self.field:
            return
        await self.jira.set_fields(key, {self.field: {"value": value} if value else None})
        got = (await self.jira.get_issue(key)).view.resume_stage
        if got != value:
            raise WalkAborted(f"resume field on {key}: wrote {value!r}, read back {got!r}")

    async def check_resume(self, key: str, paused: Status) -> None:
        """Each resume action exists, and whether Jira hides the ones for other stages."""
        routes = [r for r in self.routes if r.source is paused and r.resume_stage is not None]
        current = (await self.jira.get_issue(key)).view.resume_stage
        hides = True
        for r in routes:
            assert r.resume_stage is not None
            if self.field:
                await self.set_resume(key, r.resume_stage.value)
            offered = await self.jira.transitions(key)
            name = self.cfg.workflow.action_name(r.action)
            pairs = {(t.name, self.by_id.get(t.to_status_id)) for t in offered}
            if (name, r.target) not in pairs:
                self.report.problems.append(
                    f"{STATUS_NAMES[paused]}: missing transition {name} -> {r.target.value}"
                )
                self.report.findings[paused.value].missing.append(f"{name} -> {r.target.value}")
            others = {
                self.cfg.workflow.action_name(o.action)
                for o in routes
                if o.resume_stage is not r.resume_stage
            }
            if others & {t.name for t in offered}:
                hides = False
        if self.field:
            await self.set_resume(key, current)
        if not self.field:
            self.report.warnings.append(
                f"{STATUS_NAMES[paused]}: resume field not configured, so Jira shows every resume "
                "action; the coordinator rejects a wrong one"
            )
        elif hides:
            self.report.info.append(f"{STATUS_NAMES[paused]}: Jira hides resume actions for other stages")
        else:
            self.report.warnings.append(
                f"{STATUS_NAMES[paused]}: Jira shows resume actions for every stage (no field "
                "condition). Not required: the coordinator rejects a wrong resume and moves the "
                "ticket to Blocked with the correct one"
            )

    async def move(self, key: str, action: Action) -> Status:
        status = await self.status_of(key)
        if status is None:
            raise WalkAborted(f"{key} is in a status the config does not map")
        route = next((r for r in self.routes if r.source is status and r.action is action), None)
        if route is None:  # pragma: no cover - WALK is checked by tests
            raise WalkAborted(f"walk bug: {action.value} is not a route from {status.value}")
        offered = await self.compare(key, status)
        name = self.cfg.workflow.action_name(action)
        target_id = self.cfg.workflow.statuses[route.target]
        exact = [t for t in offered if t.name == name and t.to_status_id == target_id]
        fallback = [t for t in offered if t.to_status_id == target_id]
        candidates = exact or fallback
        if not candidates:
            raise WalkAborted(
                f"cannot continue: no transition from {STATUS_NAMES[status]} to {STATUS_NAMES[route.target]}"
            )
        chosen = candidates[0]
        await self.jira.do_transition(key, chosen.id)
        self.report.transitions_done += 1
        now = await self.status_of(key)
        if now is not route.target:
            raise WalkAborted(
                f"{chosen.name!r} from {STATUS_NAMES[status]} landed in "
                f"{STATUS_NAMES[now] if now else 'an unmapped status'}, expected {STATUS_NAMES[route.target]}"
            )
        self.emit(f"  {STATUS_NAMES[status]} --{chosen.name}--> {STATUS_NAMES[route.target]}")
        return route.target


async def _create(cfg: Config, jira: CreatesIssues, issue_type: str, title: str) -> str:
    body = markdown_to_adf(
        "Created by `delivery workflow verify` to check that the Jira workflow matches "
        "docs/jira-workflow-setup.md. It is unassigned, so no supervisor will pick it up. "
        "Safe to delete."
    )
    return await jira.create_issue(
        cfg.jira.project_key,
        issue_type,
        f"[delivery workflow check] {title}",
        body,
        [CHECK_LABEL],
    )


async def verify_workflow(
    cfg: Config, jira: CreatesIssues, emit: Callable[[str], None] = print
) -> VerifyReport:
    report = VerifyReport()
    try:
        await _verify(cfg, jira, report, emit)
    except IntegrationError as exc:
        report.problems.append(f"stopped: Jira call failed ({exc})")
    return report


async def _verify(
    cfg: Config, jira: CreatesIssues, report: VerifyReport, emit: Callable[[str], None]
) -> None:
    w = _Walker(cfg, jira, report, emit)
    me = await jira.myself()

    known = set(cfg.workflow.statuses.values())
    by_type = await jira.issue_type_statuses(cfg.jira.project_key)
    usable: list[str] = []
    for t in cfg.jira.supported_issue_types:
        have = by_type.get(t)
        if have is None:
            report.problems.append(f"issue type {t} is not available in {cfg.jira.project_key}")
        elif not known <= have:
            report.problems.append(
                f"issue type {t} does not use the delivery workflow ({len(known & have)} of "
                f"{len(known)} statuses): give it the workflow or remove it from jira.supported_issue_types"
            )
        else:
            usable.append(t)
    others = sorted(t for t in by_type if t not in cfg.jira.supported_issue_types)
    if others:
        report.info.append(f"issue types the coordinator ignores: {', '.join(others)}")
    names = {s.id: s.name for s in await jira.project_statuses(cfg.jira.project_key)}
    extra = sorted({sid for t in usable for sid in by_type[t]} - known)
    if extra:
        report.warnings.append(
            "statuses outside the agreed workflow on supported issue types: "
            + ", ".join(f"{names.get(sid, '?')} ({sid})" for sid in extra)
        )
    if not usable:
        return
    issue_type = "Story" if "Story" in usable else usable[0]

    key = await _create(cfg, jira, issue_type, "lifecycle walk")
    report.tickets.append(key)
    emit(f"Created {key} ({issue_type}, unassigned, label {CHECK_LABEL})")
    first = await w.status_of(key)
    if first is not Status.BACKLOG:
        report.problems.append(
            f"new tickets start in {STATUS_NAMES[first] if first else 'an unmapped status'}, not Backlog"
        )
        return
    report.info.append("new tickets start in Backlog")

    walk = _with_proposal(WALK) if cfg.release.proposal else WALK
    if set(cfg.workflow.statuses) >= OPTIONAL_STATUSES:
        walk = _with_resolution(walk)
        report.info.append("resolution statuses are mapped: the walk includes resolving a blocker")
    try:
        for action, resume in walk:
            if resume == "clear":
                await w.set_resume(key, None)
            elif isinstance(resume, Stage):
                await w.set_resume(key, resume.value)
            await w.move(key, action)
        await w.compare(key, Status.DONE)
    except WalkAborted as exc:
        report.problems.append(str(exc))
    if w.field and not any("resume field" in p for p in report.problems):
        report.info.append(f"resume field {w.field}: set, read back and cleared")

    issue = await jira.get_issue(key)
    report.info.append(f"resolution at {issue.view.status_name}: {issue.resolution or 'none'}")

    # Coordinator plumbing on the same ticket: changelog authorship, properties, comments.
    changes = await jira.status_changes(key)
    if len(changes) != report.transitions_done:
        report.problems.append(
            f"changelog shows {len(changes)} status changes, expected {report.transitions_done}"
        )
    elif any(c.author_account_id != me.account_id for c in changes):
        report.problems.append("changelog status changes are not attributed to the authenticated account")
    else:
        report.info.append(f"changelog: {len(changes)} status changes, all attributed to {me.display_name}")
    await jira.set_property(key, PROPERTY, {"check": "ok", "transitions": report.transitions_done})
    got = await jira.get_property(key, PROPERTY)
    if (got or {}).get("check") != "ok":
        report.problems.append(f"issue property {PROPERTY} did not round-trip")
    else:
        report.info.append("issue properties: write and read back")
    summary = (
        f"Workflow check: {report.transitions_done} transitions, "
        f"{len(report.problems)} problems, {len(report.warnings)} warnings."
    )
    comment = await jira.add_comment(key, markdown_to_adf(summary))
    if comment.author_account_id != me.account_id:
        report.problems.append("comment author is not the authenticated account")

    # Cancel from Backlog on a second ticket (Cancel must exist from every unfinished status;
    # the walk above already checked it was offered at each one).
    second = await _create(cfg, jira, issue_type, "cancel check")
    report.tickets.append(second)
    emit(f"Created {second} (unassigned, label {CHECK_LABEL})")
    try:
        await w.move(second, Action.CANCEL)
        await w.compare(second, Status.CANCELLED)
        cancelled = await jira.get_issue(second)
        report.info.append(f"resolution at Cancelled: {cancelled.resolution or 'none'}")
    except WalkAborted as exc:
        report.problems.append(str(exc))

    visited = set(report.findings)
    unvisited = [
        STATUS_NAMES[s]
        for s in Status
        if s.value not in visited
        and (
            s in required_statuses(cfg.release.proposal)
            or (s in OPTIONAL_STATUSES and s in cfg.workflow.statuses)
        )
    ]
    if unvisited:
        report.warnings.append(f"statuses not reached: {', '.join(unvisited)}")
    for s in TERMINAL_STATUSES:
        f = report.findings.get(s.value)
        if f and f.unexpected:
            report.info.append(
                f"{STATUS_NAMES[s]} offers transitions out ({len(f.unexpected)}); see warnings"
            )


def render(report: VerifyReport) -> str:
    lines = [f"Test tickets: {', '.join(report.tickets) or 'none'} ({report.transitions_done} transitions)"]
    lines += [f"FAIL  {p}" for p in report.problems]
    lines += [f"WARN  {w}" for w in report.warnings]
    lines += [f"INFO  {i}" for i in report.info]
    lines.append(
        "RESULT: workflow matches the agreed routes" if report.ok else "RESULT: fix the FAIL items above"
    )
    if report.tickets:
        lines.append(f"Delete the test tickets when done: {', '.join(report.tickets)}")
    return "\n".join(lines)
