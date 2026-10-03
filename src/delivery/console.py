"""Terminal output for the running supervisor: clear blocks for session start and finish."""

from __future__ import annotations

import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from delivery.comments import STAGE_TITLES
from delivery.config import Config
from delivery.models import Outcome, RunRecord, RunState, utcnow
from delivery.workflow import STAGES, STATUS_NAMES, Stage

WIDTH = 78
RULE = "=" * WIDTH
THIN = "-" * WIDTH
LABEL = 10

OUTCOMES = {
    RunState.AWAITING_HUMAN: "WAITING FOR YOU",
    RunState.COMPLETED: "COMPLETED",
    RunState.BLOCKED: "BLOCKED",
    RunState.FAILED: "FAILED",
    RunState.INTERRUPTED: "STOPPED",
}


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def line(message: str) -> str:
    """A one-line event, timestamped."""
    return f"[{now()}] {message}"


def ticket_url(cfg: Config, key: str) -> str:
    return f"{cfg.jira.base_url}/browse/{key}"


def block(title: str, rows: list[tuple[str, str]], extra: list[str] | None = None) -> str:
    out = ["", RULE, f" {title}".ljust(WIDTH - 9) + f"{now():>9}", THIN]
    for label, value in rows:
        if not value:
            continue
        wrapped = textwrap.wrap(value, WIDTH - LABEL - 2, break_long_words=False, break_on_hyphens=False)
        for i, part in enumerate(wrapped or [""]):
            out.append(f" {(label if i == 0 else ''):<{LABEL}}{part}")
    if extra:
        out.append(THIN)
        out += [(" " + ln)[:WIDTH] for ln in extra]
    out += [RULE, ""]
    return "\n".join(out)


def _stage_title(stage: Stage) -> str:
    return STAGE_TITLES.get(stage.value, stage.value)


def _models(cfg: Config, stage: Stage) -> str:
    names = {cfg.claude.model_for(p) or "Claude Code default" for p in STAGES[stage].procedures}
    return ", ".join(sorted(names))


def session_started(
    cfg: Config, record: RunRecord, summary: str, *, resumed: bool = False, adopted: bool = False
) -> str:
    sd = STAGES[record.stage]
    verb = "RESUMED" if resumed else "TAKEN OVER" if adopted else "STARTED"
    status = (
        f"{STATUS_NAMES[sd.active]} (moved there by hand; checked as if {STATUS_NAMES[sd.ready]})"
        if adopted
        else f"{STATUS_NAMES[sd.ready]} -> {STATUS_NAMES[sd.active]}"
    )
    interactive = cfg.claude.interactive
    if interactive.enabled:
        window = "a window opens on it; " if interactive.window != "none" else ""
        watch = f"{window}`delivery attach {record.ticket_key}` to watch or type to Claude"
    else:
        watch = f"delivery logs {record.ticket_key} --follow"
    return block(
        f"{verb}  {_stage_title(record.stage)}  {record.ticket_key}",
        [
            ("Ticket", f"{record.ticket_key}  {summary}"),
            ("Status", status),
            ("Claude", f"{', '.join(sd.procedures)} on {_models(cfg, record.stage)}"),
            ("Run", record.run_id),
            ("Jira", ticket_url(cfg, record.ticket_key)),
            ("Watch", watch),
        ],
    )


def follow_up_published(
    cfg: Config,
    key: str,
    n: int,
    sha: str,
    files: int,
    was_in: str,
    asked: list[str],
    *,
    ended: bool = False,
) -> str:
    starts = "start now" if ended else "start once you close the session"
    return block(
        f"FOLLOW-UP  Development  {key}  -  pushed as c{n}",
        [
            ("Asked", "; ".join(asked)[:300]),
            ("Commit", f"{sha[:12]}  ({files} file{'s' if files != 1 else ''} changed)"),
            ("Status", f"{was_in} -> Ready for verification (verification and review {starts})"),
            ("Jira", ticket_url(cfg, key)),
            (
                "Session",
                "closed"
                if ended
                else f"still open: delivery attach {key}; type /exit in it when you have finished",
            ),
        ],
    )


def document_follow_up_published(
    cfg: Config, key: str, stage: Stage, title: str, rev: int, token: str, replaces: str, asked: list[str]
) -> str:
    return block(
        f"FOLLOW-UP  {_stage_title(stage)}  {key}  -  {title} v{rev:03d}",
        [
            ("Asked", "; ".join(asked)[:300]),
            ("Published", f"{title} v{rev:03d} for review ({token}); it supersedes {replaces}"),
            ("Jira", ticket_url(cfg, key)),
            ("Session", f"still open: delivery attach {key}"),
        ],
    )


