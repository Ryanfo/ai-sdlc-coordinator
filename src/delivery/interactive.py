"""Interactive Claude sessions in tmux: watch the work, type to Claude, keep it open afterwards.

With ``[claude.interactive] enabled`` each procedure runs as a normal interactive ``claude``
session (the full terminal interface) instead of ``claude -p``. The coordinator still owns it:

* it starts the session in its private tmux server (delivery.tmux) with the same restrictions
  as print mode (--restricted, the generated --settings profile, --tools, --plugin-dir, the
  permission mode, a sanitised environment via ``env -i``) and opens a terminal window on it;
* hooks added to the generated settings (delivery.session_hook) record what happens and, on
  Stop, check the result file; until it is valid Claude is told to carry on. This replaces
  ``--json-schema``: the coordinator reads the same schema-checked result, from a file;
* the session transcript is mirrored into the run's log (``claude-<procedure>.jsonl``) as it is
  written, so `delivery logs --follow`, the loop and stall guardrails and the finish summary
  work unchanged;
* the proof that the delivery plugin ran is the skill's own "Base directory for this skill"
  line in the transcript, pointing into the configured plugin directory.

Once the result is valid the session is handed off. With ``keep_open`` it stays open for
questions and follow-up changes (delivery.open_sessions); otherwise it is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from delivery.claude import (
    _AUTH_HINTS,
    _USAGE_HINTS,
    PLUGIN_NAME,
    ChildHandle,
    ClaudeInvocation,
    ClaudeOutcome,
    ClaudeStatus,
    OpenSession,
    unavailable_text,
    worker_env,
)
from delivery.config import InteractiveConfig
from delivery.journal import atomic_write_json, ensure_private_dir
from delivery.proc import base_child_env, run_process
from delivery.session_hook import (
    EVENT_NAMES,
    EVENTS,
    EXPECT,
    HANDED_OFF,
    MAX_BLOCKS,
    api_error_text,
    read_events,
)
from delivery.tmux import TmuxError, session_name
from delivery.tmux import for_config as tmux_for

RESULT_FILE = "result.json"
POLL_SECONDS = 1.0
# No prompt this long after launch: Claude is stuck before it could start the procedure.
STARTUP_SECONDS = 90.0
# Claude Code asks this in every git repository or worktree it has not been told to trust
# (trusting a parent folder does not cover them) and runs nothing until it is answered. Print
# mode never asks. The coordinator answers it for its own worktrees only: with --restricted
# the repository's .claude settings are ignored either way, so trusting adds nothing else.
TRUST_QUESTION = "trust this folder"
TRUST_YES = "Yes, I trust this folder"
# An API error ends the turn; if nothing follows it for this long the session has failed.
API_ERROR_QUIET_SECONDS = 20.0
SKILL_BASE = "Base directory for this skill:"
_DENIED = re.compile(r"(permission to use .* (has been )?denied|denied by|blocked by hook)", re.I)
_UNKNOWN_SKILL = re.compile(rf"^Unknown (skill|command): /{PLUGIN_NAME}:")


def hook_settings(session_dir: Path) -> dict[str, Any]:
    """Hooks that report this session's events to the coordinator (see delivery.session_hook)."""

    def command(event: str) -> str:
        return " ".join(
            shlex.quote(a) for a in (sys.executable, "-m", "delivery.session_hook", event, str(session_dir))
        )

    return {
        name: [{"hooks": [{"type": "command", "command": command(short), "timeout": 60}]}]
        for name, short in EVENT_NAMES.items()
    }


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(
            str(c.get("text") or c.get("content") or "") if isinstance(c, dict) else str(c) for c in content
        )
    return str(content or "")


