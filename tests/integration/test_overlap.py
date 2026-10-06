"""Cross-developer overlap detection and integration evidence (amendment §4-6)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import DEV, OTHER_DEV, STATUS_IDS
from delivery.ports import IssueLink
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from fakes.github import FakeGitHub
from fakes.jira import FakeJira
from gitutil import make_origin
from harness import World, make_world, step


def two_developers(tmp_path: Path, checks: dict[str, list[str]] | None = None) -> tuple[World, World]:
    origin = make_origin(tmp_path)
    jira = FakeJira(STATUS_IDS, me=DEV)
    github = FakeGitHub(origin, auto_ci={"unit": "success"})
    a = make_world(
        tmp_path, account=DEV, jira=jira, github=github, origin=origin, name="dev-a", checks=checks
    )
    b = make_world(
        tmp_path,
        account=OTHER_DEV,
        jira=jira.as_user(OTHER_DEV),
        github=github,
        origin=origin,
        name="dev-b",
        checks=checks,
    )
    return a, b


async def _plan(w: World, sup: Supervisor, key: str) -> None:
    await step(sup)
    w.decide(key, f"APPROVE SPEC {w.token(key, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)


async def test_overlapping_plans_from_two_developers_get_linked_deduplicated_warnings(tmp_path: Path) -> None:
    a, b = two_developers(tmp_path)
    shared = {"paths": ["src/search/**"], "components": ["search"]}
    a.scenario({"plan-ticket": [{"footprint": shared}]})
    b.scenario({"plan-ticket": [{"footprint": {"paths": ["src/search/index.ts"], "components": ["Search"]}}]})
    a.new_ticket("PILOT-1", assignee=DEV)
    a.submit("PILOT-1")
    b.new_ticket("PILOT-2", assignee=OTHER_DEV)
    b.submit("PILOT-2", author=OTHER_DEV)
    async with Supervisor(a.deps) as sa, Supervisor(b.deps) as sb:
        await _plan(a, sa, "PILOT-1")  # A publishes first: nothing to compare yet
        await _plan(b, sb, "PILOT-2")  # B sees A's footprint
        assert b.jira.status_of("PILOT-2") is Status.PLAN_REVIEW
        warn_b = [c for c in b.comments("PILOT-2") if "Overlap warning" in c]
        warn_a = [c for c in a.comments("PILOT-1") if "Overlap warning" in c]
        assert len(warn_b) == 2 and len(warn_a) == 2  # same path + same component, mirrored
        assert "PILOT-1" in warn_b[0] and "PILOT-2" in warn_a[0]
        assert "src/search" in "".join(warn_b)
        # Same-file overlap is a warning, not a block: both proceed to development.
        a.decide("PILOT-1", f"APPROVE PLAN {a.token('PILOT-1', 'PLAN')}", Status.READY_DEVELOPMENT)
        b.decide("PILOT-2", f"APPROVE PLAN {b.token('PILOT-2', 'PLAN')}", Status.READY_DEVELOPMENT)
        await step(sa)
        await step(sb)
        assert a.jira.status_of("PILOT-1") is Status.READY_VERIFICATION
        assert b.jira.status_of("PILOT-2") is Status.READY_VERIFICATION
    # Repeated checkpoints never flood: one comment per warning ID per ticket.
    for w, key in ((a, "PILOT-1"), (b, "PILOT-2")):
        ids = [
            line.split(": ", 1)[1]
            for c in w.comments(key)
            for line in c.splitlines()
            if line.startswith("Overlap warning: ")
        ]
        assert ids, "expected overlap warnings"
        assert len(ids) == len(set(ids))


async def test_shared_interface_and_declared_dependency_are_flagged_never_paused(tmp_path: Path) -> None:
    a, b = two_developers(tmp_path)
    fp_a = {"paths": ["src/a.ts"], "interfaces": ["SearchResult"]}
    fp_b = {"paths": ["src/b.ts"], "interfaces": ["searchresult"]}
    a.scenario({"plan-ticket": [{"footprint": fp_a}]})
    b.scenario({"plan-ticket": [{"footprint": fp_b}]})
    a.new_ticket("PILOT-1", assignee=DEV)
    a.submit("PILOT-1")
    link = IssueLink("Blocks", "inward", "is blocked by", "PILOT-1")
    b.new_ticket("PILOT-2", assignee=OTHER_DEV, links=[link])
    b.submit("PILOT-2", author=OTHER_DEV)
    async with Supervisor(a.deps) as sa, Supervisor(b.deps) as sb:
        await _plan(a, sa, "PILOT-1")
        await _plan(b, sb, "PILOT-2")
        warnings = [c for c in b.comments("PILOT-2") if "Overlap warning" in c]
        assert any("shared_contract" in c and "Higher risk" in c for c in warnings)
        assert any("declared_dependency" in c and "PILOT-2 depends on PILOT-1" in c for c in warnings)
        assert not any("OVERLAP OVL-" in c for c in b.comments("PILOT-2"))
        # No decision is needed: development starts as soon as the plan is approved.
        b.decide("PILOT-2", f"APPROVE PLAN {b.token('PILOT-2', 'PLAN')}", Status.READY_DEVELOPMENT)
        await step(sb)
        assert b.jira.status_of("PILOT-2") is Status.READY_VERIFICATION, b.last_comment("PILOT-2")
        assert b.record("PILOT-2").pause is None


async def test_behavioural_overlap_caught_by_integration_tree_without_text_conflict(tmp_path: Path) -> None:
    # The integration check fails only when both changes are present together, although Git
    # merges them cleanly (different files): a behavioural incompatibility.
    check = ["sh", "-c", "! { test -f src/flag_a.ts && test -f src/flag_b.ts; }"]
    a, b = two_developers(tmp_path, checks={"unit": check})
    a.scenario(
        {
            "plan-ticket": [{"footprint": {"paths": ["src/**"]}}],
            "implement-ticket": [{"edit": {"src/flag_a.ts": "export const a = 1;\n"}}],
        }
    )
    b.scenario(
        {
            "plan-ticket": [{"footprint": {"paths": ["src/**"]}}],
            "implement-ticket": [{"edit": {"src/flag_b.ts": "export const b = 2;\n"}}],
        }
    )
    a.new_ticket("PILOT-1", assignee=DEV)
    a.submit("PILOT-1")
    b.new_ticket("PILOT-2", assignee=OTHER_DEV)
    b.submit("PILOT-2", author=OTHER_DEV)
    async with Supervisor(a.deps) as sa, Supervisor(b.deps) as sb:
        for w, s, k in ((a, sa, "PILOT-1"), (b, sb, "PILOT-2")):
            await _plan(w, s, k)
            w.decide(k, f"APPROVE PLAN {w.token(k, 'PLAN')}", Status.READY_DEVELOPMENT)
            await step(s)
        await step(sa)  # A verifies alone: passes (B's candidate is published but merges cleanly)
        await step(sb)  # B's integration tree includes A's candidate: the combined check fails
    status_a, status_b = a.jira.status_of("PILOT-1"), b.jira.status_of("PILOT-2")
    assert Status.CHANGES_REQUESTED in (status_a, status_b)
    failed = "PILOT-1" if status_a is Status.CHANGES_REQUESTED else "PILOT-2"
    w = a if failed == "PILOT-1" else b
    assert "coordinator check unit (integration) failed" in w.last_comment(failed)
    checks = next(
        Path(w.cfg.runtime.state_dir).rglob(f"runs/{failed}/*verification*/inputs/coordinator_checks.json")
    )
    results = json.loads(checks.read_text())
    cand = [r for r in results if r["target"] == "candidate"][0]
    integ = [r for r in results if r["target"] == "integration"][0]
    assert cand["conclusion"] == "passed" and integ["conclusion"] == "failed"
    assert integ["tree_sha"] and integ["base_sha"] and cand["sha"] != integ["sha"]
