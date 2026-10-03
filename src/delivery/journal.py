"""Local recovery journal: per-run append-only events, atomic snapshots and an outbox.

Layout under ``runtime.state_dir`` (directories 0700, files 0600)::

    runs/<TICKET>/<run-id>/snapshot.json     atomic RunRecord snapshot
    runs/<TICKET>/<run-id>/events.jsonl      append-only events, fsync'd
    runs/<TICKET>/<run-id>/{envelope.json,result.json,stdout.log,stderr.log,tmp/,...}
    supervisor/<identity>/supervisor.json    supervisor record (dispatch pause, socket)
    supervisor/<identity>/events.jsonl
    locks/                                   OS locks (identity, tickets, repositories)

Every external operation is journaled as an intent before it is sent and as a result
afterwards. An intent without a result is "uncertain" and must be reconciled against
the remote system before any retry. A corrupt record blocks only its own ticket.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from delivery.models import (
    ACTIVE_RUN_STATES,
    Model,
    RunRecord,
    RunState,
    utcnow,
)
from delivery.redaction import redact


class JournalCorrupt(Exception):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {detail}")


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temporary file and atomic replace, flushing file and directory."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any) -> None:
    if isinstance(obj, BaseModel):
        text = obj.model_dump_json(indent=2)
    else:
        text = json.dumps(obj, indent=2, sort_keys=True, default=str)
    atomic_write(path, text.encode())


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        ensure_private_dir(self.path.parent)
        record = {
            "at": datetime.now(UTC).isoformat(),
            "type": event_type,
            "data": json.loads(redact(json.dumps(data or {}, default=str))),
        }
        line = (json.dumps(record, sort_keys=True) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise JournalCorrupt(self.path, "last event is incomplete (partial write)")
        events = []
        for n, line in enumerate(raw.splitlines(), start=1):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                raise JournalCorrupt(self.path, f"event line {n} is not valid JSON") from None
            if not isinstance(ev, dict) or "type" not in ev:
                raise JournalCorrupt(self.path, f"event line {n} is malformed")
            events.append(ev)
        return events


class OpStatus(StrEnum):
    NONE = "none"
    INTENDED = "intended"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(frozen=True)
class OpState:
    op_id: str
    status: OpStatus
    op_type: str = ""
    detail: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class RunJournal:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.events = EventLog(directory / "events.jsonl")
        self.snapshot_path = directory / "snapshot.json"

    # paths -------------------------------------------------------------------
    @property
    def envelope_path(self) -> Path:
        return self.dir / "envelope.json"

    @property
    def result_path(self) -> Path:
        return self.dir / "result.json"

    @property
    def stdout_path(self) -> Path:
        return self.dir / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.dir / "stderr.log"

    @property
    def tmp_dir(self) -> Path:
        return ensure_private_dir(self.dir / "tmp")

    @property
    def logs_dir(self) -> Path:
        return ensure_private_dir(self.dir / "logs")

    @property
    def output_dir(self) -> Path:
        return ensure_private_dir(self.dir / "output")

    # records -----------------------------------------------------------------
    def create(self, record: RunRecord) -> None:
        if self.snapshot_path.exists():
            raise FileExistsError(self.snapshot_path)
        ensure_private_dir(self.dir.parent)
        ensure_private_dir(self.dir)
        self.events.append("created", {"run_id": record.run_id, "stage": record.stage})
        atomic_write_json(self.snapshot_path, record)

    def save(self, record: RunRecord, event: str | None = None, **data: Any) -> RunRecord:
        record = record.model_copy(update={"updated_at": utcnow()})
        self.events.append(event or "state", {"state": record.state.value, **data})
        atomic_write_json(self.snapshot_path, record)
        return record

    def load(self) -> RunRecord:
        self.events.read()  # integrity check of the append-only log
        if not self.snapshot_path.exists():
            raise JournalCorrupt(self.snapshot_path, "snapshot missing")
        try:
            return RunRecord.model_validate_json(self.snapshot_path.read_bytes())
        except ValidationError as exc:
            detail = f"snapshot invalid: {exc.error_count()} errors"
            raise JournalCorrupt(self.snapshot_path, detail) from None

    # outbox ------------------------------------------------------------------
    def intend(self, op_id: str, op_type: str, detail: dict[str, Any] | None = None) -> None:
        self.events.append("op_intent", {"op_id": op_id, "op_type": op_type, "detail": detail or {}})

    def confirm(self, op_id: str, result: dict[str, Any] | None = None) -> None:
        self.events.append("op_result", {"op_id": op_id, "result": result or {}})

    def fail(self, op_id: str, error: str, definite: bool) -> None:
        self.events.append("op_failed", {"op_id": op_id, "error": error, "definite": definite})

    def ops(self) -> dict[str, OpState]:
        states: dict[str, OpState] = {}
        for ev in self.events.read():
            data = ev.get("data", {})
            op_id = data.get("op_id")
            if not op_id:
                continue
            prev = states.get(op_id)
            if ev["type"] == "op_intent":
                states[op_id] = OpState(op_id, OpStatus.INTENDED, data.get("op_type", ""), data.get("detail"))
            elif ev["type"] == "op_result":
                states[op_id] = OpState(
                    op_id,
                    OpStatus.CONFIRMED,
                    prev.op_type if prev else "",
                    prev.detail if prev else None,
                    data.get("result"),
                )
            elif ev["type"] == "op_failed" and data.get("definite"):
                # Only a definite failure clears uncertainty; an ambiguous one stays intended.
                states[op_id] = OpState(
                    op_id,
                    OpStatus.FAILED,
                    prev.op_type if prev else "",
                    prev.detail if prev else None,
                    {"error": data.get("error")},
                )
        return states

    def op_state(self, op_id: str) -> OpState:
        return self.ops().get(op_id, OpState(op_id, OpStatus.NONE))

    def pending_ops(self) -> list[OpState]:
        return [s for s in self.ops().values() if s.status is OpStatus.INTENDED]


class SupervisorRecord(Model):
    schema_version: int = 1
    identity_key: str
    worker_id: str
    developer_account_id: str
    pid: int | None = None
    host: str = ""
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    stopped_at: datetime | None = None
    dispatch_paused: bool = False
    pause_reason: str = ""
    control_socket: str | None = None
    version: str = ""
    last_poll_at: datetime | None = None
    last_poll_error: str = ""
    integration_backoff: dict[str, str] = Field(default_factory=dict)
    # Newest modification time of the coordinator's code when this supervisor started.
    code_mtime: float | None = None
    # Claude could not be used (login expired or usage limit): new work waits, runs that hit
    # it resume once Claude works again. {"kind", "detail", "since", "next_check"}.
    claude_unavailable: dict[str, str] | None = None


@dataclass
class RunEntry:
    ticket_key: str
    run_id: str
    journal: RunJournal
    record: RunRecord | None
    error: JournalCorrupt | None


class JournalStore:
    def __init__(self, state_dir: Path, identity_key: str) -> None:
        self.root = state_dir
        self.identity_key = identity_key
        self.runs_dir = state_dir / "runs"
        self.locks_dir = state_dir / "locks"
        self.supervisor_dir = state_dir / "supervisor" / identity_key

    def init(self) -> None:
        # Parents first: mkdir(parents=True) would create them with the default mode.
        for d in (
            self.root,
            self.runs_dir,
            self.locks_dir,
            self.root / "intake",
            self.supervisor_dir.parent,
            self.supervisor_dir,
        ):
            ensure_private_dir(d)

    # supervisor ----------------------------------------------------------------
    @property
    def supervisor_path(self) -> Path:
        return self.supervisor_dir / "supervisor.json"

    @property
    def supervisor_events(self) -> EventLog:
        return EventLog(self.supervisor_dir / "events.jsonl")

    def load_supervisor(self) -> SupervisorRecord | None:
        if not self.supervisor_path.exists():
            return None
        try:
            return SupervisorRecord.model_validate_json(self.supervisor_path.read_bytes())
        except ValidationError as exc:
            raise JournalCorrupt(self.supervisor_path, "supervisor record invalid") from exc

    def save_supervisor(self, rec: SupervisorRecord, event: str | None = None, **data: Any) -> None:
        if event:
            self.supervisor_events.append(event, data)
        atomic_write_json(self.supervisor_path, rec)

    # runs ----------------------------------------------------------------------
    def run_journal(self, ticket_key: str, run_id: str) -> RunJournal:
        return RunJournal(self.runs_dir / ticket_key / run_id)

    def iter_runs(self, ticket_key: str | None = None) -> Iterator[RunEntry]:
        if not self.runs_dir.exists():
            return
        tickets = [self.runs_dir / ticket_key] if ticket_key else sorted(self.runs_dir.iterdir())
        for tdir in tickets:
            if not tdir.is_dir():
                continue
            for rdir in sorted(tdir.iterdir()):
                if not rdir.is_dir():
                    continue
                j = RunJournal(rdir)
                try:
                    yield RunEntry(tdir.name, rdir.name, j, j.load(), None)
                except JournalCorrupt as exc:
                    yield RunEntry(tdir.name, rdir.name, j, None, exc)

    def runs_for_ticket(self, ticket_key: str) -> list[RunEntry]:
        entries = list(self.iter_runs(ticket_key))
        return sorted(
            entries,
            key=lambda e: (e.record.created_at.isoformat() if e.record else "", e.run_id),
        )

    def latest_run(self, ticket_key: str) -> RunEntry | None:
        entries = self.runs_for_ticket(ticket_key)
        return entries[-1] if entries else None

    def find_attempt(self, attempt_key: str) -> RunEntry | None:
        for e in self.iter_runs(attempt_key.split(":", 1)[0]):
            if e.record and e.record.attempt_key == attempt_key:
                return e
        return None

    def unfinished(self) -> list[RunEntry]:
        """Runs needing reconciliation: active, interrupted, uncertain or corrupt."""
        out = []
        for e in self.iter_runs():
            if e.error is not None or (
                e.record
                and (
                    e.record.state in ACTIVE_RUN_STATES
                    or e.record.state is RunState.INTERRUPTED
                    or e.journal.pending_ops()
                )
            ):
                out.append(e)
        return out


def new_run_id(ticket_key: str, stage: str, now: datetime | None = None) -> str:
    ts = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{ticket_key}-{stage}-{ts}-{secrets.token_hex(3)}"
