"""The office: an animated, read-only view of the coordinator at work (`coordinator office`).

The page (``data/office.html``) shows a pixel-art paper-company office where the coordinator is
the regional manager and every stage has its own employee. Tickets are folders carried between
desks, the conference room (waiting for you) and HR (blocked or failed).

This module only reads the local journal under ``runtime.state_dir``; it never talks to Jira,
GitHub or Claude, and it serves on 127.0.0.1 only. Raw journal events are normalised into a
small set of *beats* the page knows how to animate, and nothing else leaves the journal: no
comment bodies, paths, digests or account IDs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PAGE = Path(__file__).parent / "data" / "office.html"

# Run states the page animates when a run enters them.
_STATE_BEATS = {
    "running": "start",
    "publishing": "publishing",
    "awaiting_human": "await",
    "completed": "done",
    "blocked": "blocked",
    "failed": "failed",
    "interrupted": "interrupted",
    "cancelled": "cancelled",
}
# Events that are worth a beat of their own (the rest are bookkeeping).
_EVENT_BEATS = {
    "created": "arrive",
    "coordinator_checks": "checks",
    "release_checks": "checks",
    "candidate_published": "candidate",
    "session_kept_open": "open",
}
_OP_BEATS = {
    "jira_transition": "jira",
    "jira_comment": "comment",
    "git_push": "push",
}
_SUPERVISOR_BEATS = {
    "supervisor_started": "boss_in",
    "supervisor_stopped": "boss_out",
    "claude_unavailable": "break",
    "claude_available": "back",
}


def _read_complete_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """New whole JSON lines after ``offset``; a line still being written waits for next time."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read()
    except OSError:
        return [], offset
    end = raw.rfind(b"\n")
    if end < 0:
        return [], offset
    events = []
    for line in raw[: end + 1].splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and "type" in ev:
            events.append(ev)
    return events, offset + end + 1


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class _Run:
    ticket: str
    run_id: str
    stage: str = ""
    offset: int = 0
    state: str = ""


