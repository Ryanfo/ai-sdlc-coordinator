"""`coordinator` in the background: a real tmux server and a stand-in supervisor (no Jira needed)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import ConfigFactory
from delivery import background, logfile
from delivery.config import Config

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

# Holds the supervisor lock and answers the control socket like `delivery run` does.
STAND_IN = """\
import asyncio, signal, sys
from pathlib import Path
from delivery.config import load_config
from delivery.control import ControlServer, socket_path
from delivery.ownership import supervisor_lock

async def main():
    cfg = load_config(Path(sys.argv[1]))
    lock = supervisor_lock(cfg)
    lock.acquire({"worker_id": "stand-in"})
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    async def handle(req):
        if req.get("cmd") == "shutdown":
            stop.set()
            return {"ok": True, "running_sessions": 2}
        return {"ok": True}

    server = ControlServer(socket_path(cfg.runtime.state_dir, cfg.identity_key), handle)
    await server.start()
    print("stand-in supervisor running", flush=True)
    await stop.wait()
    await server.stop()
    lock.release()

asyncio.run(main())
"""


@pytest.fixture
def cfg(make_config: ConfigFactory, tmp_path: Path) -> Iterator[Config]:
    c = make_config(overrides={"claude.interactive": {"socket": f"dlvbg-{tmp_path.name}"[-30:]}})
    yield c
    subprocess.run(background.server(c).argv("kill-server"), capture_output=True, check=False)


def _until(cond, timeout: float = 10) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.1)


def test_start_runs_in_the_background_and_stop_stops_it(cfg: Config, tmp_path: Path) -> None:
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    assert cfg.source_path is not None
    before = background.state(cfg)
    assert not before.running and not before.in_background
    assert background.describe(cfg, before) == "not running"

    shown: list[str] = []
    command = [sys.executable, str(script), str(cfg.source_path)]
    assert background.start(cfg, command=command, emit=shown.append), shown
    st = background.state(cfg)
    assert st.running and st.in_background and st.pid
    assert "running in the background" in background.describe(cfg, st)
    # What a person would see when they attach: the supervisor's own terminal.
    _until(lambda: "stand-in supervisor running" in _pane(cfg))
    assert background.attach_argv(cfg)[-3:] == ["attach-session", "-t", "=coordinator"]

    assert background.stop(cfg, emit=shown.append)
    assert any("saving 2 running sessions" in s for s in shown)
    _until(lambda: not background.state(cfg).in_background)
    assert not background.state(cfg).running
    assert background.stop(cfg, emit=shown.append) and shown[-1] == "The coordinator is not running."


def test_a_coordinator_that_stops_at_once_says_why(cfg: Config) -> None:
    path = logfile.log_path(cfg.runtime.state_dir, cfg.identity_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("an earlier start\n")
    fail = f"open({str(path)!r}, 'a').write('Jira credentials: no token stored\\n')"
    shown: list[str] = []
    assert not background.start(cfg, command=[sys.executable, "-c", fail], emit=shown.append)
    assert "stopped as soon as it started" in shown[0]
    assert any("no token stored" in s for s in shown)
    assert not any("an earlier start" in s for s in shown)


def _pane(cfg: Config) -> str:
    res = subprocess.run(
        background.server(cfg).argv("capture-pane", "-p", "-t", "=coordinator:"),
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout
