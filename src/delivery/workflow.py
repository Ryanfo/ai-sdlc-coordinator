"""Canonical workflow: statuses, stages, actions and permitted routes.

This module is pure data and pure functions. The coordinator never chooses a
destination status by reasoning; it looks the route up here. Site-specific
status IDs and transition names are mapped in configuration, never hard-coded.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    REFINEMENT = "refinement"
    PLANNING = "planning"
    DEVELOPMENT = "development"
    VERIFICATION = "verification"
    RELEASE_PREPARATION = "release_preparation"
    RELEASE_VERIFICATION = "release_verification"


class Status(StrEnum):
    BACKLOG = "backlog"
    READY_REFINEMENT = "ready_refinement"
    REFINING = "refining"
    SPECIFICATION_REVIEW = "specification_review"
    READY_PLANNING = "ready_planning"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    READY_DEVELOPMENT = "ready_development"
    DEVELOPING = "developing"
    READY_VERIFICATION = "ready_verification"
    VERIFYING = "verifying"
    CODE_REVIEW = "code_review"
    ACCEPTANCE_REVIEW = "acceptance_review"
    CHANGES_REQUESTED = "changes_requested"
    NEEDS_CLARIFICATION = "needs_clarification"
    BLOCKED = "blocked"
    READY_RELEASE_PREPARATION = "ready_release_preparation"
    PREPARING_RELEASE = "preparing_release"
    RELEASE_REVIEW = "release_review"
    READY_RELEASE = "ready_release"
    READY_RELEASE_VERIFICATION = "ready_release_verification"
    VERIFYING_RELEASE = "verifying_release"
    DONE = "done"
    CANCELLED = "cancelled"


class Category(StrEnum):
    TODO = "new"
    IN_PROGRESS = "indeterminate"
    DONE = "done"


# Suggested Jira display names and categories from the setup instructions.
STATUS_NAMES: dict[Status, str] = {
    Status.BACKLOG: "Backlog",
    Status.READY_REFINEMENT: "Ready for refinement",
    Status.REFINING: "Refining",
    Status.SPECIFICATION_REVIEW: "Specification review",
    Status.READY_PLANNING: "Ready for planning",
    Status.PLANNING: "Planning",
    Status.PLAN_REVIEW: "Plan review",
    Status.READY_DEVELOPMENT: "Ready for development",
    Status.DEVELOPING: "Developing",
    Status.READY_VERIFICATION: "Ready for verification",
    Status.VERIFYING: "Verifying",
    Status.CODE_REVIEW: "Code review",
    Status.ACCEPTANCE_REVIEW: "Acceptance review",
    Status.CHANGES_REQUESTED: "Changes requested",
    Status.NEEDS_CLARIFICATION: "Needs clarification",
    Status.BLOCKED: "Blocked",
    Status.READY_RELEASE_PREPARATION: "Ready for release preparation",
    Status.PREPARING_RELEASE: "Preparing release",
    Status.RELEASE_REVIEW: "Release review",
    Status.READY_RELEASE: "Ready for release",
    Status.READY_RELEASE_VERIFICATION: "Ready for release verification",
    Status.VERIFYING_RELEASE: "Verifying release",
    Status.DONE: "Done",
    Status.CANCELLED: "Cancelled",
}

_TODO = {
    Status.BACKLOG,
    Status.READY_REFINEMENT,
    Status.READY_PLANNING,
    Status.READY_DEVELOPMENT,
    Status.READY_VERIFICATION,
    Status.READY_RELEASE_PREPARATION,
    Status.READY_RELEASE,
    Status.READY_RELEASE_VERIFICATION,
}
_DONE = {Status.DONE, Status.CANCELLED}

STATUS_CATEGORIES: dict[Status, Category] = {
    s: Category.TODO if s in _TODO else Category.DONE if s in _DONE else Category.IN_PROGRESS for s in Status
}

TERMINAL_STATUSES = frozenset(_DONE)
PAUSED_STATUSES = frozenset({Status.NEEDS_CLARIFICATION, Status.BLOCKED})
HUMAN_REVIEW_STATUSES = frozenset(
    {
        Status.SPECIFICATION_REVIEW,
        Status.PLAN_REVIEW,
        Status.CODE_REVIEW,
        Status.ACCEPTANCE_REVIEW,
        Status.RELEASE_REVIEW,
        Status.CHANGES_REQUESTED,
    }
)


class Action(StrEnum):
    SUBMIT_REFINEMENT = "submit_refinement"
    START_REFINEMENT = "start_refinement"
    COMPLETE_REFINEMENT = "complete_refinement"
    APPROVE_SPECIFICATION = "approve_specification"
    REQUEST_SPECIFICATION_CHANGES = "request_specification_changes"
    START_PLANNING = "start_planning"
    COMPLETE_PLANNING = "complete_planning"
    APPROVE_PLAN = "approve_plan"
    REQUEST_PLAN_CHANGES = "request_plan_changes"
    START_DEVELOPMENT = "start_development"
    COMPLETE_DEVELOPMENT = "complete_development"
    START_VERIFICATION = "start_verification"
    COMPLETE_VERIFICATION = "complete_verification"
    VERIFICATION_FAILED = "verification_failed"
    APPROVE_CODE = "approve_code"
    REQUEST_CODE_CHANGES = "request_code_changes"
    ACCEPT_DELIVERY = "accept_delivery"
    REQUEST_ACCEPTANCE_CHANGES = "request_acceptance_changes"
    SUBMIT_IMPLEMENTATION_CHANGES = "submit_implementation_changes"
    SUBMIT_FOLLOW_UP = "submit_follow_up"
    REVISE_SCOPE = "revise_scope"
    START_RELEASE_PREPARATION = "start_release_preparation"
    COMPLETE_RELEASE_PREPARATION = "complete_release_preparation"
    APPROVE_RELEASE = "approve_release"
    REQUEST_RELEASE_CHANGES = "request_release_changes"
    RECORD_RELEASE = "record_release"
    START_RELEASE_VERIFICATION = "start_release_verification"
    COMPLETE_RELEASE_VERIFICATION = "complete_release_verification"
    ASK_QUESTIONS = "ask_questions"
    BLOCK_STAGE = "block_stage"
    SUBMIT_REFINEMENT_ANSWERS = "submit_refinement_answers"
    SUBMIT_PLANNING_ANSWERS = "submit_planning_answers"
    SUBMIT_DEVELOPMENT_ANSWERS = "submit_development_answers"
    SUBMIT_VERIFICATION_ANSWERS = "submit_verification_answers"
    SUBMIT_RELEASE_PREPARATION_ANSWERS = "submit_release_preparation_answers"
    SUBMIT_RELEASE_VERIFICATION_ANSWERS = "submit_release_verification_answers"
    RESUME_REFINEMENT = "resume_refinement"
    RESUME_PLANNING = "resume_planning"
    RESUME_DEVELOPMENT = "resume_development"
    RESUME_VERIFICATION = "resume_verification"
    RESUME_RELEASE_PREPARATION = "resume_release_preparation"
    RESUME_RELEASE_VERIFICATION = "resume_release_verification"
    CANCEL = "cancel"


# Transition display names from the setup instructions. Configurable per site.
DEFAULT_ACTION_NAMES: dict[Action, str] = {
    Action.SUBMIT_REFINEMENT: "Submit for refinement",
    Action.START_REFINEMENT: "Start refinement",
    Action.COMPLETE_REFINEMENT: "Complete refinement",
    Action.APPROVE_SPECIFICATION: "Approve specification",
    Action.REQUEST_SPECIFICATION_CHANGES: "Request specification changes",
    Action.START_PLANNING: "Start planning",
    Action.COMPLETE_PLANNING: "Complete planning",
    Action.APPROVE_PLAN: "Approve plan",
    Action.REQUEST_PLAN_CHANGES: "Request plan changes",
    Action.START_DEVELOPMENT: "Start development",
    Action.COMPLETE_DEVELOPMENT: "Complete development",
    Action.START_VERIFICATION: "Start verification",
    Action.COMPLETE_VERIFICATION: "Complete verification",
    Action.VERIFICATION_FAILED: "Verification failed",
    Action.APPROVE_CODE: "Approve code",
    Action.REQUEST_CODE_CHANGES: "Request code changes",
    Action.ACCEPT_DELIVERY: "Accept delivery",
    Action.REQUEST_ACCEPTANCE_CHANGES: "Request acceptance changes",
    Action.SUBMIT_IMPLEMENTATION_CHANGES: "Submit implementation changes",
    Action.SUBMIT_FOLLOW_UP: "Submit follow-up changes",
    Action.REVISE_SCOPE: "Revise scope",
    Action.START_RELEASE_PREPARATION: "Start release preparation",
    Action.COMPLETE_RELEASE_PREPARATION: "Complete release preparation",
    Action.APPROVE_RELEASE: "Approve release",
    Action.REQUEST_RELEASE_CHANGES: "Request release changes",
    Action.RECORD_RELEASE: "Record release",
    Action.START_RELEASE_VERIFICATION: "Start release verification",
    Action.COMPLETE_RELEASE_VERIFICATION: "Complete release verification",
    Action.ASK_QUESTIONS: "Ask questions",
    Action.BLOCK_STAGE: "Block stage",
    Action.SUBMIT_REFINEMENT_ANSWERS: "Submit refinement answers",
    Action.SUBMIT_PLANNING_ANSWERS: "Submit planning answers",
    Action.SUBMIT_DEVELOPMENT_ANSWERS: "Submit development answers",
    Action.SUBMIT_VERIFICATION_ANSWERS: "Submit verification answers",
    Action.SUBMIT_RELEASE_PREPARATION_ANSWERS: "Submit release preparation answers",
    Action.SUBMIT_RELEASE_VERIFICATION_ANSWERS: "Submit release verification answers",
    Action.RESUME_REFINEMENT: "Resume refinement",
    Action.RESUME_PLANNING: "Resume planning",
    Action.RESUME_DEVELOPMENT: "Resume development",
    Action.RESUME_VERIFICATION: "Resume verification",
    Action.RESUME_RELEASE_PREPARATION: "Resume release preparation",
    Action.RESUME_RELEASE_VERIFICATION: "Resume release verification",
    Action.CANCEL: "Cancel",
}


class Actor(StrEnum):
    HUMAN = "human"
    COORDINATOR = "coordinator"


class Requirement(StrEnum):
    """Input that must be present and valid before the destination stage starts."""

    NONE = "none"
    BRIEF = "brief"
    SPEC_APPROVAL = "spec_approval"
    SPEC_CHANGES = "spec_changes"
    PLAN_APPROVAL = "plan_approval"
    PLAN_CHANGES = "plan_changes"
    CODE_APPROVAL = "code_approval"
    CODE_CHANGES = "code_changes"
    ACCEPTANCE = "acceptance"
    ACCEPTANCE_CHANGES = "acceptance_changes"
    IMPLEMENTATION_CHANGES = "implementation_changes"
    SCOPE_REVISION = "scope_revision"
    RELEASE_APPROVAL = "release_approval"
    RELEASE_CHANGES = "release_changes"
    RELEASE_RECORD = "release_record"
    CLARIFICATION_ANSWERS = "clarification_answers"
    BLOCKER_RESOLVED = "blocker_resolved"
    CANCEL_REASON = "cancel_reason"
    STAGE_SUCCESS = "stage_success"


@dataclass(frozen=True)
class StageDef:
    stage: Stage
    ready: Status
    active: Status
    success: Status
    start_action: Action
    complete_action: Action
    answers_action: Action
    resume_action: Action
    procedures: tuple[str, ...]
    round_code: str


STAGES: dict[Stage, StageDef] = {
    Stage.REFINEMENT: StageDef(
        Stage.REFINEMENT,
        Status.READY_REFINEMENT,
        Status.REFINING,
        Status.SPECIFICATION_REVIEW,
        Action.START_REFINEMENT,
        Action.COMPLETE_REFINEMENT,
        Action.SUBMIT_REFINEMENT_ANSWERS,
        Action.RESUME_REFINEMENT,
        ("refine-ticket",),
        "REFINE",
    ),
    Stage.PLANNING: StageDef(
        Stage.PLANNING,
        Status.READY_PLANNING,
        Status.PLANNING,
        Status.PLAN_REVIEW,
        Action.START_PLANNING,
        Action.COMPLETE_PLANNING,
        Action.SUBMIT_PLANNING_ANSWERS,
        Action.RESUME_PLANNING,
        ("plan-ticket",),
        "PLAN",
    ),
    Stage.DEVELOPMENT: StageDef(
        Stage.DEVELOPMENT,
        Status.READY_DEVELOPMENT,
        Status.DEVELOPING,
        Status.READY_VERIFICATION,
        Action.START_DEVELOPMENT,
        Action.COMPLETE_DEVELOPMENT,
        Action.SUBMIT_DEVELOPMENT_ANSWERS,
        Action.RESUME_DEVELOPMENT,
        ("implement-ticket",),
        "DEV",
    ),
    Stage.VERIFICATION: StageDef(
        Stage.VERIFICATION,
        Status.READY_VERIFICATION,
        Status.VERIFYING,
        Status.CODE_REVIEW,
        Action.START_VERIFICATION,
        Action.COMPLETE_VERIFICATION,
        Action.SUBMIT_VERIFICATION_ANSWERS,
        Action.RESUME_VERIFICATION,
        ("review-ticket", "verify-ticket"),
        "VERIFY",
    ),
    Stage.RELEASE_PREPARATION: StageDef(
        Stage.RELEASE_PREPARATION,
        Status.READY_RELEASE_PREPARATION,
        Status.PREPARING_RELEASE,
        Status.RELEASE_REVIEW,
        Action.START_RELEASE_PREPARATION,
        Action.COMPLETE_RELEASE_PREPARATION,
        Action.SUBMIT_RELEASE_PREPARATION_ANSWERS,
        Action.RESUME_RELEASE_PREPARATION,
        ("prepare-release",),
        "RELPREP",
    ),
    Stage.RELEASE_VERIFICATION: StageDef(
        Stage.RELEASE_VERIFICATION,
        Status.READY_RELEASE_VERIFICATION,
        Status.VERIFYING_RELEASE,
        Status.DONE,
        Action.START_RELEASE_VERIFICATION,
        Action.COMPLETE_RELEASE_VERIFICATION,
        Action.SUBMIT_RELEASE_VERIFICATION_ANSWERS,
        Action.RESUME_RELEASE_VERIFICATION,
        ("verify-release",),
        "RELVERIFY",
    ),
}

READY_STATUSES = frozenset(d.ready for d in STAGES.values())
ACTIVE_STATUSES = frozenset(d.active for d in STAGES.values())
_BY_READY = {d.ready: d for d in STAGES.values()}
_BY_ACTIVE = {d.active: d for d in STAGES.values()}


def stage_for_ready(status: Status) -> StageDef | None:
    return _BY_READY.get(status)


def stage_for_active(status: Status) -> StageDef | None:
    return _BY_ACTIVE.get(status)


@dataclass(frozen=True)
class Route:
    source: Status
    action: Action
    target: Status
    actor: Actor
    requires: Requirement
    resume_stage: Stage | None = None


def _human(src: Status, act: Action, dst: Status, req: Requirement) -> Route:
    return Route(src, act, dst, Actor.HUMAN, req)


_HUMAN_MAIN: tuple[Route, ...] = (
    _human(Status.BACKLOG, Action.SUBMIT_REFINEMENT, Status.READY_REFINEMENT, Requirement.BRIEF),
    _human(
        Status.SPECIFICATION_REVIEW,
        Action.APPROVE_SPECIFICATION,
        Status.READY_PLANNING,
        Requirement.SPEC_APPROVAL,
    ),
    _human(
        Status.SPECIFICATION_REVIEW,
        Action.REQUEST_SPECIFICATION_CHANGES,
        Status.READY_REFINEMENT,
        Requirement.SPEC_CHANGES,
    ),
    _human(Status.PLAN_REVIEW, Action.APPROVE_PLAN, Status.READY_DEVELOPMENT, Requirement.PLAN_APPROVAL),
    _human(
        Status.PLAN_REVIEW,
        Action.REQUEST_PLAN_CHANGES,
        Status.READY_PLANNING,
        Requirement.PLAN_CHANGES,
    ),
    _human(Status.CODE_REVIEW, Action.APPROVE_CODE, Status.ACCEPTANCE_REVIEW, Requirement.CODE_APPROVAL),
    _human(
        Status.CODE_REVIEW,
        Action.REQUEST_CODE_CHANGES,
        Status.CHANGES_REQUESTED,
        Requirement.CODE_CHANGES,
    ),
    _human(
        Status.ACCEPTANCE_REVIEW,
        Action.ACCEPT_DELIVERY,
        Status.READY_RELEASE_PREPARATION,
        Requirement.ACCEPTANCE,
    ),
    _human(
        Status.ACCEPTANCE_REVIEW,
        Action.REQUEST_ACCEPTANCE_CHANGES,
        Status.CHANGES_REQUESTED,
        Requirement.ACCEPTANCE_CHANGES,
    ),
    _human(
        Status.CHANGES_REQUESTED,
        Action.SUBMIT_IMPLEMENTATION_CHANGES,
        Status.READY_DEVELOPMENT,
        Requirement.IMPLEMENTATION_CHANGES,
    ),
    _human(
        Status.CHANGES_REQUESTED,
        Action.REVISE_SCOPE,
        Status.READY_REFINEMENT,
        Requirement.SCOPE_REVISION,
    ),
    _human(
        Status.RELEASE_REVIEW,
        Action.APPROVE_RELEASE,
        Status.READY_RELEASE,
        Requirement.RELEASE_APPROVAL,
    ),
    _human(
        Status.RELEASE_REVIEW,
        Action.REQUEST_RELEASE_CHANGES,
        Status.READY_RELEASE_PREPARATION,
        Requirement.RELEASE_CHANGES,
    ),
    _human(
        Status.READY_RELEASE,
        Action.RECORD_RELEASE,
        Status.READY_RELEASE_VERIFICATION,
        Requirement.RELEASE_RECORD,
    ),
)

_HUMAN_RESUME: tuple[Route, ...] = tuple(
    Route(
        paused,
        d.answers_action if paused is Status.NEEDS_CLARIFICATION else d.resume_action,
        d.ready,
        Actor.HUMAN,
        Requirement.CLARIFICATION_ANSWERS
        if paused is Status.NEEDS_CLARIFICATION
        else Requirement.BLOCKER_RESOLVED,
        d.stage,
    )
    for d in STAGES.values()
    for paused in (Status.NEEDS_CLARIFICATION, Status.BLOCKED)
)

_UNFINISHED = tuple(s for s in Status if s not in TERMINAL_STATUSES)
_HUMAN_CANCEL: tuple[Route, ...] = tuple(
    Route(s, Action.CANCEL, Status.CANCELLED, Actor.HUMAN, Requirement.CANCEL_REASON) for s in _UNFINISHED
)


def _coordinator_routes() -> tuple[Route, ...]:
    routes: list[Route] = []
    for d in STAGES.values():
        c = Actor.COORDINATOR
        routes.append(Route(d.ready, d.start_action, d.active, c, Requirement.NONE))
        routes.append(Route(d.active, d.complete_action, d.success, c, Requirement.STAGE_SUCCESS))
        routes.append(Route(d.active, Action.ASK_QUESTIONS, Status.NEEDS_CLARIFICATION, c, Requirement.NONE))
        routes.append(Route(d.active, Action.BLOCK_STAGE, Status.BLOCKED, c, Requirement.NONE))
    routes.append(
        Route(
            Status.VERIFYING,
            Action.VERIFICATION_FAILED,
            Status.CHANGES_REQUESTED,
            Actor.COORDINATOR,
            Requirement.NONE,
        )
    )
    return tuple(routes)


# A developer can keep talking to a finished development session (interactive sessions with
# keep_open). The coordinator publishes changes made there as a new candidate, which must be
# verified and reviewed again, so the ticket returns to Ready for verification (where it waits
# until that session has closed: see delivery.open_sessions).
FOLLOW_UP_SOURCES = (Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW, Status.CHANGES_REQUESTED)
FOLLOW_UP_STATUSES = frozenset({Status.READY_VERIFICATION, *FOLLOW_UP_SOURCES})
FOLLOW_UP_ROUTES: tuple[Route, ...] = tuple(
    Route(s, Action.SUBMIT_FOLLOW_UP, Status.READY_VERIFICATION, Actor.COORDINATOR, Requirement.STAGE_SUCCESS)
    for s in FOLLOW_UP_SOURCES
)

# The pilot release is the human merge of the PR. The coordinator reads that merge from GitHub
# and records it itself (it never merges or deploys), so nobody has to copy the merge commit into
# Jira. Humans may still choose Record release; the release is then read from GitHub as well,
# unless a RECORD RELEASE comment names the commit.
RECORD_RELEASE_ROUTE = Route(
    Status.READY_RELEASE,
    Action.RECORD_RELEASE,
    Status.READY_RELEASE_VERIFICATION,
    Actor.COORDINATOR,
    Requirement.RELEASE_RECORD,
)

ROUTES: tuple[Route, ...] = (
    _HUMAN_MAIN
    + _HUMAN_RESUME
    + _HUMAN_CANCEL
    + _coordinator_routes()
    + FOLLOW_UP_ROUTES
    + (RECORD_RELEASE_ROUTE,)
)


class IllegalTransition(Exception):
    pass


def coordinator_route(source: Status, action: Action) -> Route:
    """Return the coordinator route for an action, refusing anything not explicitly permitted."""
    for r in ROUTES:
        if r.actor is Actor.COORDINATOR and r.source is source and r.action is action:
            return r
    raise IllegalTransition(f"coordinator may not perform {action.value} from {source.value}")


def human_routes_into(target: Status) -> tuple[Route, ...]:
    return tuple(r for r in ROUTES if r.actor is Actor.HUMAN and r.target is target)


def human_route(source: Status, target: Status) -> tuple[Route, ...]:
    """Human routes from source to target. Several exist only for paused statuses."""
    return tuple(r for r in ROUTES if r.actor is Actor.HUMAN and r.source is source and r.target is target)


def resume_route(paused: Status, target: Status, recorded_stage: Stage | None) -> Route | None:
    """Return the resume route only when it returns to the recorded originating stage."""
    if paused not in PAUSED_STATUSES or recorded_stage is None:
        return None
    for r in human_route(paused, target):
        if r.resume_stage is recorded_stage:
            return r
    return None


def route_for_action(source: Status, action: Action) -> Route | None:
    for r in ROUTES:
        if r.source is source and r.action is action:
            return r
    return None


def all_actions_by_status() -> dict[Status, tuple[Route, ...]]:
    out: dict[Status, list[Route]] = {s: [] for s in Status}
    for r in ROUTES:
        out[r.source].append(r)
    return {k: tuple(v) for k, v in out.items()}


# Board grouping from the setup instructions. Visual only; never a trigger.
BOARD_COLUMNS: tuple[tuple[str, tuple[Status, ...]], ...] = (
    ("Backlog", (Status.BACKLOG,)),
    ("Ready", tuple(d.ready for d in STAGES.values())),
    ("Agent working", tuple(d.active for d in STAGES.values())),
    ("Needs clarification", (Status.NEEDS_CLARIFICATION,)),
    (
        "Human review",
        (
            Status.SPECIFICATION_REVIEW,
            Status.PLAN_REVIEW,
            Status.CODE_REVIEW,
            Status.ACCEPTANCE_REVIEW,
            Status.RELEASE_REVIEW,
            Status.CHANGES_REQUESTED,
        ),
    ),
    ("Blocked", (Status.BLOCKED,)),
    ("Ready for release", (Status.READY_RELEASE,)),
    ("Done", (Status.DONE, Status.CANCELLED)),
)