@dataclass
class OfficeFeed:
    """Tails the journal and turns it into beats: ``{seq, at, kind, ticket, run, stage, ...}``."""

    state_dir: Path
    identity_key: str
    beats: list[dict[str, Any]] = field(default_factory=list)
    _runs: dict[Path, _Run] = field(default_factory=dict)
    _supervisor_offset: int = 0
    _titles: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def supervisor_dir(self) -> Path:
        return self.state_dir / "supervisor" / self.identity_key

    def poll(self) -> list[dict[str, Any]]:
        """Read whatever is new since the last poll; returns (and keeps) the new beats."""
        fresh: list[dict[str, Any]] = []
        for rdir in self._run_dirs():
            run = self._runs.get(rdir)
            if run is None:
                run = self._runs[rdir] = _Run(rdir.parent.name, rdir.name)
            events, run.offset = _read_complete_lines(rdir / "events.jsonl", run.offset)
            for ev in events:
                fresh.extend(self._run_beats(run, rdir, ev))
        events, self._supervisor_offset = _read_complete_lines(
            self.supervisor_dir / "events.jsonl", self._supervisor_offset
        )
        for ev in events:
            kind = _SUPERVISOR_BEATS.get(ev["type"])
            if kind:
                fresh.append({"at": ev.get("at", ""), "kind": kind})
        fresh.sort(key=lambda b: b["at"])
        for b in fresh:
            b["seq"] = len(self.beats) + 1
            self.beats.append(b)
        return fresh

    def _run_dirs(self) -> list[Path]:
        if not self.runs_dir.is_dir():
            return []
        out: list[Path] = []
        for tdir in sorted(self.runs_dir.iterdir()):
            if tdir.is_dir():
                out.extend(r for r in sorted(tdir.iterdir()) if (r / "events.jsonl").exists())
        return out

    def _run_beats(self, run: _Run, rdir: Path, ev: dict[str, Any]) -> list[dict[str, Any]]:
        data = ev.get("data") or {}
        if not run.stage:
            run.stage = str(data.get("stage") or _load_json(rdir / "snapshot.json").get("stage", ""))
            self._remember_title(run.ticket, rdir)
        base = {"at": ev.get("at", ""), "ticket": run.ticket, "run": run.run_id, "stage": run.stage}
        out: list[dict[str, Any]] = []
        etype = ev["type"]
        if etype in _EVENT_BEATS:
            out.append({**base, "kind": _EVENT_BEATS[etype]})
        elif etype == "op_intent":
            op = data.get("op_type", "")
            kind = _OP_BEATS.get(op)
            if kind == "jira":
                out.append({**base, "kind": kind, "to": str((data.get("detail") or {}).get("to", ""))})
            elif kind:
                out.append({**base, "kind": kind})
        elif etype == "op_result" and "number" in (data.get("result") or {}):
            # Only a PR result carries a number; the intent alone would announce it twice.
            out.append({**base, "kind": "pr", "number": data["result"]["number"]})
        state = data.get("state")
        if isinstance(state, str) and state != run.state:
            run.state = state
            kind = _STATE_BEATS.get(state)
            if kind:
                out.append({**base, "kind": kind})
        return out

    def _remember_title(self, ticket: str, rdir: Path) -> None:
        if ticket in self._titles:
            return
        for env in sorted((rdir / "inputs").glob("envelope-*.json")):
            brief = _load_json(env).get("brief") or {}
            if brief.get("summary"):
                self._titles[ticket] = {
                    "title": str(brief["summary"])[:120],
                    "type": str(brief.get("issue_type", "")),
                }
                return

    def tickets(self) -> list[dict[str, Any]]:
        """Each ticket's latest run, for the side panel."""
        latest: dict[str, dict[str, Any]] = {}
        for rdir, run in self._runs.items():
            snap = _load_json(rdir / "snapshot.json")
            if not snap:
                continue
            created = str(snap.get("created_at", ""))
            prev = latest.get(run.ticket)
            if prev is None or created >= prev["created_at"]:
                latest[run.ticket] = {
                    "key": run.ticket,
                    "created_at": created,
                    "updated_at": str(snap.get("updated_at", "")),
                    "stage": snap.get("stage", ""),
                    "state": snap.get("state", ""),
                    "pr_url": snap.get("pr_url"),
                    "next_action": str(snap.get("next_action", ""))[:200],
                    **self._titles.get(run.ticket, {}),
                }
        return sorted(latest.values(), key=lambda t: t["updated_at"], reverse=True)

    def supervisor(self) -> dict[str, Any]:
        rec = _load_json(self.supervisor_dir / "supervisor.json")
        pid = rec.get("pid")
        running = isinstance(pid, int) and not rec.get("stopped_at") and _alive(pid)
        return {
            "running": running,
            "dispatch_paused": bool(rec.get("dispatch_paused")),
            "claude_unavailable": bool(rec.get("claude_unavailable")),
        }


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Hub:
    """One feed shared by every browser tab, polled by a background thread."""

    def __init__(self, feed: OfficeFeed, interval: float) -> None:
        self.feed = feed
        self.interval = interval
        self.cond = threading.Condition()
        self.stopped = False
        with self.cond:
            feed.poll()

    def run(self) -> None:
        while not self.stopped:
            time.sleep(self.interval)
            with self.cond:
                if self.feed.poll():
                    self.cond.notify_all()

    def state(self) -> dict[str, Any]:
        with self.cond:
            return {
                "beats": list(self.feed.beats),
                "tickets": self.feed.tickets(),
                "supervisor": self.feed.supervisor(),
            }

    def wait_after(self, seq: int, timeout: float) -> list[dict[str, Any]]:
        with self.cond:
            self.cond.wait_for(lambda: len(self.feed.beats) > seq or self.stopped, timeout)
            return self.feed.beats[seq:]


def _handler(hub: _Hub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, body: bytes, ctype: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._send(json.dumps(hub.state()).encode(), "application/json")
            elif url.path == "/api/events":
                self._stream(int(parse_qs(url.query).get("after", ["0"])[0] or 0))
            else:
                self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)

        def _stream(self, seq: int) -> None:
            """Server-sent events: every new beat, plus a comment every 15s to keep it open."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while not hub.stopped:
                    beats = hub.wait_after(seq, 15)
                    if beats:
                        seq = beats[-1]["seq"]
                        payload = {"beats": beats, "tickets": hub.state()["tickets"]}
                        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                    else:
                        self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


class OfficeServer:
    def __init__(self, state_dir: Path, identity_key: str, port: int = 0, interval: float = 1.0) -> None:
        self.hub = _Hub(OfficeFeed(state_dir, identity_key), interval)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler(self.hub))
        self.httpd.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host!s}:{port}/"

    def serve_forever(self) -> None:
        threading.Thread(target=self.hub.run, daemon=True, name="office-feed").start()
        try:
            self.httpd.serve_forever()
        finally:
            self.close()

    def close(self) -> None:
        with self.hub.cond:
            self.hub.stopped = True
            self.hub.cond.notify_all()
        self.httpd.server_close()
