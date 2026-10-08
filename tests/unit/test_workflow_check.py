from __future__ import annotations

import pytest

from conftest import DEV, STATUS_IDS, ConfigFactory
from delivery.ownership import evaluate_eligibility
from delivery.workflow import OPTIONAL_STATUSES, PROPOSAL_STATUSES, STATUS_NAMES, Action, Status, routes_for
from delivery.workflow_check import CHECK_LABEL, WALK, _with_proposal, _with_resolution, verify_workflow
from fakes.jira import FakeJira


def fake(proposal: bool = False) -> FakeJira:
    return FakeJira(STATUS_IDS, me=DEV, proposal=proposal)


@pytest.mark.parametrize(
    ("walk", "proposal", "unreached"),
    [
        (WALK, False, {Status.CANCELLED, *OPTIONAL_STATUSES, *PROPOSAL_STATUSES}),
        (_with_resolution(WALK), False, {Status.CANCELLED, *PROPOSAL_STATUSES}),
        (_with_proposal(WALK), True, {Status.CANCELLED, *OPTIONAL_STATUSES}),
        (_with_resolution(_with_proposal(WALK)), True, {Status.CANCELLED}),
    ],
)
def test_walk_follows_permitted_routes_and_visits_every_status(walk, proposal, unreached) -> None:  # type: ignore[no-untyped-def]
    status, seen = Status.BACKLOG, {Status.BACKLOG}
    routes = routes_for(proposal)
    for action, _ in walk:
        route = next((r for r in routes if r.source is status and r.action is action), None)
        assert route is not None, f"{action} from {status}"
        status = route.target
        seen.add(status)
    assert status is Status.DONE
    assert set(Status) - seen == unreached  # Cancelled is reached by the second ticket


async def test_matching_workflow_passes_and_leaves_unassigned_labelled_tickets(
    make_config: ConfigFactory,
) -> None:
    cfg, jira = make_config(), fake()
    report = await verify_workflow(cfg, jira, emit=lambda _: None)
    assert report.ok, report.problems
    assert report.warnings == []
    assert set(report.findings) == {s.value for s in Status if s not in PROPOSAL_STATUSES}
    assert report.transitions_done == len(_with_resolution(WALK)) + 1
    done, cancelled = (jira.issues[k] for k in report.tickets)
    assert (done.status, cancelled.status) == (Status.DONE, Status.CANCELLED)
    for issue in (done, cancelled):
        assert issue.assignee is None and CHECK_LABEL in issue.labels
        view = (await jira.get_issue(issue.key)).view
        assert not evaluate_eligibility(view, cfg).eligible
    assert any("hides resume actions" in i for i in report.info)
    assert any("resume field customfield_10050" in i for i in report.info)


async def test_missing_side_route_is_reported_and_walk_continues(make_config: ConfigFactory) -> None:
    jira = fake()
    jira.drop_routes.add((Status.SPECIFICATION_REVIEW, Status.READY_REFINEMENT))
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert not report.ok
    assert report.problems == [
        "Specification review: missing transition Request specification changes -> ready_refinement"
    ]
    assert jira.issues[report.tickets[0]].status is Status.DONE


async def test_missing_main_route_stops_the_walk(make_config: ConfigFactory) -> None:
    jira = fake()
    jira.drop_routes.add((Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW))
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert any("cannot continue" in p for p in report.problems)
    assert jira.issues[report.tickets[0]].status is Status.CODE_REVIEW


async def test_missing_resume_route_is_found_although_conditions_hide_it(make_config: ConfigFactory) -> None:
    jira = fake(proposal=True)
    jira.drop_routes.add((Status.BLOCKED, Status.READY_RELEASE_PREPARATION))
    report = await verify_workflow(
        make_config(overrides={"release": {"proposal": True}}), jira, emit=lambda _: None
    )
    assert report.problems == [
        "Blocked: missing transition Resume release preparation -> ready_release_preparation"
    ]


async def test_renamed_transition_is_reported_but_still_used(make_config: ConfigFactory) -> None:
    jira = fake()
    jira.action_names[Action.APPROVE_PLAN] = "Approve the plan"
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert "Plan review: missing transition Approve plan -> ready_development" in report.problems
    assert any("Approve the plan -> ready_development" in w for w in report.warnings)
    assert jira.issues[report.tickets[0]].status is Status.DONE


