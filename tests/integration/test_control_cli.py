"""Operator commands reach a running supervisor through the local control socket."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from delivery.cli import _control
from delivery.supervisor import Supervisor
from harness import drain, make_world


async def test_status_pause_and_stop_through_the_socket(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"sleep": 20}]})
    for k in ("PILOT-1", "PILOT-2"):
        w.new_ticket(k)
        w.submit(k)
    async with Supervisor(w.deps) as sup:
        sock = Path(str(sup.record.control_socket))
        assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
        await sup.deps.repo.ensure()
        await sup.poll_once()
        await asyncio.sleep(1)
        status = await _control(w.cfg, {"cmd": "status"})
        assert status and {s["ticket"] for s in status["sessions"]} == {"PILOT-1", "PILOT-2"}
        assert all(s["run_id"] and s["started_at"] and s["child_pid"] for s in status["sessions"])
        paused = await _control(w.cfg, {"cmd": "pause", "reason": "busy"})
        assert paused and paused["dispatch_paused"] and paused["running_sessions"] == 2
        stopped = await _control(w.cfg, {"cmd": "stop", "ticket": "PILOT-1"})
        assert stopped and stopped["ok"] and stopped["other_sessions"] == ["PILOT-2"]
        unknown = await _control(w.cfg, {"cmd": "bogus"})
        assert unknown and not unknown["ok"]
        sup.sessions["PILOT-2"].rc.stop_reason = "test end"
        await sup.stop_ticket("PILOT-2", "test end")
        await drain(sup)
    assert await _control(w.cfg, {"cmd": "status"}) is None  # socket removed on shutdown
