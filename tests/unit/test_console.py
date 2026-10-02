from __future__ import annotations

from conftest import ConfigFactory
from delivery import console
from delivery.models import RunRecord, RunState, utcnow
from delivery.workflow import Stage


def _record(**kw: object) -> RunRecord:
    base: dict[str, object] = {
        "ticket_key": "PILOT-7",
        "run_id": "PILOT-7-planning-1",
        "attempt": 1,
        "stage": Stage.PLANNING,
        "developer_account_id": "x",
        "worker_id": "w",
        "session_label": "s",
        "state": RunState.RUNNING,
        "attempt_key": "k",
        "started_at": utcnow(),
    }
    return RunRecord.model_validate({**base, **kw})


def test_session_blocks_name_the_action_status_ticket_and_link(make_config: ConfigFactory) -> None:
    cfg = make_config(overrides={"claude.models": {"plan-ticket": "opus"}})
    started = console.session_started(cfg, _record(), "Add a welcome block")
    lines = started.splitlines()
    assert lines[1] == "=" * 78 and lines[-1] == "=" * 78
    assert "STARTED  Planning  PILOT-7" in started
    assert "Ready for planning -> Planning" in started
    assert "PILOT-7  Add a welcome block" in started
    assert "plan-ticket on opus" in started
    assert "https://example.atlassian.net/browse/PILOT-7" in started
    adopted = console.session_started(cfg, _record(adopted=True), "S", adopted=True)
    assert "TAKEN OVER" in adopted and "moved there by hand" in adopted


def test_finished_block_shows_outcome_next_step_and_wraps_long_text(make_config: ConfigFactory) -> None:
    rec = _record(state=RunState.BLOCKED, reason="word " * 40, next_action="Choose Resume planning.")
    out = console.session_finished(make_config(), rec, "S")
    assert "FINISHED  Planning  PILOT-7  -  BLOCKED" in out
    assert "Next      Choose Resume planning." in out
    assert all(len(line) <= 78 for line in out.splitlines() if "file://" not in line)
