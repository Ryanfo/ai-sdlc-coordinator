from __future__ import annotations

import pytest

from delivery.workflow import (
    ACTIVE_STATUSES,
    BOARD_COLUMNS,
    LIFECYCLE_STAGES,
    PROPOSAL_STATUSES,
    READY_STATUSES,
    RESOLVED_ACTIONS,
    ROUTES,
    STAGES,
    Action,
    Actor,
    IllegalTransition,
    Requirement,
    Stage,
    Status,
    coordinator_route,
    human_route,
    required_statuses,
    resume_route,
    routes_for,
)

# Every human route from the handoff §6 table: (from, to, requirement)
HUMAN_TABLE = [
    (Status.BACKLOG, Status.READY_REFINEMENT, Requirement.BRIEF),
    (Status.SPECIFICATION_REVIEW, Status.READY_PLANNING, Requirement.SPEC_APPROVAL),
    (Status.SPECIFICATION_REVIEW, Status.READY_REFINEMENT, Requirement.SPEC_CHANGES),
    (Status.PLAN_REVIEW, Status.READY_DEVELOPMENT, Requirement.PLAN_APPROVAL),
    (Status.PLAN_REVIEW, Status.READY_PLANNING, Requirement.PLAN_CHANGES),
    (Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW, Requirement.CODE_APPROVAL),
    (Status.CODE_REVIEW, Status.CHANGES_REQUESTED, Requirement.CODE_CHANGES),
    (Status.ACCEPTANCE_REVIEW, Status.READY_RELEASE_PREPARATION, Requirement.ACCEPTANCE),
    (Status.ACCEPTANCE_REVIEW, Status.CHANGES_REQUESTED, Requirement.ACCEPTANCE_CHANGES),
    (Status.CHANGES_REQUESTED, Status.READY_DEVELOPMENT, Requirement.IMPLEMENTATION_CHANGES),
    (Status.CHANGES_REQUESTED, Status.READY_REFINEMENT, Requirement.SCOPE_REVISION),
    (Status.RELEASE_REVIEW, Status.READY_RELEASE, Requirement.RELEASE_APPROVAL),
    (Status.RELEASE_REVIEW, Status.READY_RELEASE_PREPARATION, Requirement.RELEASE_CHANGES),
    # Without a release proposal, accepting the delivery goes straight to Ready for release.
    (Status.ACCEPTANCE_REVIEW, Status.READY_RELEASE, Requirement.ACCEPTANCE),
]


@pytest.mark.parametrize(("src", "dst", "req"), HUMAN_TABLE)
def test_every_human_route_exists(src: Status, dst: Status, req: Requirement) -> None:
    routes = human_route(src, dst)
    assert len(routes) == 1
    assert routes[0].requires is req


def test_every_stage_has_start_complete_question_block_routes() -> None:
    for d in LIFECYCLE_STAGES.values():
        assert coordinator_route(d.ready, d.start_action).target is d.active
        assert coordinator_route(d.active, d.complete_action).target is d.success
        assert coordinator_route(d.active, Action.ASK_QUESTIONS).target is Status.NEEDS_CLARIFICATION
        assert coordinator_route(d.active, Action.BLOCK_STAGE).target is Status.BLOCKED
    assert coordinator_route(Status.VERIFYING, Action.VERIFICATION_FAILED).target is Status.CHANGES_REQUESTED


@pytest.mark.parametrize(
    ("src", "action"),
    [
        (Status.SPECIFICATION_REVIEW, Action.APPROVE_SPECIFICATION),  # coordinator never approves
        (Status.CODE_REVIEW, Action.APPROVE_CODE),
        (Status.REFINING, Action.COMPLETE_PLANNING),  # wrong stage
        (Status.BACKLOG, Action.START_REFINEMENT),  # must be in ready status
        (Status.NEEDS_CLARIFICATION, Action.SUBMIT_REFINEMENT_ANSWERS),
        (Status.RELEASE_REVIEW, Action.CANCEL),
    ],
)
def test_coordinator_cannot_take_human_or_wrong_routes(src: Status, action: Action) -> None:
    with pytest.raises(IllegalTransition):
        coordinator_route(src, action)


def test_coordinator_completes_a_merged_release() -> None:
    # The human merge is the release; the coordinator moves the ticket to Done when it sees it.
    route = coordinator_route(Status.READY_RELEASE, Action.RECORD_RELEASE)
    assert route.target is Status.DONE
    assert not [
        r
        for r in ROUTES
        if r.source is Status.READY_RELEASE and r.actor is Actor.HUMAN and r.target is not Status.CANCELLED
    ]


