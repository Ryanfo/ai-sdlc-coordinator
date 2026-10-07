"""Figma frames linked in a ticket: snapshotted for each stage, pinned at refinement."""

from __future__ import annotations

import json
from pathlib import Path

from delivery.supervisor import Supervisor
from delivery.workflow import Status
from fakes.figma import FakeFigma, frame, text
from harness import make_world, step

KEY = "PILOT-1"
FILE = "AbCdEfGhIj1234567890"
DESC = (
    "Users need a welcome block. AC1: shows HELLO WORLD.\n"
    f"Design: https://www.figma.com/design/{FILE}/Task-app?node-id=1-2&t=x"
)


def _envelope(w, procedure: str) -> dict:  # type: ignore[no-untyped-def]
    inv = [i for i in w.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"])][-1]
    prompt = inv["argv"][inv["argv"].index("-p") + 1]
    return json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text())


async def test_design_is_pinned_at_refinement_and_drift_is_reported(tmp_path: Path) -> None:
    fig = FakeFigma()
    v1 = fig.publish(FILE, {"1:2": frame("1:2", "Home", text("1:3", "HELLO WORLD"))})
    w = make_world(tmp_path)
    w.deps.figma = fig
    w.new_ticket(KEY, description=DESC)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        [d] = _envelope(w, "refine-ticket")["designs"]
        assert (d["frame_name"], d["version"], d["changed_in_figma_since"]) == ("Home", v1, False)
        assert '"HELLO WORLD"' in Path(d["summary_path"]).read_text()
        assert w.record(KEY).design_versions == {FILE: v1}
        # The designer changes the copy after the specification was written.
        fig.publish(FILE, {"1:2": frame("1:2", "Home", text("1:3", "Hello, world!"))})
        w.move(KEY, Status.READY_PLANNING)
        await step(sup)
        [d] = _envelope(w, "plan-ticket")["designs"]
        assert d["version"] == v1 and d["changed_in_figma_since"] is True
        assert '"HELLO WORLD"' in Path(d["summary_path"]).read_text()
        assert any("To adopt the new Figma design" in c for c in w.comments(KEY))
        assert w.jira.status_of(KEY) is Status.PLAN_REVIEW


async def test_without_a_figma_token_the_link_is_listed_as_skipped(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY, description=DESC)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
    env = _envelope(w, "refine-ticket")
    assert env["designs"] == []
    assert "delivery credentials set figma" in env["designs_skipped"][0]["reason"]
