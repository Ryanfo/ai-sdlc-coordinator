from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from delivery.journal import JournalStore, RunJournal
from delivery.models import RunRecord, RunState
from delivery.office import OfficeFeed, OfficeServer
from delivery.workflow import Stage

IDENTITY = "abc"


def _run(store: JournalStore, key: str, stage: Stage, suffix: str = "x") -> tuple[RunJournal, RunRecord]:
    rid = f"{key}-{stage.value}-{suffix}"
    j = store.run_journal(key, rid)
    rec = RunRecord(
        ticket_key=key,
        run_id=rid,
        attempt=1,
        stage=stage,
        developer_account_id="dev-account-0001",
        worker_id="w",
        session_label=rid,
        state=RunState.STARTING,
        attempt_key=f"{key}:{stage.value}:{'0' * 64}",
    )
    j.create(rec)
    (j.dir / "inputs").mkdir()
    brief = {"summary": "Add a starred filter", "issue_type": "Story", "description": "secret plans"}
    (j.dir / "inputs" / "envelope-implement-ticket.json").write_text(json.dumps({"brief": brief}))
    return j, j.save(rec, "starting")


def _store(tmp_path: Path) -> JournalStore:
    store = JournalStore(tmp_path / "state", IDENTITY)
    store.init()
    return store


def test_a_development_run_becomes_office_beats(tmp_path: Path) -> None:
    store = _store(tmp_path)
    j, rec = _run(store, "PILOT-1", Stage.DEVELOPMENT)
    j.intend("op1", "jira_transition", {"key": "PILOT-1", "to": "developing", "transition_id": "9"})
    j.confirm("op1", {"status": "developing"})
    rec = j.save(rec.model_copy(update={"state": RunState.RUNNING}), "running")
    j.save(rec, "heartbeat")
    rec = j.save(rec.model_copy(update={"state": RunState.PUBLISHING}), "publishing")
    j.intend("op2", "git_push", {"branch": "feature/PILOT-1"})
    j.intend("op3", "github_pr", {"head": "feature/PILOT-1"})
    j.confirm("op3", {"number": 5, "url": "https://github.com/example/app/pull/5"})
    j.intend("op4", "jira_comment", {"body": "a private comment"})
    j.save(rec.model_copy(update={"state": RunState.AWAITING_HUMAN}), "published")

    feed = OfficeFeed(store.root, IDENTITY)
    beats = feed.poll()

    assert [b["kind"] for b in beats] == [
        "arrive",
        "jira",
        "start",
        "publishing",
        "push",
        "pr",
        "comment",
        "await",
    ]
    assert [b["seq"] for b in beats] == list(range(1, 9))
    assert all(b["ticket"] == "PILOT-1" and b["stage"] == "development" for b in beats)
    assert beats[1]["to"] == "developing"
    assert beats[5]["number"] == 5
    # Only what the page needs leaves the journal: no comment bodies, branches or URLs.
    text = json.dumps(beats)
    assert "private comment" not in text and "feature/" not in text and "github.com" not in text

    [ticket] = feed.tickets()
    assert ticket["key"] == "PILOT-1"
    assert ticket["title"] == "Add a starred filter"
    assert ticket["state"] == "awaiting_human"
    assert "secret plans" not in json.dumps(ticket)


def test_poll_returns_only_new_beats_and_waits_for_whole_lines(tmp_path: Path) -> None:
    store = _store(tmp_path)
    j, rec = _run(store, "PILOT-2", Stage.VERIFICATION)
    feed = OfficeFeed(store.root, IDENTITY)
    assert [b["kind"] for b in feed.poll()] == ["arrive"]
    assert feed.poll() == []

    rec = j.save(rec.model_copy(update={"state": RunState.RUNNING}), "running")
    with j.events.path.open("a") as fh:
        fh.write('{"at": "2026-10-05T10:00:00+00:00", "type": "coordinator_checks", "data"')
    assert [b["kind"] for b in feed.poll()] == ["start"]
    with j.events.path.open("a") as fh:
        fh.write(': {"state": "running"}}\n')
    new = feed.poll()
    assert [(b["kind"], b["seq"]) for b in new] == [("checks", 3)]

    j.save(rec.model_copy(update={"state": RunState.FAILED}), "failed")
    assert [b["kind"] for b in feed.poll()] == ["failed"]
    j.save(rec.model_copy(update={"state": RunState.CANCELLED}), "cancelled")
    assert [b["kind"] for b in feed.poll()] == ["cancelled"]


def test_supervisor_events_move_the_manager(tmp_path: Path) -> None:
    store = _store(tmp_path)
    log = store.supervisor_events
    log.append("supervisor_started", {"pid": 1})
    log.append("claude_unavailable", {"kind": "usage"})
    log.append("claude_available", {"resuming": 0})
    log.append("supervisor_stopped", {"interrupted": []})
    feed = OfficeFeed(store.root, IDENTITY)
    assert [b["kind"] for b in feed.poll()] == ["boss_in", "break", "back", "boss_out"]
    assert feed.supervisor()["running"] is False


@pytest.fixture
def server(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _run(store, "PILOT-3", Stage.REFINEMENT)
    srv = OfficeServer(store.root, IDENTITY, port=0, interval=0.05)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.httpd.shutdown()
    thread.join(5)


def test_server_serves_the_page_and_the_state_on_localhost(server: OfficeServer) -> None:
    assert server.url.startswith("http://127.0.0.1:")
    page = httpx.get(server.url, timeout=5).text
    assert "<canvas" in page and "DUNDER MIFFLIN" in page
    state = httpx.get(server.url + "api/state", timeout=5).json()
    assert [b["kind"] for b in state["beats"]] == ["arrive"]
    assert state["tickets"][0]["key"] == "PILOT-3"
    assert state["supervisor"]["running"] is False
    assert httpx.get(server.url + "nope", timeout=5).status_code == 404
