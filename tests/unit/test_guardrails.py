from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from conftest import ConfigFactory
from delivery.guardrails import LoopDetector, Watchdog


def call(i: int, cmd: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": cmd}}]
        },
    }


def result(i: int, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": text}]},
    }


def test_same_step_with_same_result_is_a_loop() -> None:
    d = LoopDetector(repeats=4)
    reasons = []
    for i in range(4):
        d.feed(call(i, "npm run test:e2e"))
        reasons.append(d.feed(result(i, "Error: port in use")))
    assert reasons[:3] == [None, None, None]
    assert (
        reasons[3]
        and "same step with the same result 4 times" in reasons[3]
        and "npm run test:e2e" in reasons[3]
    )


def test_rerunning_tests_after_changes_is_not_a_loop() -> None:
    d = LoopDetector(repeats=4)
    for i in range(12):
        d.feed(call(2 * i, "npm run test:unit"))
        assert d.feed(result(2 * i, f"{i} failing")) is None  # different result each time
        d.feed(call(2 * i + 1, f"edit {i}"))
        assert d.feed(result(2 * i + 1, "ok")) is None


def test_alternating_loop_is_caught() -> None:
    d = LoopDetector(repeats=3)
    found = None
    for i in range(6):
        cmd = "a" if i % 2 == 0 else "b"
        d.feed(call(i, cmd))
        found = found or d.feed(result(i, "same"))
    assert found


async def test_watchdog_stops_a_stalled_session(tmp_path: Path) -> None:
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n")
    stopped: list[bool] = []

    async def stop() -> None:
        stopped.append(True)

    w = Watchdog(log, loop_repeats=5, stall_seconds=0.3, stop=stop, poll=0.05)
    await asyncio.wait_for(w.run(), timeout=5)
    assert stopped and w.reason and "nothing happened" in w.reason


async def test_watchdog_reads_the_live_log_and_stops_a_loop(tmp_path: Path) -> None:
    log = tmp_path / "s.jsonl"
    log.write_text("")
    stopped: list[bool] = []

    async def stop() -> None:
        stopped.append(True)

    w = Watchdog(log, loop_repeats=3, stall_seconds=30, stop=stop, poll=0.05)
    task = asyncio.create_task(w.run())
    with log.open("a") as fh:
        for i in range(3):
            fh.write(json.dumps(call(i, "npm test")) + "\n" + json.dumps(result(i, "fail")) + "\n")
            fh.flush()
            await asyncio.sleep(0.1)
    await asyncio.wait_for(task, timeout=5)
    assert stopped and w.reason and "3 times" in w.reason


def test_turn_limits_and_timeouts_default_generously_and_can_be_set(make_config: ConfigFactory) -> None:
    c = make_config().claude
    assert c.turns_for("implement-ticket") == 500 and c.turns_for("refine-ticket") == 150
    assert c.timeout_for("implement-ticket", 1800) == 7200 and c.timeout_for("plan-ticket", 1800) == 1800
    c = make_config(
        overrides={
            "claude.turn_limits": {"implement-ticket": 900},
            "claude.timeout_minutes": {"plan-ticket": 50},
        }
    ).claude
    assert c.turns_for("implement-ticket") == 900 and c.timeout_for("plan-ticket", 1800) == 3000
