"""Plain explanation of a ticket's latest run for `delivery inspect`: what happened, why,
where the evidence is on this machine, and what can be done next in Jira."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from delivery.config import Config
from delivery.journal import RunEntry
from delivery.models import CheckResult, RunState
from delivery.workflow import Action

WIDTH = 100
INDENT = "    "

# What each human action does next, for the actions people pick from a waiting status.
ACTION_EFFECTS: dict[Action, str] = {
    Action.SUBMIT_IMPLEMENTATION_CHANGES: "development fixes the pending items as a new candidate",
    Action.SUBMIT_FOLLOW_UP: "verifies the same candidate again (no code change); after ACCEPT "
    "DEVIATIONS from Code review, rewrites the specification instead",
    Action.REVISE_SCOPE: "back to refinement to change what is being built",
    Action.REQUEST_CODE_CHANGES: "needs a CHANGE CODE comment (no items needed when the PR's review "
    "comments say it all); then Submit implementation changes",
    Action.REQUEST_ACCEPTANCE_CHANGES: "needs a CHANGE ACCEPTANCE comment",
    Action.APPROVE_CODE: "needs APPROVE CODE and an independent GitHub approval at the current head",
    Action.ACCEPT_DELIVERY: "needs an ACCEPT DELIVERY comment after code approval",
    Action.CANCEL: "stops all work on the ticket",
    Action.USE_APPROVED_PLAN: "the coordinator's: a fast-track plan approved with the specification",
    Action.COMPLETE_SPIKE: "the coordinator's: closes a spike whose findings were accepted",
}

STATE_WORDS = {
    RunState.AWAITING_HUMAN: "waiting for you",
    RunState.BLOCKED: "blocked",
    RunState.FAILED: "failed",
    RunState.INTERRUPTED: "stopped",
    RunState.COMPLETED: "completed",
}


def _wrap(text: str, indent: str = INDENT) -> list[str]:
    return textwrap.wrap(
        " ".join(text.split()),
        WIDTH,
        initial_indent=indent,
        subsequent_indent=indent + "  ",
        break_long_words=False,
        break_on_hyphens=False,
    ) or [indent]


def _section(title: str, body: list[str]) -> list[str]:
    return [f"  {title}", *body] if body else []


def _checks(checks: list[CheckResult], run_dir: Path) -> list[str]:
    if not checks:
        return []
    failed = [c for c in checks if c.conclusion != "passed"]
    lines = [f"{INDENT}{len(checks) - len(failed)} of {len(checks)} passed"]
    for c in failed:
        where = c.url or c.log_path or ""
        lines += _wrap(f"{c.name} ({c.source}/{c.target}): {c.conclusion}  {where}")
        if c.log_path and Path(c.log_path).is_file():
            err = Path(c.log_path).with_suffix(".err.log")
            tail = _tail(err if err.is_file() and err.stat().st_size else Path(c.log_path))
            lines += [f"{INDENT}  | {t}" for t in tail]
    return lines


def _tail(path: Path, n: int = 8) -> list[str]:
    try:
        text = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [t[: WIDTH - 8] for t in text if t.strip()][-n:]


def latest_outcome(cfg: Config, entry: RunEntry) -> list[str]:
    """The latest run of a ticket explained: outcome, reasons, findings, evidence locations."""
    rec = entry.record
    run_dir = entry.journal.dir
    if rec is None:
        return [f"Latest run {entry.run_id}: its record is unreadable ({entry.error})."]
    state = STATE_WORDS.get(rec.state, rec.state.value)
    out = [f"Latest run {rec.run_id}  ({rec.stage.value}, {state})"]
    decision: dict[str, Any] = rec.outputs.get("decision") or {}
    extra: dict[str, Any] = decision.get("extra") or {}
    if decision.get("outcome"):
        out += _section("Outcome", _wrap(decision["outcome"].replace("_", " ")))
    problems = list(extra.get("problems") or [])
    why = [f"R{i}: {p}" for i, p in enumerate(problems, 1)] if problems else []
    if not why and rec.reason:
        why = [rec.reason]
    out += _section("Why", [ln for w in why for ln in _wrap(w)])
    conflicts = list(extra.get("merge_conflicts") or rec.outputs.get("merge_conflicts") or [])
    if conflicts:
        body: list[str] = []
        cand = str(extra.get("candidate") or rec.candidate_sha or "")
        repo = cfg.repository.checkout_path
        base = cfg.repository.base_branch
        for c in conflicts:
            other = f"the latest {base}" if c["with"] == base else f"{c['with']}'s unmerged candidate"
            body += _wrap(f"{other} ({str(c['sha'])[:12]}): {', '.join(c['paths'])}")
            if cand:
                body += _wrap(
                    f"see it: git -C {repo} fetch origin && git -C {repo} merge-tree "
                    f"--write-tree --name-only {cand[:12]} {str(c['sha'])[:12]}",
                    INDENT + "  ",
                )
        body += _wrap("Flagged, not a failure: resolve when merging the pull request.")
        out += _section("Merge conflicts", body)
    checks = [CheckResult.model_validate(c) for c in extra.get("ci", [])]
    out += _section("Checks", _checks(list(rec.checks) + checks, run_dir))
    findings = list(extra.get("findings") or [])
    if findings:
        body = []
        for f in findings:
            who = "verifier" if int(str(f["id"])[1:]) >= 100 else "reviewer"
            body += _wrap(f"{f['id']} ({f['severity']}, {who}): {f['description']}")
        out += _section("Findings", body)
    found = list(extra.get("deviations") or [])
    if found:
        body = []
        for d in found:
            asked = "asked for" if d.get("requested") else "not asked for"
            body += _wrap(f"{d['id']} ({asked}): {d['description']}")
        body += _wrap(
            "Not a failure: accept (ACCEPT DEVIATIONS) or change back (a D-item in the change request)."
        )
        out += _section("Deviations from the specification", body)
    amendment = rec.outputs.get("amendment")
    if amendment:
        out += _section(
            "Specification",
            _wrap(
                f"v{int(amendment['revision']):03d} rewritten to include the accepted deviations "
                f"{', '.join(amendment.get('accepted', []))}"
            ),
        )
    out += _section("Next", _wrap(rec.next_action) if rec.next_action else [])
    logs = sorted((run_dir / "logs").glob("claude-*.txt")) if (run_dir / "logs").is_dir() else []
    files = [
        f"{INDENT}run folder     {run_dir}",
        f"{INDENT}Claude logs    delivery logs {rec.ticket_key} --run {rec.run_id}",
        *[f"{INDENT}               {p}" for p in logs],
    ]
    if (run_dir / "logs").is_dir() and any((run_dir / "logs").glob("*-*.log")):
        files.append(f"{INDENT}check output   {run_dir / 'logs'}/<target>-<check>.log")
    if (run_dir / "inputs").is_dir():
        files.append(f"{INDENT}Claude inputs  {run_dir / 'inputs'}")
    out += _section("Evidence", files)
    if rec.state in (RunState.BLOCKED, RunState.FAILED):
        from delivery.console import log_tail

        tail = log_tail(run_dir / "logs")
        out += _section("Last steps", [f"{INDENT}{t}" for t in tail])
    return out


def jira_actions(cfg: Config, transitions: list[tuple[str, str]]) -> list[str]:
    """The transitions Jira offers now, each with what it leads to."""
    if not transitions:
        return []
    by_name = {cfg.workflow.action_name(a): a for a in Action}
    lines = ["In Jira you can choose now:"]
    for name, target in transitions:
        action = by_name.get(name)
        effect = ACTION_EFFECTS.get(action) if action else None
        lines += _wrap(f"{name} -> {target}" + (f": {effect}" if effect else ""), "  ")
    return lines
