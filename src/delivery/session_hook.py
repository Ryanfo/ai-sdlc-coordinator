"""Claude Code hook for interactive sessions: records events and holds Claude to its result.

Runs as ``python -m delivery.session_hook <event> <session dir>`` from the hooks in the
coordinator-generated settings file. It appends what happened to ``events.jsonl`` in the
session directory, which the session itself cannot write. On Stop it checks the result file
against the envelope; until the result is valid it tells Claude to carry on (at most
MAX_BLOCKS times in a row), so an interactive session finishes a procedure the way
``claude -p --json-schema`` does. Once the coordinator has taken the result (``handed-off``
exists) it only records events. It never fails the session: any error is recorded instead.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVENTS = "events.jsonl"
EXPECT = "expect.json"
HANDED_OFF = "handed-off"
MAX_BLOCKS = 3
EVENT_NAMES = {"SessionStart": "start", "UserPromptSubmit": "prompt", "Stop": "stop", "SessionEnd": "end"}


def append(session_dir: Path, rec: dict[str, Any]) -> None:
    with (session_dir / EVENTS).open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def read_events(session_dir: Path) -> list[dict[str, Any]]:
    try:
        lines = (session_dir / EVENTS).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def blocks_in_a_row(events: list[dict[str, Any]]) -> int:
    """Stops refused since the last prompt (a person typing starts a fresh count)."""
    n = 0
    for ev in events:
        if ev.get("event") == "prompt":
            n = 0
        elif ev.get("event") == "stop" and ev.get("result") == "blocked":
            n += 1
    return n


def _tail(path: Path, max_bytes: int = 200_000) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return
    for line in data.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def api_error_text(ev: dict[str, Any]) -> str | None:
    """The text of a transcript entry that records a failed API call, else None."""
    if not (ev.get("isApiErrorMessage") or (ev.get("type") == "assistant" and ev.get("error"))):
        return None
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, list):
        text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    else:
        text = str(content or "")
    return (text or str(ev.get("error") or "API error")).strip()[:500]


def last_api_error(transcript: str | None) -> str | None:
    """The API error that ended the last turn, if the last assistant entry is one."""
    if not transcript:
        return None
    last: str | None = None
    for ev in _tail(Path(transcript)):
        if ev.get("type") == "assistant":
            last = api_error_text(ev)
    return last


def check_result(expect: dict[str, Any]) -> str | None:
    """Return what is wrong with the result file, or None when it is valid."""
    from delivery.results import OutputInvalid, validate_result
    from delivery.workflow import Stage

    path = Path(expect["result_path"])
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return f"{path} has not been written"
    except (OSError, json.JSONDecodeError) as exc:
        return f"{path} is not valid JSON ({exc})"
    if not isinstance(raw, dict):
        return f"{path} must contain one JSON object"
    if "required_keys" in expect:  # a diagnostic result (doctor --claude-probe), not a stage result
        missing = [k for k in expect["required_keys"] if k not in raw]
        if missing:
            return f"{path} is missing {missing}"
        if raw.get("contract_id") != expect.get("contract_id"):
            return f"contract_id must be {expect.get('contract_id')!r}"
        return None
    try:
        validate_result(
            raw,
            contract_id=expect["contract_id"],
            procedure=expect["procedure"],
            run_id=expect["run_id"],
            ticket=expect["ticket"],
            stage=Stage(expect["stage"]),
            input_revision=expect["input_revision"],
        )
    except OutputInvalid as exc:
        return str(exc)
    if expect.get("procedure") == "resolve-blocker":
        # Claude is told here, in the session, when a step it asks a person to take would not work.
        from delivery.resolution import check_next_steps

        return check_next_steps(raw, expect)
    return None


def block_reason(expect: dict[str, Any], problem: str) -> str:
    return (
        f"The delivery coordinator has not received your result yet: {problem}. Finish the "
        f"procedure, then use the Write tool to write the structured result as one JSON object to "
        f"{expect['result_path']} (it must match the schema in {expect['schema_path']}), then stop."
    )


def handle(event: str, session_dir: Path, data: dict[str, Any]) -> dict[str, Any] | None:
    """Record the event; return the hook's JSON reply, if any."""
    rec: dict[str, Any] = {
        "event": event,
        "at": datetime.now(UTC).isoformat(),
        "session_id": data.get("session_id"),
        "transcript_path": data.get("transcript_path"),
    }
    if event == "prompt":
        rec["prompt"] = str(data.get("prompt", ""))[:4000]
    if event == "end":
        rec["reason"] = data.get("reason")
    reply = None
    if event == "stop" and not (session_dir / HANDED_OFF).exists():
        expect = json.loads((session_dir / EXPECT).read_text())
        problem = check_result(expect)
        if problem is None:
            rec["result"] = "valid"
        elif (err := last_api_error(data.get("transcript_path"))) is not None:
            rec.update(result="api_error", detail=err)
        elif blocks_in_a_row(read_events(session_dir)) >= int(expect.get("max_blocks", MAX_BLOCKS)):
            rec.update(result="missing", detail=problem)
        else:
            rec.update(result="blocked", detail=problem)
            reply = {"decision": "block", "reason": block_reason(expect, problem)}
    append(session_dir, rec)
    return reply


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m delivery.session_hook <event> <session dir>", file=sys.stderr)
        return 0
    event, session_dir = argv[1], Path(argv[2])
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        data = {}
    try:
        reply = handle(event, session_dir, data if isinstance(data, dict) else {})
    except Exception as exc:  # never break the session over bookkeeping
        with contextlib.suppress(OSError):
            append(session_dir, {"event": event, "hook_error": str(exc)[:500]})
        return 0
    if reply is not None:
        print(json.dumps(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
