"""Terminal output for the running supervisor: clear blocks for session start and finish."""

from __future__ import annotations

import textwrap
from datetime import datetime

from delivery.comments import STAGE_TITLES
from delivery.config import Config
from delivery.models import RunRecord, RunState, utcnow
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


def block(title: str, rows: list[tuple[str, str]]) -> str:
    out = ["", RULE, f" {title}".ljust(WIDTH - 9) + f"{now():>9}", THIN]
    for label, value in rows:
        if not value:
            continue
        wrapped = textwrap.wrap(value, WIDTH - LABEL - 2, break_long_words=False, break_on_hyphens=False)
        for i, part in enumerate(wrapped or [""]):
            out.append(f" {(label if i == 0 else ''):<{LABEL}}{part}")
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
    return block(
        f"{verb}  {_stage_title(record.stage)}  {record.ticket_key}",
        [
            ("Ticket", f"{record.ticket_key}  {summary}"),
            ("Status", status),
            ("Claude", f"{', '.join(sd.procedures)} on {_models(cfg, record.stage)}"),
            ("Run", record.run_id),
            ("Jira", ticket_url(cfg, record.ticket_key)),
        ],
    )


def session_finished(cfg: Config, record: RunRecord, summary: str) -> str:
    outcome = OUTCOMES.get(record.state, record.state.value.upper())
    took = ""
    if record.started_at:
        secs = int((utcnow() - record.started_at).total_seconds())
        took = f"{secs // 60}m {secs % 60:02d}s"
    return block(
        f"FINISHED  {_stage_title(record.stage)}  {record.ticket_key}  -  {outcome}",
        [
            ("Ticket", f"{record.ticket_key}  {summary}"),
            ("Result", record.reason[:400]),
            ("Next", record.next_action),
            ("Took", took),
            ("Run", record.run_id),
            ("Jira", ticket_url(cfg, record.ticket_key)),
        ],
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
            ("Stop", "Ctrl-C (running sessions are saved and resume on restart)"),
        ],
    )