class Mirror:
    """Copies the session transcript into the run log as it grows, and reads facts from it."""

    def __init__(self, log: Path) -> None:
        self.log = log
        self.transcript: Path | None = None
        self.offset = 0
        self.buffer = ""
        self.entries: list[dict[str, Any]] = []
        self.count = 0
        self.last_growth = time.monotonic()
        log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)

    def write(self, ev: dict[str, Any]) -> None:
        with self.log.open("a") as fh:
            fh.write(json.dumps(ev) + "\n")

    def attach(self, transcript: str | None) -> None:
        if self.transcript is None and transcript:
            self.transcript = Path(transcript)

    def pump(self) -> None:
        if self.transcript is None:
            return
        try:
            with self.transcript.open() as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        self.last_growth = time.monotonic()
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split("\n")
        with self.log.open("a") as fh:
            for line in lines:
                if not line.strip():
                    continue
                fh.write(line + "\n")
                self.count += 1
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    self.entries.append(ev)

    # ------------------------------------------------------------------ facts
    def _user_texts(self) -> list[tuple[dict[str, Any], str]]:
        return [
            (e, _text((e.get("message") or {}).get("content", e.get("content"))))
            for e in self.entries
            if e.get("type") in ("user", "system")
        ]

    def plugin_ran(self, plugin_dir: Path, procedure: str) -> bool:
        want = (plugin_dir / "skills" / procedure).resolve()
        for _, text in self._user_texts():
            if text.startswith(SKILL_BASE):
                first = text[len(SKILL_BASE) :].strip().splitlines()[0].strip() if text.strip() else ""
                try:
                    if Path(first).resolve() == want:
                        return True
                except OSError:
                    continue
        return False

    def unknown_skill(self) -> str | None:
        """Claude Code's own notice that the /delivery:<procedure> command does not exist."""
        for e, text in self._user_texts():
            if e.get("type") == "system" and _UNKNOWN_SKILL.match(text.strip()):
                return text.strip()[:300]
        return None

    def denials(self) -> list[dict[str, Any]]:
        calls: dict[str, dict[str, Any]] = {}
        out = []
        for e in self.entries:
            for c in (e.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if e.get("type") == "assistant" and c.get("type") == "tool_use":
                    calls[str(c.get("id"))] = c
                elif (
                    c.get("type") == "tool_result"
                    and c.get("is_error")
                    and _DENIED.search(_text(c.get("content")))
                ):
                    call = calls.get(str(c.get("tool_use_id")), {})
                    out.append(
                        {
                            "tool_name": call.get("name"),
                            "tool_use_id": c.get("tool_use_id"),
                            "tool_input": call.get("input") or {},
                        }
                    )
        return out

    def turns(self) -> int:
        ids = {
            (e.get("message") or {}).get("id") or e.get("uuid")
            for e in self.entries
            if e.get("type") == "assistant"
        }
        return len(ids)

    def trailing_api_error(self) -> str | None:
        last = None
        for e in self.entries:
            if e.get("type") == "assistant":
                last = api_error_text(e)
        return last


def discard_transcript(path: str | None) -> None:
    """Delete Claude Code's own copy of a session transcript once the run log has it.

    Print mode never saves sessions (--no-session-persistence); interactive sessions always
    do, under ~/.claude/projects. Only that exact file is removed.
    """
    if not path:
        return
    p = Path(path)
    if p.suffix == ".jsonl" and p.parent.parent == Path.home() / ".claude" / "projects":
        p.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            p.parent.rmdir()  # each run's worktree gets its own folder; drop it once empty


def _api_status(text: str) -> ClaudeStatus:
    if _AUTH_HINTS.search(text):
        return ClaudeStatus.AUTH
    if _USAGE_HINTS.search(text):
        return ClaudeStatus.USAGE_LIMIT
    if unavailable_text(text):
        return ClaudeStatus.UNAVAILABLE
    return ClaudeStatus.ERROR


def _api_outcome(text: str) -> ClaudeOutcome:
    status = _api_status(text)
    detail = {
        ClaudeStatus.AUTH: "Claude Code login missing or expired",
        ClaudeStatus.USAGE_LIMIT: "Claude subscription usage limit reached; no paid fallback is used",
        ClaudeStatus.UNAVAILABLE: f"Claude's API was unavailable: {text[:300]}",
    }.get(status, f"API error: {text[:300]}")
    return ClaudeOutcome(status, detail)


async def open_window(app: str, folder: Path, title: str, attach: list[str]) -> str | None:
    """Open a terminal window attached to the session (macOS). Returns a problem, if any."""
    if sys.platform != "darwin":
        return "terminal windows are opened on macOS only"
    script = folder / "attach.command"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '\\033]0;%s\\007' {shlex.quote(title)}\n"
        f"exec {' '.join(shlex.quote(a) for a in attach)}\n"
    )
    script.chmod(0o700)
    res = await run_process(["open", "-a", app, str(script)], cwd=folder, env=base_child_env(), timeout=20)
    if res.returncode != 0:
        return (res.stderr or res.stdout).strip()[:300] or f"open exited {res.returncode}"
    return None


