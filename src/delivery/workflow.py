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
    # Not part of the lifecycle: a developer-attended session that clears a blocker, after which
    # the ticket goes back to the stage that blocked (see STAGES and RESOLVED_ACTIONS).
    RESOLUTION = "resolution"

    @classmethod
    def _missing_(cls, value: object) -> Stage | None:
        # Records written before release verification was removed.
        return cls.RELEASE_PREPARATION if value == "release_verification" else None


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
    READY_RESOLUTION = "ready_resolution"
    RESOLVING = "resolving"
    READY_RELEASE_PREPARATION = "ready_release_preparation"
    PREPARING_RELEASE = "preparing_release"
    RELEASE_REVIEW = "release_review"
    READY_RELEASE = "ready_release"
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
    Status.READY_RESOLUTION: "Ready for resolution",
    Status.RESOLVING: "Resolving",
    Status.READY_RELEASE_PREPARATION: "Ready for release preparation",
    Status.PREPARING_RELEASE: "Preparing release",
    Status.RELEASE_REVIEW: "Release review",
    Status.READY_RELEASE: "Ready for release",
    Status.DONE: "Done",
    Status.CANCELLED: "Cancelled",
}

_TODO = {
    Status.BACKLOG,
    Status.READY_REFINEMENT,
    Status.READY_PLANNING,
    Status.READY_DEVELOPMENT,
    Status.READY_VERIFICATION,
    Status.READY_RESOLUTION,
    Status.READY_RELEASE_PREPARATION,
    Status.READY_RELEASE,
}
_DONE = {Status.DONE, Status.CANCELLED}

STATUS_CATEGORIES: dict[Status, Category] = {
    s: Category.TODO if s in _TODO else Category.DONE if s in _DONE else Category.IN_PROGRESS for s in Status
}

TERMINAL_STATUSES = frozenset(_DONE)
# Statuses a project may not have: resolution is opt-in, so a Jira project without them is valid.
OPTIONAL_STATUSES = frozenset({Status.READY_RESOLUTION, Status.RESOLVING})
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
    ASK_QUESTIONS = "ask_questions"
    BLOCK_STAGE = "block_stage"
    SUBMIT_REFINEMENT_ANSWERS = "submit_refinement_answers"
    SUBMIT_PLANNING_ANSWERS = "submit_planning_answers"
    SUBMIT_DEVELOPMENT_ANSWERS = "submit_development_answers"
    SUBMIT_VERIFICATION_ANSWERS = "submit_verification_answers"
    SUBMIT_RELEASE_PREPARATION_ANSWERS = "submit_release_preparation_answers"
    RESUME_REFINEMENT = "resume_refinement"
    RESUME_PLANNING = "resume_planning"
    RESUME_DEVELOPMENT = "resume_development"
    RESUME_VERIFICATION = "resume_verification"
    RESUME_RELEASE_PREPARATION = "resume_release_preparation"
    REQUEST_RESOLUTION = "request_resolution"
    START_RESOLUTION = "start_resolution"
    RESOLVED_REFINEMENT = "resolved_refinement"
    RESOLVED_PLANNING = "resolved_planning"
    RESOLVED_DEVELOPMENT = "resolved_development"
    RESOLVED_VERIFICATION = "resolved_verification"
    RESOLVED_RELEASE_PREPARATION = "resolved_release_preparation"
    CANCEL = "cancel"
    # Optional routes (OPTIONAL_ROUTES): each needs one more transition in Jira.
    USE_APPROVED_PLAN = "use_approved_plan"
    COMPLETE_SPIKE = "complete_spike"


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
    Action.ASK_QUESTIONS: "Ask questions",
    Action.BLOCK_STAGE: "Block stage",
    Action.SUBMIT_REFINEMENT_ANSWERS: "Submit refinement answers",
    Action.SUBMIT_PLANNING_ANSWERS: "Submit planning answers",
    Action.SUBMIT_DEVELOPMENT_ANSWERS: "Submit development answers",
    Action.SUBMIT_VERIFICATION_ANSWERS: "Submit verification answers",
    Action.SUBMIT_RELEASE_PREPARATION_ANSWERS: "Submit release preparation answers",
    Action.RESUME_REFINEMENT: "Resume refinement",
    Action.RESUME_PLANNING: "Resume planning",
    Action.RESUME_DEVELOPMENT: "Resume development",
    Action.RESUME_VERIFICATION: "Resume verification",
    Action.RESUME_RELEASE_PREPARATION: "Resume release preparation",
    Action.REQUEST_RESOLUTION: "Request resolution",
    Action.START_RESOLUTION: "Start resolution",
    Action.RESOLVED_REFINEMENT: "Resolved: resume refinement",
    Action.RESOLVED_PLANNING: "Resolved: resume planning",
    Action.RESOLVED_DEVELOPMENT: "Resolved: resume development",
    Action.RESOLVED_VERIFICATION: "Resolved: resume verification",
    Action.RESOLVED_RELEASE_PREPARATION: "Resolved: resume release preparation",
    Action.CANCEL: "Cancel",
    Action.USE_APPROVED_PLAN: "Use approved plan",
    Action.COMPLETE_SPIKE: "Complete spike",
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
    CLARIFICATION_ANSWERS = "clarification_answers"
    BLOCKER_RESOLVED = "blocker_resolved"
    CANCEL_REASON = "cancel_reason"
    STAGE_SUCCESS = "stage_success"
    PLAN_WITH_SPECIFICATION = "plan_with_specification"
    RESOLUTION_REQUEST = "resolution_request"
    RESOLVED = "resolved"


