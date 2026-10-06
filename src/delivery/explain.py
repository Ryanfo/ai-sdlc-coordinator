"""Plain explanation of a ticket's latest run for `delivery inspect`: what happened, why,
where the evidence is on this machine, and what can be done next in Jira."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from delivery.config import Config
from delivery.journal import JournalStore, RunEntry
from delivery.models import CheckResult, RunState
from delivery.workflow import Action

if TYPE_CHECKING:
    from delivery.intake import TicketContext
    from delivery.ports import GitHubPort, JiraPort

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
    RunState.CANCELLED: "cancelled",
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


@dataclass
class Inspection:
    """What `delivery inspect` found: data for --json, lines for people, and the ticket context."""

    data: dict[str, Any]
    lines: list[str]
    ctx: TicketContext


async def inspect_ticket(cfg: Config, jira: JiraPort, github: GitHubPort, ticket: str) -> Inspection:
    """Explain one ticket without changing anything: Jira status, eligibility, what intake waits
    for, gates, the latest run here and the Jira actions available now."""
    from delivery.intake import IntakeEvaluator, load_context
    from delivery.ownership import evaluate_eligibility

    ctx = await load_context(jira, cfg, ticket)
    elig = evaluate_eligibility(ctx.issue.view, cfg)
    intake = await IntakeEvaluator(cfg, github).evaluate(ctx, elig.stage) if elig.stage else None
    transitions = [(t.name, t.to_status_name) for t in await jira.transitions(ticket)]
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    entries = store.runs_for_ticket(ticket)
    runs = [
        {
            "run_id": e.run_id,
            "state": e.record.state.value if e.record else "CORRUPT",
            "stage": e.record.stage.value if e.record else None,
            "reason": e.record.reason if e.record else (e.error.detail if e.error else ""),
            "pending_ops": [o.op_type for o in e.journal.pending_ops()] if e.record else [],
            "dir": str(e.journal.dir),
        }
        for e in entries
    ]
    rec = ctx.record
    data = {
        "ticket": ticket,
        "status": ctx.status.value if ctx.status else ctx.issue.view.status_name,
        "assignee": ctx.issue.view.assignee_account_id,
        "eligible": elig.eligible,
        "eligibility_reasons": list(elig.reasons),
        "intake": intake.persisted() if intake else None,
        "gates": [g.model_dump(mode="json") for g in rec.gates],
        "pause": rec.pause.model_dump(mode="json") if rec.pause else None,
        "candidate": rec.candidate_sha,
        "pr": rec.pr_number,
        "artefacts": rec.artefacts,
        "footprint": rec.footprint_ref,
        "overlap_warnings": rec.overlap_warnings,
        "overlap_decisions": rec.overlap_decisions,
        "release": rec.release,
        "pending_feedback": rec.pending_feedback,
        "jira_actions": [{"name": n, "to": t} for n, t in transitions],
        "local_runs": runs,
    }
    lines = [
        f"{ticket}: {data['status']} (assignee {data['assignee']})",
        f"eligible: {elig.eligible}" + (f" ({'; '.join(elig.reasons)})" if elig.reasons else ""),
    ]
    if intake:
        lines.append(
            f"intake: {intake.kind.value} - {intake.reason}"
            + (f" -> {intake.next_action}" if intake.next_action else "")
        )
    lines.append(
        "gates: " + ", ".join(f"{g.token}={g.state.value}" for g in rec.gates) if rec.gates else "gates: none"
    )
    if rec.pause:
        lines.append(
            f"paused: {rec.pause.kind} resume={rec.pause.resume_stage.value} "
            f"{rec.pause.round_token or rec.pause.reason}"
        )
    lines.append(f"candidate: {rec.candidate_sha or '-'} PR #{rec.pr_number or '-'}")
    lines.append(f"overlap warnings: {', '.join(rec.overlap_warnings) or 'none'}")
    if entries:
        lines += ["", *latest_outcome(cfg, entries[-1])]
    actions = jira_actions(cfg, transitions)
    if actions:
        lines += ["", *actions]
    if runs:
        lines += ["", "Runs on this machine (oldest first):"]
        lines += [
            f"  {r['run_id']}  {r['state']}" + (f"  pending {r['pending_ops']}" if r["pending_ops"] else "")
            for r in runs
        ]
    lines += ["", f"Ask Claude what is wrong and how to fix it: coordinator help {ticket}"]
    return Inspection(data, lines, ctx)
