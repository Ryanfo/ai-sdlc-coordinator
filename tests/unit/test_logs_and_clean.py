"""The coordinator's saved log, `coordinator clean`, `coordinator open` and `coordinator logs`."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from delivery import cleanup, cli, logfile
from delivery.models import RunRecord, RunState, Stage
from delivery.open_sessions import OpenRecord, SessionRegistry
from harness import make_world


def test_the_log_keeps_terminal_output_even_when_the_terminal_is_gone(tmp_path: Path) -> None:
    path = tmp_path / "s" / "coordinator.log"
    handler = logfile.attach(path)
    try:

        def gone(_: str) -> None:
            raise OSError(5, "Input/output error")

        logfile.emitter(gone)("=====\n STARTED  Planning  PILOT-7\n=====")
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            logging.getLogger("delivery").exception("run crashed")
    finally:
        logfile.detach(handler)
    text = path.read_text()
    assert text.startswith("\n----- ") and "STARTED  Planning  PILOT-7" in text
    assert "ERROR run crashed" in text and "RuntimeError: kaboom" in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert logfile.tail(path, 2)[-1].endswith("RuntimeError: kaboom")


def test_follow_continues_across_rotation(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.log"
    path.write_text("old\n")
    got: list[str] = []

    def write() -> None:
        time.sleep(0.2)
        with path.open("a") as fh:
            fh.write("one\n")
        time.sleep(0.2)
        os.rename(path, tmp_path / "coordinator.log.1")
        path.write_text("two\n")

    t = threading.Thread(target=write)
    t.start()
    for line in logfile.follow(path, poll=0.05, stop=lambda: len(got) >= 2):
        got.append(line)
    t.join()
    assert got == ["one", "two"]


async def test_clean_removes_only_what_finished_runs_left(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    await w.repo.ensure()
    store, root = w.deps.store, w.cfg.repository.worktree_root

    def run(run_id: str, state: RunState) -> None:
        store.run_journal("PILOT-1", run_id).create(
            RunRecord(
                ticket_key="PILOT-1",
                run_id=run_id,
                attempt=1,
                stage=Stage.DEVELOPMENT,
                developer_account_id="dev",
                worker_id="w",
                session_label=run_id,
                state=state,
                attempt_key=run_id,
            )
        )

    run("PILOT-1-done", RunState.AWAITING_HUMAN)
    run("PILOT-1-paused", RunState.INTERRUPTED)
    run("PILOT-1-open", RunState.AWAITING_HUMAN)
    finished = await w.repo.add_worktree(root / "PILOT-1" / "PILOT-1-done" / "feature", start="origin/main")
    (finished / "draft.ts").write_text("never pushed\n")
    (root / "PILOT-1" / "PILOT-1-paused" / "feature").mkdir(parents=True)
    (root / "PILOT-1" / "PILOT-1-paused" / "feature" / "x").write_text("x")
    (root / "PILOT-1" / "PILOT-1-open" / "delivery").mkdir(parents=True)
    (root / "PILOT-9" / "PILOT-9-gone").mkdir(parents=True)
    kept_open = str(root / "PILOT-1" / "PILOT-1-open" / "delivery")
    registry = SessionRegistry(w.cfg.runtime.state_dir)
    common = {"ticket_key": "PILOT-1", "stage": Stage.DEVELOPMENT, "procedure": "implement-ticket"}
    registry.save(
        OpenRecord(
            name="PILOT-1-live",
            run_id="PILOT-1-open",
            worktree=kept_open,
            worktrees=[kept_open],
            journal_dir=str(tmp_path),
            session_dir=str(tmp_path),
            **common,  # type: ignore[arg-type]
        )
    )
    registry.save(
        OpenRecord(
            name="PILOT-1-ended",
            run_id="PILOT-1-x",
            worktree="",
            journal_dir=str(tmp_path),
            session_dir=str(tmp_path),
            **common,  # type: ignore[arg-type]
        )
    )

    p = cleanup.plan(w.cfg, live_sessions={"PILOT-1-live"})
    assert [i.path.name for i in p.worktrees] == ["PILOT-1-done"]
    assert sorted(i.path.name for i in p.kept) == ["PILOT-1-open", "PILOT-1-paused"]
    assert [d.name for d in p.empty] == ["PILOT-9-gone"]
    assert [r.name for r in p.dead_sessions] == ["PILOT-1-ended"]

    shown: list[str] = []
    await cleanup.apply(w.cfg, w.repo, p, shown.append)
    assert not (root / "PILOT-1" / "PILOT-1-done").exists() and not (root / "PILOT-9").exists()
    assert (root / "PILOT-1" / "PILOT-1-paused" / "feature" / "x").exists()
    assert Path(kept_open).is_dir()
    patch = store.run_journal("PILOT-1", "PILOT-1-done").dir / "leftover-feature.patch"
    assert "never pushed" in patch.read_text() and "changed files saved" in shown[0]
    listed = subprocess.run(
        ["git", "--git-dir", str(w.repo.git_dir), "worktree", "list"], capture_output=True, text=True
    ).stdout
    assert "PILOT-1-done" not in listed


def test_clean_command_asks_first_without_yes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = make_world(tmp_path)
    (w.cfg.repository.worktree_root / "PILOT-3" / "PILOT-3-gone").mkdir(parents=True)
    cfg = ["--config", str(w.cfg.source_path)]
    assert cli.main(["clean", *cfg]) == 0
    assert "Nothing removed; run `coordinator clean --yes`" in capsys.readouterr().out
    assert (w.cfg.repository.worktree_root / "PILOT-3").exists()
    assert cli.main(["clean", "--yes", *cfg]) == 0
    assert not (w.cfg.repository.worktree_root / "PILOT-3").exists()
    assert cli.main(["clean", *cfg]) == 0
    assert "Nothing to clean." in capsys.readouterr().out


def test_logs_without_a_ticket_and_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path)
    cfg = ["--config", str(w.cfg.source_path)]
    assert cli.main(["logs", *cfg]) == cli.EXIT_FAIL
    assert "No coordinator log yet" in capsys.readouterr().out
    path = logfile.log_path(w.cfg.runtime.state_dir, w.cfg.identity_key)
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"line {n}\n" for n in range(100)))
    assert cli.main(["logs", "-n", "3", *cfg]) == 0
    out = capsys.readouterr().out
    assert "line 99" in out and "line 96" not in out
    assert cli.main(["logs", "--raw", *cfg]) == 0
    assert capsys.readouterr().out.strip() == str(path)

    opened: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: opened.append(argv))
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/xdg-open")
    assert cli.main(["open", *cfg]) == 0
    assert opened[-1][-1] == str(path)
    assert cli.main(["open", "PILOT-1", "--jira", *cfg]) == 0
    assert opened[-1][-1] == "https://example.atlassian.net/browse/PILOT-1"
    assert cli.main(["open", "PILOT-1", *cfg]) == cli.EXIT_FAIL
    # A run with a session log: its readable copy is refreshed and opened.
    run_dir = w.deps.store.run_journal("PILOT-1", "PILOT-1-refinement-x").dir
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "logs" / "claude-refine-ticket.jsonl").write_text(
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the brief."}]}}\n'
    )
    (run_dir / "snapshot.json").write_text(
        RunRecord(
            ticket_key="PILOT-1",
            run_id="PILOT-1-refinement-x",
            attempt=1,
            stage=Stage.REFINEMENT,
            developer_account_id="d",
            worker_id="w",
            session_label="s",
            state=RunState.RUNNING,
            attempt_key="k",
        ).model_dump_json()
    )
    assert cli.main(["open", "PILOT-1", *cfg]) == 0
    readable = Path(opened[-1][-1])
    assert readable.name == "claude-refine-ticket.txt" and "Reading the brief." in readable.read_text()
    assert cli.main(["open", "PILOT-1", "--folder", *cfg]) == 0
    assert opened[-1][-1] == str(run_dir)