def test_the_release_proposal_is_optional() -> None:
    with_proposal = routes_for(True)
    without = routes_for(False)
    assert {r.target for r in with_proposal if r.action is Action.ACCEPT_DELIVERY} == {
        Status.READY_RELEASE_PREPARATION
    }
    assert {r.target for r in without if r.action is Action.ACCEPT_DELIVERY} == {Status.READY_RELEASE}
    # Nothing in a Jira project without a proposal touches the three release preparation statuses.
    assert not [r for r in without if PROPOSAL_STATUSES & {r.source, r.target}]
    assert required_statuses(True) >= PROPOSAL_STATUSES
    assert not PROPOSAL_STATUSES & required_statuses(False)


def test_no_global_arbitrary_transition() -> None:
    targets_from_backlog = {r.target for r in ROUTES if r.source is Status.BACKLOG}
    assert targets_from_backlog == {Status.READY_REFINEMENT, Status.CANCELLED}
    # Done and Cancelled have no outgoing routes (no automatic reopen in v1).
    assert not [r for r in ROUTES if r.source in (Status.DONE, Status.CANCELLED)]


@pytest.mark.parametrize("stage", list(LIFECYCLE_STAGES))
def test_resume_only_to_recorded_stage(stage: Stage) -> None:
    d = STAGES[stage]
    for paused in (Status.NEEDS_CLARIFICATION, Status.BLOCKED):
        assert resume_route(paused, d.ready, stage) is not None
        for other in LIFECYCLE_STAGES:
            if other is not stage:
                assert resume_route(paused, STAGES[other].ready, stage) is None
        assert resume_route(paused, d.ready, None) is None


def test_board_maps_every_status_exactly_once() -> None:
    mapped = [s for _, statuses in BOARD_COLUMNS for s in statuses]
    assert sorted(mapped) == sorted(Status)
    assert len(mapped) == len(set(mapped))


def test_ready_and_active_sets() -> None:
    assert len(READY_STATUSES) == 6  # five stages and resolution
    assert len(ACTIVE_STATUSES) == 6
    assert Status.READY_RELEASE not in READY_STATUSES  # human release gate, not a machine queue


def test_cancel_from_every_unfinished_status_is_human_only() -> None:
    cancels = [r for r in ROUTES if r.action is Action.CANCEL]
    assert all(r.actor is Actor.HUMAN for r in cancels)
    assert len(cancels) == len(Status) - 2


# --------------------------------------------------------------------------- resolution


def test_resolution_is_a_stage_outside_the_lifecycle() -> None:
    assert Stage.RESOLUTION in STAGES and Stage.RESOLUTION not in LIFECYCLE_STAGES
    d = STAGES[Stage.RESOLUTION]
    assert (d.ready, d.active) == (Status.READY_RESOLUTION, Status.RESOLVING)
    assert coordinator_route(d.ready, d.start_action).target is Status.RESOLVING
    # It cannot ask questions or be resumed: questions are asked in the session, and the ticket
    # leaves Resolving only to the stage that blocked, or back to Blocked.
    with pytest.raises(IllegalTransition):
        coordinator_route(Status.RESOLVING, Action.ASK_QUESTIONS)
    assert not human_route(Status.NEEDS_CLARIFICATION, Status.READY_RESOLUTION)
    assert not human_route(Status.READY_RESOLUTION, Status.READY_REFINEMENT)


def test_only_blocked_tickets_can_be_sent_to_resolution() -> None:
    into = [r for r in ROUTES if r.target is Status.READY_RESOLUTION]
    assert [(r.source, r.actor) for r in into] == [(Status.BLOCKED, Actor.HUMAN)]
    assert into[0].requires is Requirement.RESOLUTION_REQUEST


def test_resolving_returns_to_the_stage_that_blocked_or_to_blocked() -> None:
    out = {(r.action, r.target, r.resume_stage) for r in ROUTES if r.source is Status.RESOLVING}
    for stage, action in RESOLVED_ACTIONS.items():
        assert (action, STAGES[stage].ready, stage) in out
        assert coordinator_route(Status.RESOLVING, action).requires is Requirement.RESOLVED
    assert (Action.BLOCK_STAGE, Status.BLOCKED, None) in out
    assert (Action.CANCEL, Status.CANCELLED, None) in out
    assert len(out) == len(RESOLVED_ACTIONS) + 2
    assert set(RESOLVED_ACTIONS) == set(LIFECYCLE_STAGES)


def test_resolution_routes_are_optional_in_jira() -> None:
    from delivery.workflow import OPTIONAL_ROUTES, OPTIONAL_STATUSES, RESOLUTION_ROUTES

    assert set(RESOLUTION_ROUTES) <= set(OPTIONAL_ROUTES)
    assert {Status.READY_RESOLUTION, Status.RESOLVING} == OPTIONAL_STATUSES
