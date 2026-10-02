from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from delivery.proc import run_process

PY = sys.executable


async def test_log_file_fills_while_the_child_runs(tmp_path: Path) -> None:
    log = tmp_path / "out.jsonl"
    code = "import sys, time\nprint('first', flush=True)\ntime.sleep(1.5)\nprint('second', flush=True)"
    task = asyncio.create_task(run_process([PY, "-c", code], cwd=tmp_path, stdout_path=log, timeout=30))
    await asyncio.sleep(0.8)
    assert log.read_text() == "first\n"  # visible before the child finishes
    res = await task
    assert res.stdout == "first\nsecond\n" and log.read_text() == "first\nsecond\n"
    assert oct(log.stat().st_mode & 0o777) == "0o600"


async def test_timeout_keeps_what_was_written(tmp_path: Path) -> None:
    log = tmp_path / "out.jsonl"
    code = "import time\nprint('started', flush=True)\ntime.sleep(60)"
    res = await run_process([PY, "-c", code], cwd=tmp_path, stdout_path=log, timeout=1)
    assert res.timed_out and res.stdout == "started\n" and log.read_text() == "started\n"


async def test_over_the_cap_keeps_first_and_last_lines(tmp_path: Path) -> None:
    code = "for i in range(5000): print(f'line {i:05d}')\nprint('{\"type\": \"result\"}')"
    res = await run_process([PY, "-c", code], cwd=tmp_path, max_capture=20_000)
    assert res.stdout.startswith("line 00000\n")
    assert "...[output truncated]..." in res.stdout
    assert res.stdout.endswith('{"type": "result"}\n')  # the final result event is never lost


async def test_stdin_and_secret_redaction_in_the_log(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    secret = "ghp_" + "a" * 36
    code = "import sys\nprint(sys.stdin.read().upper())"
    res = await run_process([PY, "-c", code], cwd=tmp_path, stdout_path=log, stdin_data=b"hello", timeout=30)
    assert res.stdout == "HELLO\n"
    res = await run_process([PY, "-c", f"print('{secret}')"], cwd=tmp_path, stdout_path=log, timeout=30)
    assert secret not in log.read_text()
