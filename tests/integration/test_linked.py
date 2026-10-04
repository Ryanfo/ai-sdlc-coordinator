"""Linked tickets reach Claude with their approved documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delivery.ports import IssueLink
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, make_world, step


def _envelope(w: World, ticket: str, procedure: str) -> dict[str, Any]:
    inv = [i for i in w.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"])]
    for i in reversed(inv):
        prompt = i["argv"][i["argv"].index("-p") + 1]
        env = json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text())
        if env["ticket_key"] == ticket:
            return dict(env)
    raise AssertionError(f"no {procedure} run for {ticket}")


async def test_a_bug_gets_the_story_it_was_found_in(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    async with Supervisor(w.deps) as sup:
        w.new_ticket("PILOT-1", summary="Search tasks")
        w.submit("PILOT-1")
        await step(sup)
        w.decide("PILOT-1", f"APPROVE SPEC {w.token('PILOT-1', 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
        w.decide("PILOT-1", f"APPROVE PLAN {w.token('PILOT-1', 'PLAN')}", Status.READY_DEVELOPMENT)
        await step(sup)
        assert w.record("PILOT-1").pr_number

        w.new_ticket(
            "PILOT-2",
            summary="Search ignores capitals",
            description="Searching for MILK finds nothing although a task called milk exists.",
            issue_type="Bug",
            links=[
                IssueLink("Problem/Incident", "outward", "is caused by", "PILOT-1"),
                IssueLink("Relates", "outward", "relates to", "PILOT-404"),  # not readable: left out
                IssueLink("Relates", "inward", "relates to", "PILOT-1"),  # the same ticket twice
            ],
        )
        w.submit("PILOT-2")
        await step(sup)
        env = _envelope(w, "PILOT-2", "refine-ticket")
        (linked,) = env["linked_tickets"]
        assert linked["key"] == "PILOT-1" and linked["relation"] == "is caused by"
        assert linked["summary"] == "Search tasks" and linked["issue_type"] == "Story"
        assert linked["status"] in ("Ready for verification", "Verifying")  # verified in the same poll
        assert linked["pull_request"].endswith(f"/pull/{w.record('PILOT-1').pr_number}")
        docs = {d["kind"]: d for d in linked["documents"]}
        assert set(docs) == {"specification", "plan"}
        assert "AC1" in Path(docs["specification"]["path"]).read_text()
        assert docs["plan"]["url"].startswith("https://github.com/example/app/blob/")
        assert "linked_tickets" in env["instructions"]
        # A ticket without links gets none.
        assert _envelope(w, "PILOT-1", "plan-ticket")["linked_tickets"] == []
