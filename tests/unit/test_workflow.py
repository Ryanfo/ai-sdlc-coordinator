from __future__ import annotations

import pytest

from delivery.workflow import (
    ACTIVE_STATUSES,
    BOARD_COLUMNS,
    READY_STATUSES,
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
    resume_route,
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
    (Status.READY_RELEASE, Status.READY_RELEASE_VERIFICATION, Requirement.RELEASE_RECORD),
]


@pytest.mark.parametrize(("src", "dst", "req"), HUMAN_TABLE)
def test_every_human_route_exists(src: Status, dst: Status, req: Requirement) -> None:
    routes = human_route(src, dst)
    assert len(routes) == 1
    assert routes[0].requires is req


def test_every_stage_has_start_complete_question_block_routes() -> None:
    for d in STAGES.values():
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


def test_coordinator_records_a_merged_release_on_the_human_route() -> None:
    # The human merge is the release; the coordinator records it on the same Jira transition a
    # human would use (and humans still may), with the same requirement checked on arrival.
    route = coordinator_route(Status.READY_RELEASE, Action.RECORD_RELEASE)
    assert route.target is Status.READY_RELEASE_VERIFICATION
    assert route.requires is Requirement.RELEASE_RECORD
    assert human_route(Status.READY_RELEASE, Status.READY_RELEASE_VERIFICATION)


def test_no_global_arbitrary_transition() -> None:
    targets_from_backlog = {r.target for r in ROUTES if r.source is Status.BACKLOG}
    assert targets_from_backlog == {Status.READY_REFINEMENT, Status.CANCELLED}
    # Done and Cancelled have no outgoing routes (no automatic reopen in v1).
    assert not [r for r in ROUTES if r.source in (Status.DONE, Status.CANCELLED)]


@pytest.mark.parametrize("stage", list(Stage))
def test_resume_only_to_recorded_stage(stage: Stage) -> None:
    d = STAGES[stage]
    for paused in (Status.NEEDS_CLARIFICATION, Status.BLOCKED):
        assert resume_route(paused, d.ready, stage) is not None
        for other in Stage:
            if other is not stage:
                assert resume_route(paused, STAGES[other].ready, stage) is None
        assert resume_route(paused, d.ready, None) is None


def test_failed_release_verification_routes_to_blocked() -> None:
    r = coordinator_route(Status.VERIFYING_RELEASE, Action.BLOCK_STAGE)
    assert r.target is Status.BLOCKED


def test_board_maps_every_status_exactly_once() -> None:
    mapped = [s for _, statuses in BOARD_COLUMNS for s in statuses]
    assert sorted(mapped) == sorted(Status)
    assert len(mapped) == len(set(mapped))


def test_ready_and_active_sets() -> None:
    assert len(READY_STATUSES) == 6
    assert len(ACTIVE_STATUSES) == 6
    assert Status.READY_RELEASE not in READY_STATUSES  # human release gate, not a machine queue


def test_cancel_from_every_unfinished_status_is_human_only() -> None:
    cancels = [r for r in ROUTES if r.action is Action.CANCEL]
    assert all(r.actor is Actor.HUMAN for r in cancels)
    assert len(cancels) == len(Status) - 2
