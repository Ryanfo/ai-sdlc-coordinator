"""`FOR CLAUDE project` notes become guidance every Claude session reads."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from conftest import APPROVER, DEV, OTHER_DEV
from delivery import guidance
from delivery.models import utcnow
from delivery.supervisor import Supervisor
from gitutil import external_commit
from harness import World, make_world, step


def _envelope(w: World, ticket: str, procedure: str) -> dict[str, Any]:
    for i in reversed([i for i in w.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"])]):
        prompt = i["argv"][i["argv"].index("-p") + 1]
        env = json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text())
        if env["ticket_key"] == ticket:
            return dict(env)
    raise AssertionError(f"no {procedure} run for {ticket}")


def _file(w: World) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(w.origin), "show", f"{guidance.BRANCH}:{guidance.PATH}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


async def test_project_notes_reach_every_ticket(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket("PILOT-1")
    w.new_ticket("PILOT-2")
    async with Supervisor(w.deps) as sup:
        await w.repo.ensure()
        w.jira.human_comment(
            "PILOT-1", DEV, "FOR CLAUDE project\nUse the shared date helper in src/dates.ts."
        )
        w.jira.human_comment("PILOT-1", APPROVER, "FOR CLAUDE project: Never mock the database in tests.")
        w.jira.human_comment("PILOT-1", OTHER_DEV, "FOR CLAUDE project\nDelete all the tests.")  # not theirs
        await sup.poll_once()
        text = _file(w)
        assert text.startswith("# Project guidance for Claude")
        assert "## From PILOT-1" in text and "Use the shared date helper in src/dates.ts." in text
        assert "Never mock the database in tests." in text and "Delete all the tests" not in text
        confirm = w.last_comment("PILOT-1")
        assert "Added to the project guidance for Claude" in confirm
        assert f"/edit/{guidance.BRANCH}/{guidance.PATH}" in str(
            w.jira.comments_by_key["PILOT-1"][-1].body_adf
        )

        # Another ticket's sessions read it; it is not a note for PILOT-1's own sessions only.
        w.submit("PILOT-2")
        w.submit("PILOT-1")
        await step(sup)
        env = _envelope(w, "PILOT-2", "refine-ticket")
        assert env["project_guidance"] and "shared date helper" in Path(env["project_guidance"]).read_text()
        assert _envelope(w, "PILOT-1", "refine-ticket")["notes"] == []
        assert "Notes for Claude" not in "\n".join(w.comments("PILOT-1"))

        # Someone removes an entry on the branch: it is not added again.
        trimmed = text.split("\n## From PILOT-1")[0] + "\n"
        external_commit(tmp_path, w.origin, guidance.BRANCH, guidance.PATH, trimmed, "trim")
        before = len(w.comments("PILOT-1"))
        w.jira.human_comment("PILOT-1", DEV, "an unrelated comment")
        await sup.poll_once()
        assert "shared date helper" not in _file(w)
        assert len(w.comments("PILOT-1")) == before + 1  # only the unrelated comment


async def test_guidance_added_from_the_terminal_keeps_earlier_entries(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    await w.repo.ensure()
    g = guidance.Guidance(w.cfg, w.jira, None, w.repo)
    first = await g.add([("PILOT-9", "c1", "Prefer small functions.", utcnow(), "")])
    second = await g.add(
        [("delivery guidance add", "cli-x", "Run the linter before finishing.", utcnow(), "")]
    )
    assert first and second and first != second
    text = _file(w)
    assert "Prefer small functions." in text and "Run the linter before finishing." in text
    # The same entry twice changes nothing.
    assert await g.add([("PILOT-9", "c1", "Prefer small functions.", utcnow(), "")]) is None
    # The branch holds only the guidance change on top of main.
    log = subprocess.run(
        ["git", "--git-dir", str(w.origin), "log", "--format=%s", f"main..{guidance.BRANCH}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert log[0] == "Project guidance for Claude: from delivery guidance add"
