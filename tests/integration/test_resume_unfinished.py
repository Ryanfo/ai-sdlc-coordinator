"""A development session that runs out of turns keeps its changes; Resume continues from them."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import DEV
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import make_world, step

KEY = "PILOT-1"


def _envelopes(w) -> list[dict]:  # type: ignore[no-untyped-def]
    out = []
    # Invocation files are named by a random ID; order them by when each session started.
    for inv in sorted(w.invocations(), key=lambda i: i["started"]):
        if "/delivery:implement-ticket" in " ".join(inv["argv"]):
            prompt = inv["argv"][inv["argv"].index("-p") + 1]
            out.append(json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text()))
    return out


def _show(origin: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(origin), "show", ref], capture_output=True, text=True, check=True
    ).stdout


async def test_resume_continues_from_the_unfinished_changes(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario(
        {
            "implement-ticket": [
                {
                    "edit": {"src/style.css": "body { margin: 0 }\n", "src/a.test.ts": "draft\n"},
                    "max_turns": True,
                },
                {"edit": {"src/a.test.ts": "finished\n"}},
            ]
        }
    )
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.move(KEY, Status.READY_PLANNING)
        await step(sup)
        w.move(KEY, Status.READY_DEVELOPMENT)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.BLOCKED
        blocked = w.last_comment(KEY)
        assert "turn_limits" in blocked and "delivery logs PILOT-1" in blocked
        assert "unfinished changes (2 files) were kept" in blocked
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)  # Resume development
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
    first, second = _envelopes(w)
    assert first["prior_work"] is None
    assert second["prior_work"]["run_id"] == first["run_id"]
    assert second["prior_work"]["files"] == ["src/a.test.ts", "src/style.css"]
    tail = Path(second["prior_work"]["session_tail_path"]).read_text()
    assert "Still working on the e2e tests." in tail
    # The candidate keeps the first session's stylesheet and the second session's test.
    assert _show(w.origin, f"feature/{KEY}:src/style.css") == "body { margin: 0 }\n"
    assert _show(w.origin, f"feature/{KEY}:src/a.test.ts") == "finished\n"


async def test_a_looping_session_is_stopped_and_explained(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from delivery import cli

    w = make_world(tmp_path, extra={"claude.guardrails": {"loop_repeats": 3}})
    w.scenario({"implement-ticket": [{"edit": {"src/style.css": "x\n"}, "loop": 5}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.move(KEY, Status.READY_PLANNING)
        await step(sup)
        w.move(KEY, Status.READY_DEVELOPMENT)
        await step(sup)
    assert w.jira.status_of(KEY) is Status.BLOCKED
    comment = w.last_comment(KEY)
    assert "stopped by a guardrail" in comment and "npm run test:e2e" in comment
    assert "not making progress" in comment and "unfinished changes (1 files) were kept" in comment
    # The readable log is available from the command line.
    assert cli.main(["logs", KEY, "--config", str(w.cfg.source_path)]) == 0
    out = capsys.readouterr().out
    assert "> Bash: $ npm run test:e2e" in out and "error" not in out.split("Session", 1)[0]
    assert cli.main(["logs", KEY, "--list", "--config", str(w.cfg.source_path)]) == 0
    assert "development" in capsys.readouterr().out
