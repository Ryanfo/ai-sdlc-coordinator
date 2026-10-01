from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import DEV, OTHER_DEV, STATUS_IDS, ConfigFactory
from delivery.ownership import (
    FileLock,
    IssueView,
    LockHeld,
    RepoLocks,
    TicketClaims,
    coordination_jql,
    evaluate_eligibility,
    ready_jql,
    supervisor_lock,
)
from delivery.workflow import Stage, Status


def _issue(**kw: object) -> IssueView:
    base: dict[str, object] = {
        "key": "PILOT-1",
        "project_key": "PILOT",
        "issue_type": "Story",
        "status_id": STATUS_IDS[Status.READY_REFINEMENT],
        "status_name": "Ready for refinement",
        "assignee_account_id": DEV,
    }
    base.update(kw)
    return IssueView(**base)  # type: ignore[arg-type]


def test_correct_assignee_in_ready_status_is_eligible(make_config: ConfigFactory) -> None:
    e = evaluate_eligibility(_issue(), make_config())
    assert e.eligible and e.stage is Stage.REFINEMENT


def test_other_assignee_is_ignored_even_if_moved_by_us(make_config: ConfigFactory) -> None:
    e = evaluate_eligibility(_issue(assignee_account_id=OTHER_DEV), make_config())
    assert not e.eligible and "another account" in e.reasons[0]


@pytest.mark.parametrize(
    "status",
    [
        Status.SPECIFICATION_REVIEW,
        Status.BLOCKED,
        Status.NEEDS_CLARIFICATION,
        Status.REFINING,
        Status.READY_RELEASE,
        Status.DONE,
    ],
)
def test_no_pickup_from_review_paused_active_or_terminal(make_config: ConfigFactory, status: Status) -> None:
    e = evaluate_eligibility(_issue(status_id=STATUS_IDS[status]), make_config())
    assert not e.eligible


def test_unmapped_status_and_label_and_type(make_config: ConfigFactory) -> None:
    cfg = make_config(overrides={"jira": {"required_label": "agent-enabled"}})
    e = evaluate_eligibility(_issue(status_id="99999", issue_type="Epic"), cfg)
    joined = " ".join(e.reasons)
    assert "not mapped" in joined and "Epic" in joined and "agent-enabled" in joined
    assert evaluate_eligibility(_issue(labels=("agent-enabled",)), cfg).eligible


def test_active_execution_blocks_same_ticket_only(make_config: ConfigFactory) -> None:
    cfg = make_config()
    assert not evaluate_eligibility(_issue(), cfg, {"PILOT-1"}).eligible
    assert evaluate_eligibility(_issue(key="PILOT-2"), cfg, {"PILOT-1"}).eligible


def test_jql_filters(make_config: ConfigFactory) -> None:
    cfg = make_config(overrides={"jira": {"required_label": "agent-enabled"}})
    q = ready_jql(cfg)
    assert f'assignee = "{DEV}"' in q and 'labels = "agent-enabled"' in q
    assert STATUS_IDS[Status.READY_PLANNING] in q and STATUS_IDS[Status.PLAN_REVIEW] not in q
    c = coordination_jql(cfg)
    assert "assignee" not in c  # overlap check must see other developers' work
    assert STATUS_IDS[Status.DONE] not in c and STATUS_IDS[Status.BACKLOG] not in c


def test_second_supervisor_for_same_identity_is_refused(make_config: ConfigFactory) -> None:
    cfg = make_config()
    first = supervisor_lock(cfg)
    first.acquire({"worker_id": "test-laptop"})
    try:
        with pytest.raises(LockHeld) as exc:
            supervisor_lock(cfg).acquire()
        assert exc.value.holder["worker_id"] == "test-laptop"
    finally:
        first.release()
    supervisor_lock(cfg).acquire()


def test_supervisor_lock_is_os_backed_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    lock = FileLock(path)
    lock.acquire({"who": "parent"})
    code = (
        "import sys; from pathlib import Path; from delivery.ownership import FileLock, LockHeld\n"
        f"try:\n    FileLock(Path({str(path)!r})).acquire()\nexcept LockHeld:\n    sys.exit(7)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], check=False)
    assert proc.returncode == 7
    lock.release()
    proc = subprocess.run([sys.executable, "-c", code], check=False)
    assert proc.returncode == 0


def test_ticket_claims_are_per_ticket_not_global(tmp_path: Path) -> None:
    claims = TicketClaims(tmp_path)
    assert claims.claim("PILOT-1", "r1")
    assert not claims.claim("PILOT-1", "r2")  # duplicate writer refused
    for i in range(2, 60):
        assert claims.claim(f"PILOT-{i}", f"r{i}")  # no numeric ceiling
    claims.release("PILOT-1", "wrong-run")
    assert claims.holder("PILOT-1") == "r1"
    claims.release("PILOT-1", "r1")
    assert claims.claim("PILOT-1", "r3")


async def test_repo_lock_serialises_metadata_ops_only(tmp_path: Path) -> None:
    locks = RepoLocks(tmp_path)
    order: list[str] = []

    async def op(name: str) -> None:
        async with locks.hold("repo", name):
            order.append(f"{name}-in")
            await asyncio.sleep(0.01)
            order.append(f"{name}-out")

    await asyncio.gather(op("a"), op("b"), op("c"))
    # Critical sections never interleave.
    for i in range(0, len(order), 2):
        assert order[i].endswith("-in") and order[i + 1].endswith("-out")
        assert order[i][0] == order[i + 1][0]