def preview_ready(cfg: Config, key: str, url: str, worktree: str, log: str) -> str:
    opened = " (opened in your browser)" if cfg.preview.open_browser else ""
    return block(
        f"TRY IT  Development  {key}",
        [
            ("App", f"{url}{opened}"),
            ("From", worktree),
            (
                "Changes",
                f"ask in the open development session (delivery attach {key}); the app shows them as "
                "they are made, and each one is pushed as a new candidate",
            ),
            ("Reopen", f"delivery preview {key}"),
            ("Output", f"delivery attach {key} --procedure preview, or {log}"),
        ],
    )


def preview_trouble(cfg: Config, key: str, what: str, log: str, tail: list[str]) -> str:
    return block(
        f"APP  Development  {key}",
        [
            ("Problem", what),
            ("Output", log),
            ("Next", f"`delivery preview {key}` starts it again; the development session stays open"),
        ],
        tail or None,
    )


def open_session_closed(rec: Any, reason: str, kept: str) -> str:
    text = f"{rec.ticket_key}: closed the open {rec.procedure} session ({reason})"
    return line(text + (f"; {kept}" if kept else ""))


def latest_transcript(log_dir: Path | None) -> Path | None:
    if log_dir is None or not log_dir.is_dir():
        return None
    found = sorted(log_dir.glob("claude-*.txt"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def log_tail(log_dir: Path | None, lines: int = 12) -> list[str]:
    """The last meaningful lines of the most recent Claude session log in a run."""
    from delivery.transcript import render_file

    if log_dir is None or not log_dir.is_dir():
        return []
    logs = sorted(log_dir.glob("claude-*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return []
    try:
        rendered = [ln for ln in render_file(logs[-1], width=WIDTH - 2) if ln.strip()]
    except OSError:
        return []
    return [f"Last steps of {logs[-1].stem.removeprefix('claude-')}:", *rendered[-lines:]]


def session_finished(cfg: Config, record: RunRecord, summary: str, log_dir: Path | None = None) -> str:
    outcome = OUTCOMES.get(record.state, record.state.value.upper())
    took = ""
    if record.started_at:
        secs = int((utcnow() - record.started_at).total_seconds())
        took = f"{secs // 60}m {secs % 60:02d}s"
    trouble = record.state in (RunState.BLOCKED, RunState.FAILED)
    log_file = latest_transcript(log_dir)
    from delivery.open_sessions import SessionRegistry

    still_open = [
        r.procedure for r in SessionRegistry(cfg.runtime.state_dir).all() if r.run_id == record.run_id
    ]
    picked_up = ""
    if cfg.claude.interactive.follow_ups:
        if record.stage is Stage.DEVELOPMENT:
            picked_up = " (changes you ask for are pushed as a new candidate"
            if record.outcome is Outcome.COMPLETED:
                picked_up += "; verification and review start once you type /exit in it"
            picked_up += ")"
        elif record.stage in (Stage.REFINEMENT, Stage.PLANNING, Stage.RELEASE_PREPARATION):
            picked_up = " (changes you ask for are published as the next revision for review)"
    return block(
        f"FINISHED  {_stage_title(record.stage)}  {record.ticket_key}  -  {outcome}",
        [
            ("Ticket", f"{record.ticket_key}  {summary}"),
            ("Result", record.reason[:400]),
            ("Next", record.next_action),
            ("Took", took),
            ("Run", record.run_id),
            ("Jira", ticket_url(cfg, record.ticket_key)),
            ("Logs", f"delivery logs {record.ticket_key}"),
            ("Log file", log_file.as_uri() if log_file else ""),
            (
                "Claude",
                f"{', '.join(still_open)} still open for questions: delivery attach {record.ticket_key}"
                + picked_up
                if still_open
                else "",
            ),
        ],
        log_tail(log_dir) if trouble else None,
    )


def supervisor_started(cfg: Config, version: str) -> str:
    models = {
        p: cfg.claude.model_for(p) or "Claude Code default" for s in STAGES.values() for p in s.procedures
    }
    distinct = sorted(set(models.values()))
    return block(
        f"DELIVERY COORDINATOR {version}  running",
        [
            ("Project", f"{cfg.jira.project_key}  {cfg.jira.base_url}/browse/{cfg.jira.project_key}"),
            ("Watching", f"tickets assigned to you in Ready statuses, every {cfg.runtime.poll_seconds}s"),
            ("Worker", cfg.identity.worker_id),
            (
                "Models",
                distinct[0] if len(distinct) == 1 else ", ".join(f"{p} {m}" for p, m in models.items()),
            ),
            (
                "Sessions",
                "interactive in tmux; `delivery attach <ticket>` to watch or type"
                + ("; left open for questions after the work" if cfg.claude.interactive.keep_open else "")
                if cfg.claude.interactive.enabled
                else "",
            ),
            ("Stop", "Ctrl-C (running sessions are saved and resume on restart)"),
        ],
    )
