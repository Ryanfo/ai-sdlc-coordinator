"""Bugs: reproduce first, and show the regression test fails on the base branch without the fix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, make_world, step

# Runs every tests/*.test.sh; the app's bug is that x is 1 where it should be 2.
UNIT = ["sh", "-c", 'for f in tests/*.test.sh; do [ -e "$f" ] || continue; sh "$f" || exit 1; done']
FIX = {"src/app.ts": "export const x = 2;\n"}


def _envelope(w: World, procedure: str) -> dict[str, Any]:
    inv = [i for i in w.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"])][-1]
    prompt = inv["argv"][inv["argv"].index("-p") + 1]
    return dict(json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text()))


async def _to_code_review(w: World, sup: Supervisor, key: str, issue_type: str) -> str:
    w.new_ticket(key, issue_type=issue_type)
    w.submit(key)
    await step(sup)
    w.decide(key, f"APPROVE SPEC {w.token(key, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    w.decide(key, f"APPROVE PLAN {w.token(key, 'PLAN')}", Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    assert w.jira.status_of(key) is Status.CODE_REVIEW, w.last_comment(key)
    return w.last_comment(key)


async def test_a_bug_fix_shows_its_regression_test_fails_on_main(tmp_path: Path) -> None:
    w = make_world(tmp_path, checks={"unit": UNIT})
    regression = {"tests/x.test.sh": 'grep -q "x = 2" src/app.ts\n'}
    w.scenario({"implement-ticket": [{"edit": {**FIX, **regression}}]})
    async with Supervisor(w.deps) as sup:
        gate = await _to_code_review(w, sup, "PILOT-1", "Bug")
    assert _envelope(w, "refine-ticket")["work_kind"] == "bug"
    assert "Bug reproduced" in gate and "tests/x.test.sh" in gate and "the unit check fails" in gate
    rep = json.loads(next(Path(w.cfg.runtime.state_dir).rglob("inputs/reproduction.json")).read_text())
    assert rep["state"] == "reproduced" and rep["tests"] == ["tests/x.test.sh"]


async def test_a_test_that_passes_without_the_fix_is_pointed_out(tmp_path: Path) -> None:
    w = make_world(tmp_path, checks={"unit": UNIT})
    w.scenario({"implement-ticket": [{"edit": {**FIX, "tests/y.test.sh": "true\n"}}]})
    async with Supervisor(w.deps) as sup:
        gate = await _to_code_review(w, sup, "PILOT-1", "Bug")
    assert "Bug not reproduced" in gate and "tests/y.test.sh" in gate


async def test_a_bug_fix_without_tests_and_a_story_are_handled_as_such(tmp_path: Path) -> None:
    w = make_world(tmp_path, checks={"unit": UNIT})
    w.scenario({"implement-ticket": [{"edit": FIX}]})
    async with Supervisor(w.deps) as sup:
        bug = await _to_code_review(w, sup, "PILOT-1", "Bug")
        story = await _to_code_review(w, sup, "PILOT-2", "Story")
    assert "No regression test" in bug
    assert "reproduc" not in story.lower() and "regression test" not in story
    assert _envelope(w, "implement-ticket")["work_kind"] == "feature"
