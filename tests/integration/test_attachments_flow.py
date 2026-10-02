"""Ticket attachments reach Claude's session as read-only inputs, and count as brief changes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import make_world, step

KEY = "PILOT-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _envelope(w, procedure: str) -> dict:  # type: ignore[no-untyped-def]
    inv = next(i for i in w.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"]))
    prompt = inv["argv"][inv["argv"].index("-p") + 1]
    return json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text())


async def test_designs_reach_refinement_and_planning(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    w.jira.attach(KEY, "home.png", PNG, "image/png")
    w.jira.attach(KEY, "script.svg", b"<svg/>", "image/svg+xml")
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        env = _envelope(w, "refine-ticket")
        [ref] = env["attachments"]
        assert ref["filename"] == "home.png" and ref["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert Path(ref["path"]).read_bytes() == PNG
        assert "/inputs/attachments/" in ref["path"]  # inside the run's read-only inputs
        assert env["attachments_skipped"][0]["filename"] == "script.svg"
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        assert [a["filename"] for a in _envelope(w, "plan-ticket")["attachments"]] == ["home.png"]
