from __future__ import annotations

import pytest

from conftest import DEV, STATUS_IDS, ConfigFactory
from delivery.ownership import evaluate_eligibility
from delivery.workflow import STATUS_NAMES, Action, Status, route_for_action
from delivery.workflow_check import CHECK_LABEL, WALK, verify_workflow
from fakes.jira import FakeJira


def fake() -> FakeJira:
    return FakeJira(STATUS_IDS, me=DEV)


def test_walk_follows_permitted_routes_and_visits_every_status() -> None:
    status, seen = Status.BACKLOG, {Status.BACKLOG}
    for action, _ in WALK:
        route = route_for_action(status, action)
        assert route is not None, f"{action} from {status}"
        status = route.target
        seen.add(status)
    assert status is Status.DONE
    assert set(Status) - seen == {Status.CANCELLED}  # reached by the second ticket


async def test_matching_workflow_passes_and_leaves_unassigned_labelled_tickets(
    make_config: ConfigFactory,
) -> None:
    cfg, jira = make_config(), fake()
    report = await verify_workflow(cfg, jira, emit=lambda _: None)
    assert report.ok, report.problems
    assert report.warnings == []
    assert set(report.findings) == {s.value for s in Status}
    assert report.transitions_done == len(WALK) + 1
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
    jira = fake()
    jira.drop_routes.add((Status.BLOCKED, Status.READY_RELEASE_PREPARATION))
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
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
    assert sum("resume field not configured" in w for w in report.warnings) == 2


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
        "issue type Task does not use the delivery workflow (1 of 24 statuses): give it the "
        "workflow or remove it from jira.supported_issue_types"
    ]
    assert jira.issues[report.tickets[0]].issue_type == "Story"
    assert jira.issues[report.tickets[0]].status is Status.DONE


async def test_optional_release_verification_failed_route_is_accepted(make_config: ConfigFactory) -> None:
    from delivery.ports import JiraTransition

    jira = fake()
    base = jira._available

    def available(issue):  # type: ignore[no-untyped-def]
        out = base(issue)
        if issue.status is Status.VERIFYING_RELEASE:
            out.append(JiraTransition("x1", "Release verification failed", STATUS_IDS[Status.BLOCKED]))
        return out

    jira._available = available  # type: ignore[method-assign]
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert report.ok and report.warnings == []
    assert "Verifying release: optional route present: Release verification failed -> blocked" in report.info


async def test_jira_failure_mid_walk_is_reported_not_raised(make_config: ConfigFactory) -> None:
    from delivery.ports import AuthError

    jira = fake()
    jira.fail_next["transitions"] = AuthError("Jira 401", status=401)
    report = await verify_workflow(make_config(), jira, emit=lambda _: None)
    assert report.problems == ["stopped: Jira call failed (Jira 401)"]
    assert len(report.tickets) == 1