class InteractiveRunner:
    """Same interface as :class:`delivery.claude.ClaudeRunner`, backed by tmux."""

    def __init__(
        self,
        executable: str,
        cfg: InteractiveConfig,
        state_dir: Path,
        opener: Callable[[str, Path, str, list[str]], Any] | None = open_window,
        *,
        worktree_root: Path | None = None,
    ) -> None:
        self.executable = executable
        self.cfg = cfg
        self.tmux = tmux_for(cfg, state_dir)
        self.opener = opener if cfg.window != "none" else None
        # Folders whose trust question the coordinator answers (its managed worktrees).
        self.worktree_root = worktree_root

    def _ours(self, cwd: Path) -> bool:
        if self.worktree_root is None:
            return False
        root = self.worktree_root.expanduser().resolve()
        return root in cwd.resolve().parents

    async def _answer_trust(self, name: str) -> bool:
        """Choose "Yes, I trust this folder" (the highlighted option starts as "No, exit")."""
        for _ in range(4):
            screen = await self.tmux.capture(name)
            chosen = [ln for ln in screen.splitlines() if "❯" in ln and TRUST_YES in ln]
            if chosen:
                await self.tmux.send_keys(name, "Enter")
                return True
            if TRUST_QUESTION not in screen:
                return False
            await self.tmux.send_keys(name, "Down")
            await asyncio.sleep(0.3)
        return False

    def prompt(self, inv: ClaudeInvocation) -> str:
        return (
            f"{inv.prompt()}\n\nThis is an interactive session. When the procedure is complete, use "
            f"the Write tool to write the structured result as one JSON object to {inv.result_path}; "
            f"it must match the schema in {inv.schema_path}. The coordinator reads the result from "
            "that file." + (f"\n\n{inv.closing}" if inv.closing else "")
        )

    def argv(self, inv: ClaudeInvocation, title: str) -> list[str]:
        # The prompt comes first: --add-dir takes several values and would swallow it.
        argv = [
            self.executable,
            self.prompt(inv),
            "--plugin-dir", str(inv.plugin_dir),
            "--restricted",
            "--settings", str(inv.settings_path),
            "--strict-mcp-config",
            "--permission-mode", inv.permission_mode,
            "--tools", ",".join(inv.tools),
            "--session-id", inv.session_id,
            "--name", title,
        ]  # fmt: skip
        for d in inv.add_dirs:
            argv += ["--add-dir", str(d)]
        if inv.model:
            argv += ["--model", inv.model]
        return argv

    def _add_hooks(self, settings_path: Path, session_dir: Path) -> None:
        settings = json.loads(settings_path.read_text())
        settings["hooks"] = hook_settings(session_dir)
        atomic_write_json(settings_path, settings)

    async def _free_name(self, base: str) -> str:
        if not await self.tmux.alive(base):
            return base
        return f"{base}-{uuid.uuid4().hex[:4]}"

    async def run(
        self,
        inv: ClaudeInvocation,
        on_start: Callable[[ChildHandle], None] | None = None,
    ) -> ClaudeOutcome:
        if inv.session_dir is None or inv.result_path is None or inv.schema_path is None:
            raise ValueError("interactive sessions need session_dir, result_path and schema_path")
        if not self.tmux.available():
            return ClaudeOutcome(ClaudeStatus.START_FAILED, f"{self.cfg.tmux!r} not found; install tmux")
        sdir = ensure_private_dir(inv.session_dir)
        for stale in (EVENTS, HANDED_OFF):
            (sdir / stale).unlink(missing_ok=True)
        inv.result_path.unlink(missing_ok=True)
        atomic_write_json(
            sdir / EXPECT,
            {
                **inv.expect,
                "result_path": str(inv.result_path),
                "schema_path": str(inv.schema_path),
                "max_blocks": MAX_BLOCKS,
            },
        )
        self._add_hooks(inv.settings_path, sdir)
        title = f"{inv.ticket} {inv.procedure}".strip()
        name = await self._free_name(session_name(inv.ticket or inv.run_id, inv.procedure))
        env = {**worker_env(inv.extra_env), "TERM": "screen-256color", "COLORTERM": "truecolor"}
        mirror = Mirror(inv.stdout_path)
        mirror.write(
            {
                "type": "system",
                "subtype": "init",
                "session_id": inv.session_id,
                "model": inv.model or "default",
                "cwd": str(inv.cwd),
                "interactive": True,
                "tmux_session": name,
            }
        )
        started = time.monotonic()
        try:
            pid = await self.tmux.start(name, inv.cwd, self.argv(inv, title), env)
        except TmuxError as exc:
            return ClaudeOutcome(ClaudeStatus.START_FAILED, str(exc))

        def human_active() -> bool:
            return inv.human_present or len([e for e in read_events(sdir) if e.get("event") == "prompt"]) > 1

        if on_start:
            on_start(ChildHandle(pid, lambda: self.tmux.kill(name), human_active))
        if self.opener is not None:
            problem = await self.opener(self.cfg.window, sdir, title, self.tmux.attach_argv(name))
            if problem:
                (sdir / "window.log").write_text(problem + "\n")
        try:
            outcome = await self._wait(name, inv, sdir, mirror, started)
        except asyncio.CancelledError:
            await asyncio.shield(self.tmux.kill(name))
            mirror.pump()
            discard_transcript(str(mirror.transcript) if mirror.transcript else None)
            raise
        outcome.duration = time.monotonic() - started
        outcome.session_id = inv.session_id
        outcome.tools = list(inv.tools)
        outcome.permission_denials = mirror.denials()
        outcome.num_turns = mirror.turns()
        prompts = [str(e.get("prompt", "")) for e in read_events(sdir) if e.get("event") == "prompt"][1:]
        if outcome.status is ClaudeStatus.OK and self.cfg.keep_open and await self.tmux.alive(name):
            (sdir / HANDED_OFF).touch()
            outcome.open_session = OpenSession(
                name, sdir, str(mirror.transcript or ""), mirror.count, prompts
            )
        else:
            await self.tmux.kill(name)
            mirror.pump()
            discard_transcript(str(mirror.transcript) if mirror.transcript else None)
        mirror.write(
            {
                "type": "result",
                "subtype": "success"
                if outcome.status is ClaudeStatus.OK
                else f"error_{outcome.status.value}",
                "is_error": outcome.status is not ClaudeStatus.OK,
                "num_turns": outcome.num_turns,
                "duration_ms": int(outcome.duration * 1000),
                "permission_denials": outcome.permission_denials,
                "session_id": inv.session_id,
                "interactive": True,
                "kept_open": outcome.open_session is not None,
            }
        )
        return outcome

    async def _wait(
        self, name: str, inv: ClaudeInvocation, sdir: Path, mirror: Mirror, started: float
    ) -> ClaudeOutcome:
        assert inv.result_path is not None
        trust_answers = 0
        while True:
            await asyncio.sleep(POLL_SECONDS)
            events = read_events(sdir)
            for ev in events:
                mirror.attach(ev.get("transcript_path"))
            mirror.pump()
            now = time.monotonic()
            stops = [e for e in events if e.get("event") == "stop" and e.get("result")]
            last = stops[-1] if stops else None
            if last and last["result"] == "valid":
                return self._result(inv, mirror)
            if last and last["result"] == "missing":
                return ClaudeOutcome(
                    ClaudeStatus.MALFORMED,
                    f"Claude stopped {MAX_BLOCKS + 1} times without a valid result: {last.get('detail')}",
                )
            if last and last["result"] == "api_error":
                return _api_outcome(str(last.get("detail", "")))
            if (unknown := mirror.unknown_skill()) is not None:
                return ClaudeOutcome(ClaudeStatus.PLUGIN_MISSING, f"delivery plugin did not load: {unknown}")
            if not await self.tmux.alive(name):
                mirror.pump()
                if (err := mirror.trailing_api_error()) is not None:
                    return _api_outcome(err)
                return ClaudeOutcome(ClaudeStatus.ERROR, "the session ended before handing over a result")
            if not any(e.get("event") == "prompt" for e in events) and now - started > 2:
                screen = await self.tmux.capture(name)
                if TRUST_QUESTION in screen:
                    if not self._ours(inv.cwd) or trust_answers >= 2:
                        return ClaudeOutcome(
                            ClaudeStatus.START_FAILED,
                            f"Claude Code asked whether to trust {inv.cwd}, which is not a worktree "
                            "the coordinator manages (or answering did not work)",
                        )
                    trust_answers += 1
                    await self._answer_trust(name)
                    (sdir / "trusted").write_text(f"{inv.cwd}\n")
                    continue
                if now - started > STARTUP_SECONDS:
                    flat = " | ".join(ln.strip() for ln in screen.splitlines() if ln.strip())
                    return ClaudeOutcome(
                        ClaudeStatus.START_FAILED,
                        f"Claude did not start the procedure. Screen: {flat[-400:]}",
                    )
            if (err := mirror.trailing_api_error()) is not None and now - mirror.last_growth > (
                API_ERROR_QUIET_SECONDS
            ):
                return _api_outcome(err)
            if now - started > inv.timeout:
                return ClaudeOutcome(ClaudeStatus.TIMEOUT, f"timed out after {inv.timeout:.0f}s")

    def _result(self, inv: ClaudeInvocation, mirror: Mirror) -> ClaudeOutcome:
        assert inv.result_path is not None
        try:
            raw = json.loads(inv.result_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return ClaudeOutcome(ClaudeStatus.MALFORMED, f"result file unreadable: {exc}")
        out = ClaudeOutcome(ClaudeStatus.OK, structured=raw if isinstance(raw, dict) else None)
        if mirror.plugin_ran(inv.plugin_dir, inv.procedure):
            out.plugins = [PLUGIN_NAME]
        else:
            out.status = ClaudeStatus.PLUGIN_MISSING
            out.detail = "the transcript does not show the delivery skill loading from the plugin directory"
        return out