# Requirements of the routes by which a person asks for changes: the move alone is the request,
# and what to change is whatever people wrote (or, when nobody did, Claude asks).
CHANGE_REQUIREMENTS = frozenset(
    {
        Requirement.SPEC_CHANGES,
        Requirement.PLAN_CHANGES,
        Requirement.IMPLEMENTATION_CHANGES,
        Requirement.SCOPE_REVISION,
        Requirement.RELEASE_CHANGES,
    }
)


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


LIFECYCLE_STAGES: dict[Stage, StageDef] = {
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
}

# Resolution reuses the ready/active shape so intake, the executor and the console treat it like a
# stage, but it is not part of the lifecycle: it has no success status (the ticket returns to the
# ready status of the stage that blocked, by one of RESOLVED_ACTIONS), no clarification round and
# no resume. The unused fields below say Block stage, the only other way out.
RESOLUTION_STAGE = StageDef(
    Stage.RESOLUTION,
    Status.READY_RESOLUTION,
    Status.RESOLVING,
    Status.BLOCKED,
    Action.START_RESOLUTION,
    Action.BLOCK_STAGE,
    Action.BLOCK_STAGE,
    Action.BLOCK_STAGE,
    ("resolve-blocker",),
    "RESOLVE",
)
STAGES: dict[Stage, StageDef] = {**LIFECYCLE_STAGES, Stage.RESOLUTION: RESOLUTION_STAGE}

# The action that returns a resolved ticket to the stage that blocked.
RESOLVED_ACTIONS: dict[Stage, Action] = {
    Stage.REFINEMENT: Action.RESOLVED_REFINEMENT,
    Stage.PLANNING: Action.RESOLVED_PLANNING,
    Stage.DEVELOPMENT: Action.RESOLVED_DEVELOPMENT,
    Stage.VERIFICATION: Action.RESOLVED_VERIFICATION,
    Stage.RELEASE_PREPARATION: Action.RESOLVED_RELEASE_PREPARATION,
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
    for d in LIFECYCLE_STAGES.values()
    for paused in (Status.NEEDS_CLARIFICATION, Status.BLOCKED)
)

_UNFINISHED = tuple(s for s in Status if s not in TERMINAL_STATUSES)
_HUMAN_CANCEL: tuple[Route, ...] = tuple(
    Route(s, Action.CANCEL, Status.CANCELLED, Actor.HUMAN, Requirement.CANCEL_REASON) for s in _UNFINISHED
)


def _coordinator_routes() -> tuple[Route, ...]:
    routes: list[Route] = []
    for d in LIFECYCLE_STAGES.values():
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
# (it never merges or deploys), checks that what was merged is the accepted candidate and moves
# the ticket to Done. Nobody records anything in Jira.
RECORD_RELEASE_ROUTE = Route(
    Status.READY_RELEASE,
    Action.RECORD_RELEASE,
    Status.DONE,
    Actor.COORDINATOR,
    Requirement.NONE,
)

