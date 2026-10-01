from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from delivery.journal import JournalCorrupt, JournalStore, OpStatus, atomic_write
from delivery.models import (
    PROPERTY_MAX_BYTES,
    GateKind,
    GateRecord,
    GateState,
    RunRecord,
    RunState,
    SharedExecutionRecord,
    utcnow,
)
from delivery.redaction import redact
from delivery.workflow import Stage


def _record(run_id: str = "PILOT-1-refinement-x", state: RunState = RunState.STARTING) -> RunRecord:
    return RunRecord(
        ticket_key="PILOT-1",
        run_id=run_id,
        attempt=1,
        stage=Stage.REFINEMENT,
        developer_account_id="dev-account-0001",
        worker_id="w",
        session_label=run_id,
        state=state,
        attempt_key=f"PILOT-1:refinement:{'0' * 64}",
    )


def test_create_save_load_roundtrip_with_private_permissions(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "state", "abc")
    store.init()
    j = store.run_journal("PILOT-1", "r1")
    j.create(_record("r1"))
    j.save(_record("r1", RunState.RUNNING))
    assert j.load().state is RunState.RUNNING
    assert stat.S_IMODE(os.stat(j.dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(j.snapshot_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(j.events.path).st_mode) == 0o600


def test_partial_last_event_is_detected_and_blocks_only_that_ticket(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, "abc")
    good = store.run_journal("PILOT-1", "r1")
    good.create(_record("r1"))
    bad = store.run_journal("PILOT-2", "r2")
    bad.create(_record("r2").model_copy(update={"ticket_key": "PILOT-2"}))
    with bad.events.path.open("ab") as fh:
        fh.write(b'{"type": "op_intent", "data": {"op_id": "x"')  # torn write
    with pytest.raises(JournalCorrupt, match="partial"):
        bad.load()
    entries = {e.ticket_key: e for e in store.iter_runs()}
    assert entries["PILOT-1"].record is not None and entries["PILOT-1"].error is None
    assert entries["PILOT-2"].record is None and entries["PILOT-2"].error is not None


def test_atomic_snapshot_leaves_no_temp_files_and_survives_failed_write(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write(target, b"one")
    atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_outbox_intent_without_result_is_uncertain_until_confirmed(tmp_path: Path) -> None:
    j = JournalStore(tmp_path, "k").run_journal("PILOT-1", "r1")
    j.create(_record("r1"))
    j.intend("op-comment", "jira_comment", {"marker": "m"})
    assert j.op_state("op-comment").status is OpStatus.INTENDED
    assert [o.op_id for o in j.pending_ops()] == ["op-comment"]
    j.fail("op-comment", "timeout", definite=False)
    assert j.op_state("op-comment").status is OpStatus.INTENDED
    j.confirm("op-comment", {"comment_id": "1"})
    assert j.op_state("op-comment").status is OpStatus.CONFIRMED
    assert j.pending_ops() == []


def test_restart_does_not_lose_publication_intent(tmp_path: Path) -> None:
    store = JournalStore(tmp_path, "k")
    j = store.run_journal("PILOT-1", "r1")
    j.create(_record("r1", RunState.PUBLISHING))
    j.intend("op-transition", "jira_transition")
    # Simulate a new process: fresh objects, same directory.
    reopened = JournalStore(tmp_path, "k")
    unfinished = reopened.unfinished()
    assert [e.run_id for e in unfinished] == ["r1"]
    assert unfinished[0].journal.pending_ops()[0].op_id == "op-transition"


def test_journal_events_are_redacted(tmp_path: Path) -> None:
    j = JournalStore(tmp_path, "k").run_journal("PILOT-1", "r1")
    j.create(_record("r1"))
    j.events.append("diag", {"stderr": "Authorization: Basic dXNlcjpBVEFUVDN4RmZHRjBzZWNyZXQ="})
    assert "dXNlcjpB" not in j.events.path.read_text()


def test_redaction_patterns() -> None:
    text = "token=ghp_abcdefghijklmnopqrstuvwx ATATT3xFfGF0abcdefghijklmnop sk-ant-api03-abcdefghijk"
    out = redact(text, extra_secrets=["my-literal-secret-value"])
    assert "ghp_" not in out and "ATATT3" not in out and "sk-ant" not in out
    assert redact("x my-literal-secret-value y", ["my-literal-secret-value"]) == "x [REDACTED] y"


def test_shared_record_property_cap() -> None:
    rec = SharedExecutionRecord(ticket_key="PILOT-1", worker_id="w", developer_account_id="d")
    rec.history.extend({"event": "x" * 500, "n": i} for i in range(200))
    rec.gates.extend(
        GateRecord(
            token=f"PILOT-1-SPEC-v{i}",
            kind=GateKind.SPEC,
            ticket_key="PILOT-1",
            revision=i,
            published_at=utcnow(),
            approvers=["a" * 20],
            state=GateState.SUPERSEDED,
        )
        for i in range(10)
    )
    assert len(rec.encoded()) > PROPERTY_MAX_BYTES
    compact = rec.compacted()
    assert len(compact.encoded()) <= PROPERTY_MAX_BYTES
    rec2 = SharedExecutionRecord(
        ticket_key="PILOT-1", worker_id="w", developer_account_id="d", artefacts={"x": "y" * 30000}
    )
    with pytest.raises(ValueError, match="24 KiB"):
        rec2.compacted()
