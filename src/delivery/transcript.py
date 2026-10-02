"""Readable view of a Claude session log (``logs/claude-<procedure>.jsonl``) for `delivery logs`.

The log is Claude Code's stream-json output, one event per line, written while the session
runs. This turns it into what a person wants to see: what Claude said, which tools it used on
which files or commands, what was denied, and how the session ended.
"""

from __future__ import annotations

import json
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

WIDTH = 100


def _short(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _rel(path: str, root: str | None) -> str:
    if root and path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1 :]
    return path


def _tool(name: str, inp: dict[str, Any], root: str | None) -> str:
    if name == "Bash":
        return f"$ {_short(str(inp.get('command', '')), 160)}"
    if "file_path" in inp:
        return _rel(str(inp["file_path"]), root)
    if "pattern" in inp:
        return str(inp["pattern"])
    if name == "StructuredOutput":
        return f"(final result: {inp.get('outcome', '?')})"
    return _short(json.dumps(inp), 120)


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


class Renderer:
    def __init__(self, show_results: bool = False, width: int = WIDTH) -> None:
        self.root: str | None = None
        self.show_results = show_results
        self.width = width

    def event(self, ev: dict[str, Any]) -> list[str]:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.root = ev.get("cwd")
            plugins = ", ".join(p.get("name", "") for p in ev.get("plugins") or [] if isinstance(p, dict))
            return [
                f"Session {ev.get('session_id', '?')}  model {ev.get('model', '?')}",
                f"Working copy {self.root}" + (f"  plugins: {plugins}" if plugins else ""),
                "",
            ]
        if t == "assistant":
            out: list[str] = []
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    wrapped = textwrap.wrap(c["text"].strip(), self.width - 9) or [""]
                    out += [f"Claude:  {wrapped[0]}"] + [f"         {w}" for w in wrapped[1:]]
                elif c.get("type") == "tool_use":
                    out.append(
                        f"  > {c.get('name')}: {_tool(str(c.get('name')), c.get('input') or {}, self.root)}"
                    )
            return out
        if t == "user":
            out = []
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_result":
                    continue
                text = _result_text(c.get("content"))
                if c.get("is_error") and ("Permission to use" in text or "denied" in text.lower()):
                    out.append(f"    DENIED: {_short(text, 160)}")
                elif c.get("is_error"):
                    out.append(f"    error: {_short(text, 160)}")
                elif self.show_results:
                    out.append(f"    {_short(text, 160)}")
            return out
        if t == "result":
            secs = int((ev.get("duration_ms") or 0) / 1000)
            denials = ev.get("permission_denials") or []
            took = f"{secs // 60}m {secs % 60:02d}s"
            out = [
                "",
                f"Finished: {ev.get('subtype')} after {ev.get('num_turns')} turns, {took}"
                + (f", {len(denials)} permission denial(s)" if denials else ""),
            ]
            for d in denials:
                inp = d.get("tool_input") or {}
                out.append(f"  denied {d.get('tool_name')}: {_tool(str(d.get('tool_name')), inp, self.root)}")
            if ev.get("subtype") == "error_max_turns":
                out.append("  The session hit [claude] max_turns before finishing; raise it in the config.")
            return out
        return []


def _events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def render_file(path: Path, show_results: bool = False, width: int = WIDTH) -> list[str]:
    r = Renderer(show_results, width)
    out: list[str] = []
    with path.open() as fh:
        for ev in _events(iter(fh)):
            out += r.event(ev)
    return out


def transcript_path(log: Path) -> Path:
    return log.with_suffix(".txt")


def write_transcript(log: Path) -> Path | None:
    """Write the readable transcript next to the raw log (``claude-<procedure>.txt``)."""
    if not log.exists():
        return None
    out = transcript_path(log)
    try:
        out.write_text("\n".join(render_file(log, show_results=True)) + "\n")
        out.chmod(0o600)
    except OSError:
        return None
    return out


def follow(
    path: Path,
    emit: Callable[[str], None],
    show_results: bool = False,
    poll: float = 1.0,
    is_running: Callable[[], bool] = lambda: True,
) -> None:
    """Print the log so far, then new events as they are written, until the session ends."""
    r = Renderer(show_results)
    with path.open() as fh:
        buffer = ""
        while True:
            chunk = fh.read()
            if chunk:
                buffer += chunk
                *complete, buffer = buffer.split("\n")
                for ev in _events(iter(complete)):
                    for line in r.event(ev):
                        emit(line)
                    if ev.get("type") == "result":
                        return
            elif not is_running():
                return
            else:
                time.sleep(poll)