# A release proposal is optional (``[release] proposal``). Without one, accepting the delivery
# goes straight to Ready for release: the PR merge is the release and the ticket has no release
# preparation, release review or the statuses that go with them. Which of the two Accept delivery
# transitions a Jira project has is the Jira project's choice of target status; the coordinator
# reads the move it finds.
PROPOSAL_STATUSES = frozenset(
    {Status.READY_RELEASE_PREPARATION, Status.PREPARING_RELEASE, Status.RELEASE_REVIEW}
)
DIRECT_RELEASE_ROUTE = Route(
    Status.ACCEPTANCE_REVIEW,
    Action.ACCEPT_DELIVERY,
    Status.READY_RELEASE,
    Actor.HUMAN,
    Requirement.ACCEPTANCE,
)

# Optional routes for kinds of work, each needing one more transition in Jira (added only by
# teams that use it; without it the coordinator takes the full route instead):
# * a fast-track ticket's plan is written and approved with its specification, so planning
#   publishes that plan as approved and goes straight on to development;
# * a spike ends when its findings are approved: there is nothing to build or release.
FAST_TRACK_ROUTE = Route(
    Status.PLANNING,
    Action.USE_APPROVED_PLAN,
    Status.READY_DEVELOPMENT,
    Actor.COORDINATOR,
    Requirement.PLAN_WITH_SPECIFICATION,
)
SPIKE_ROUTE = Route(
    Status.READY_DEVELOPMENT, Action.COMPLETE_SPIKE, Status.DONE, Actor.COORDINATOR, Requirement.PLAN_APPROVAL
)
# Resolution (optional, like the two above): a developer moves a Blocked ticket to Ready for
# resolution; the coordinator opens a Claude session that clears the blocker with them and
# returns the ticket to the stage that blocked, or to Blocked when it could not. Each resolved
# route carries its stage so Jira can hide the other four by the resume stage field.
RESOLUTION_ROUTES: tuple[Route, ...] = (
    Route(
        Status.BLOCKED,
        Action.REQUEST_RESOLUTION,
        Status.READY_RESOLUTION,
        Actor.HUMAN,
        Requirement.RESOLUTION_REQUEST,
    ),
    Route(
        Status.READY_RESOLUTION,
        Action.START_RESOLUTION,
        Status.RESOLVING,
        Actor.COORDINATOR,
        Requirement.NONE,
    ),
    Route(Status.RESOLVING, Action.BLOCK_STAGE, Status.BLOCKED, Actor.COORDINATOR, Requirement.NONE),
    *(
        Route(
            Status.RESOLVING,
            RESOLVED_ACTIONS[d.stage],
            d.ready,
            Actor.COORDINATOR,
            Requirement.RESOLVED,
            d.stage,
        )
        for d in LIFECYCLE_STAGES.values()
    ),
)
OPTIONAL_ROUTES: tuple[Route, ...] = (FAST_TRACK_ROUTE, SPIKE_ROUTE, *RESOLUTION_ROUTES)

ROUTES: tuple[Route, ...] = (
    _HUMAN_MAIN
    + _HUMAN_RESUME
    + _HUMAN_CANCEL
    + _coordinator_routes()
    + FOLLOW_UP_ROUTES
    + (RECORD_RELEASE_ROUTE, DIRECT_RELEASE_ROUTE)
    + OPTIONAL_ROUTES
)


def routes_for(proposal: bool) -> tuple[Route, ...]:
    """The routes a Jira project needs: with a release proposal the release preparation statuses
    and their routes, without one the direct route from Acceptance review to Ready for release."""
    if proposal:
        return tuple(r for r in ROUTES if r is not DIRECT_RELEASE_ROUTE)
    return tuple(
        r
        for r in ROUTES
        if r.source not in PROPOSAL_STATUSES
        and r.target not in PROPOSAL_STATUSES
        and not (r.action is Action.ACCEPT_DELIVERY and r.target is Status.READY_RELEASE_PREPARATION)
    )


def required_statuses(proposal: bool) -> frozenset[Status]:
    """Statuses a Jira project must have (resolution is optional either way)."""
    wanted = set(Status) - OPTIONAL_STATUSES
    return frozenset(wanted if proposal else wanted - PROPOSAL_STATUSES)


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