async def test_without_resume_field_jira_cannot_hide_wrong_resumes(make_config: ConfigFactory) -> None:
    cfg = make_config()
    cfg = cfg.model_copy(
        update={
            "jira": cfg.jira.model_copy(
                update={"fields": cfg.jira.fields.model_copy(update={"resume_stage": None})}
            )
        }
    )
    report = await verify_workflow(cfg, fake(), emit=lambda _: None)
    assert report.ok
    # Blocked, Needs clarification and (with resolution) Resolving each show every resume action.
    assert sum("resume field not configured" in w for w in report.warnings) == 3


@pytest.mark.parametrize("status", [Status.READY_REFINEMENT])
async def test_new_tickets_must_start_in_backlog(make_config: ConfigFactory, status: Status) -> None:
    jira = fake()
    original = jira.create

    def create(*a, **kw):  # type: ignore[no-untyped-def]
        kw["status"] = status
        return original(*a, **kw)

    jira.create = create  # type: ignore[method-assign]
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert report.problems == [f"new tickets start in {STATUS_NAMES[status]}, not Backlog"]


async def test_supported_issue_type_without_the_workflow_fails(make_config: ConfigFactory) -> None:
    jira = fake()
    jira.type_statuses["Task"] = {"90001", "90002", STATUS_IDS[Status.DONE]}
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert report.problems == [
        f"issue type Task does not use the delivery workflow (1 of {len(Status)} statuses): give it the "
        "workflow or remove it from jira.supported_issue_types"
    ]
    assert jira.issues[report.tickets[0]].issue_type == "Story"
    assert jira.issues[report.tickets[0]].status is Status.DONE


async def test_with_a_release_proposal_the_walk_goes_through_release_preparation(
    make_config: ConfigFactory,
) -> None:
    cfg = make_config(overrides={"release": {"proposal": True}})
    report = await verify_workflow(cfg, fake(proposal=True), emit=lambda _: None)
    assert report.ok, report.problems
    assert report.warnings == []
    assert set(report.findings) == {s.value for s in Status}
    assert report.transitions_done == len(_with_resolution(_with_proposal(WALK))) + 1


async def test_a_jira_project_built_for_the_other_choice_is_reported(make_config: ConfigFactory) -> None:
    # Configured for a release proposal, but Accept delivery in Jira still goes to Ready for release.
    cfg = make_config(overrides={"release": {"proposal": True}})
    report = await verify_workflow(cfg, fake(proposal=False), emit=lambda _: None)
    assert not report.ok
    assert any("Accept delivery -> ready_release_preparation" in p for p in report.problems)


async def test_jira_failure_mid_walk_is_reported_not_raised(make_config: ConfigFactory) -> None:
    from delivery.ports import AuthError

    jira = fake()
    jira.fail_next["transitions"] = AuthError("Jira 401", status=401)
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert report.problems == ["stopped: Jira call failed (Jira 401)"]
    assert len(report.tickets) == 1


async def test_resolution_is_not_walked_or_required_when_the_statuses_are_not_mapped(
    make_config: ConfigFactory,
) -> None:
    cfg = make_config()
    cfg = cfg.model_copy(
        update={
            "workflow": cfg.workflow.model_copy(
                update={
                    "statuses": {s: i for s, i in cfg.workflow.statuses.items() if s not in OPTIONAL_STATUSES}
                }
            )
        }
    )
    jira = FakeJira({s: i for s, i in STATUS_IDS.items() if s not in OPTIONAL_STATUSES}, me=DEV)
    report = await verify_workflow(cfg, jira, emit=lambda _: None)
    assert report.ok, report.problems
    assert report.transitions_done == len(WALK) + 1
    assert not any("not reached" in w for w in report.warnings)


async def test_missing_resolved_action_is_reported_at_resolving(make_config: ConfigFactory) -> None:
    jira = fake()
    jira.drop_routes.add((Status.RESOLVING, Status.READY_PLANNING))
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert not report.ok
    assert any("Resolving: missing transition Resolved: resume planning" in p for p in report.problems)
