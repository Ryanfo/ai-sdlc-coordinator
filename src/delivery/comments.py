"""Jira comment bodies (markdown subset converted to ADF on publication).

Jira holds decisions only, and a decision is a move: a comment that waits for a human opens with
the action to choose and the status it moves the ticket into (taken from the workflow
definition), then links to the artefacts. Nobody is asked to paste a comment to decide.
Results, logs and working context stay in the repository and the coordinator logs; only failures
are named.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from delivery.feedback import GATE_TOKEN, DecisionKind
from delivery.models import CheckResult, DeviationRecord, Finding, ProposedTicket, Question, Severity
from delivery.overlap import OverlapFinding
from delivery.overlap import Severity as OverlapSeverity
from delivery.workflow import (
    DEFAULT_ACTION_NAMES,
    STAGES,
    STATUS_NAMES,
    Action,
    Stage,
    StageDef,
    Status,
    route_for_action,
)

STAGE_TITLES = {
    "refinement": "Refinement",
    "planning": "Planning",
    "development": "Development",
    "verification": "Verification",
    "release_preparation": "Release preparation",
    "resolution": "Resolution",
}


def _into(source: Status, action: Action) -> str:
    """The status a Jira action moves the ticket into, by its display name."""
    route = route_for_action(source, action)
    if route is None:
        raise ValueError(f"the workflow has no {action.value} from {source.value}")
    return STATUS_NAMES[route.target]


def _moves(source: Status, action: Action) -> str:
    return f"(moves into **{_into(source, action)}**)"


def _approve(source: Status, action: Action, what: str = "To approve") -> str:
    return f"**{what}**: choose **{DEFAULT_ACTION_NAMES[action]}** {_moves(source, action)}."


def _change(source: Status, action: Action, what: str = "To request changes") -> str:
    return f"**{what}**: choose **{DEFAULT_ACTION_NAMES[action]}** {_moves(source, action)}."


def _ready_to_move(source: Status, action: Action, when: str) -> str:
    return f"**Ready to move into {_into(source, action)}** {when}."


def _stage_def(stage: str) -> StageDef:
    return STAGES[Stage(stage)]


def _session_line(run_id: str, worker_id: str) -> str:
    return f"Run `{run_id}` on worker `{worker_id}`."


def started(
    stage: str,
    reason: str,
    moved_by_hand: str | None = None,
) -> str:
    text = f"**{STAGE_TITLES.get(stage, stage)} started.** {reason}"
    if moved_by_hand:
        text += f"\nMoved into {moved_by_hand} by hand: leave tickets in Ready, the coordinator moves them."
    return text


def design_drift(stage: str, frames: list[tuple[str, str]]) -> str:
    listed = "\n".join(f"- [{name}]({url})" for name, url in frames)
    return (
        "**To adopt the new Figma design**, choose Revise scope (or request specification changes).\n"
        f"{STAGE_TITLES.get(stage, stage)} keeps building the approved version. Changed frames:\n{listed}"
    )


def proposals_section(token: str, proposals: Sequence[ProposedTicket]) -> list[str]:
    """Tickets Claude proposes (slices or follow-ups): created only when someone asks."""
    if not proposals:
        return []
    lines = [
        "",
        "**To create proposed tickets** (optional): comment this with the IDs.",
        "```",
        f"{DecisionKind.CREATE_TICKETS.value} {token}",
        ", ".join(t.id for t in proposals),
        "```",
    ]
    lines += [f"- **{t.id}** {t.summary}" for t in proposals]
    return lines


def spec_gate(
    token: str,
    url: str,
    revision: int,
    *,
    note: str = "",
    plan: tuple[int, str] | None = None,
    fast_track_note: str = "",
    proposals: Sequence[ProposedTicket] = (),
) -> str:
    lines = [
        f"## Specification v{revision:03d} ready for review",
        _approve(Status.SPECIFICATION_REVIEW, Action.APPROVE_SPECIFICATION),
        _change(Status.SPECIFICATION_REVIEW, Action.REQUEST_SPECIFICATION_CHANGES),
        f"[Specification v{revision:03d}]({url})",
    ]
    if note:
        lines.append(note)
    if plan:
        lines.append(f"Includes [plan v{plan[0]:03d}]({plan[1]}): approving starts development.")
    elif fast_track_note:
        lines.append(f"No fast track: {fast_track_note}.")
    lines += proposals_section(token, proposals)
    return "\n".join(lines)


def findings_gate(
    token: str,
    url: str,
    revision: int,
    proposals: Sequence[ProposedTicket] = (),
    *,
    note: str = "",
) -> str:
    """A spike's findings, reviewed in Plan review. Accepting them completes the spike."""
    lines = [
        f"## Findings v{revision:03d} ready for review",
        _approve(Status.PLAN_REVIEW, Action.APPROVE_PLAN, "To accept the findings")
        + " The spike closes as Done.",
        _change(Status.PLAN_REVIEW, Action.REQUEST_PLAN_CHANGES, "To ask for more investigation"),
        f"[Findings v{revision:03d}]({url})",
    ]
    if note:
        lines.append(note)
    lines += proposals_section(token, proposals)
    return "\n".join(lines)


def spike_done(revision: int, url: str, token: str, proposals: Sequence[ProposedTicket], moved: bool) -> str:
    lines = [f"## Spike complete: [findings v{revision:03d}]({url}) accepted"]
    if not moved:
        lines.append("**Move it to Done by hand**: no **Complete spike** transition exists in this workflow.")
    if proposals:
        lines += [
            "",
            "**To create the proposed follow-up tickets** (any time): comment this.",
            "```",
            f"{DecisionKind.CREATE_TICKETS.value} {token}",
            ", ".join(t.id for t in proposals),
            "```",
        ]
    return "\n".join(lines)


def fast_track_plan(url: str, revision: int) -> str:
    return (
        f"## Plan v{revision:03d} approved with the specification\n"
        f"Development starts next (moving into **{_into(Status.PLANNING, Action.USE_APPROVED_PLAN)}**). "
        f"[Plan v{revision:03d}]({url})"
    )


def plan_gate(
    url: str,
    footprint_url: str,
    revision: int,
    overlap: list[OverlapFinding],
    note: str = "",
) -> str:
    lines = [
        f"## Plan v{revision:03d} ready for review",
        _approve(Status.PLAN_REVIEW, Action.APPROVE_PLAN),
        _change(Status.PLAN_REVIEW, Action.REQUEST_PLAN_CHANGES),
        f"[Plan v{revision:03d}]({url}) · [change footprint]({footprint_url})",
    ]
    if note:
        lines.append(note)
    if overlap:
        lines += ["", "**Overlaps other in-flight work**:"]
        lines += [f"- {o.warning_id}: {o.kind.value} with {o.other}" for o in overlap]
    return "\n".join(lines)


def questions(draft_url: str, qs: list[Question], stage: str) -> str:
    answers = _stage_def(stage).answers_action
    title = STAGE_TITLES.get(stage, stage)
    lines = [
        "## Questions",
        f"**To answer**: reply in a comment, then choose **Submit {title.lower()} answers** "
        f"{_moves(Status.NEEDS_CLARIFICATION, answers)}.",
        "",
    ]
    for q in qs:
        lines.append(f"- **{q.id}** {q.question}" + (f" _(why: {q.rationale})_" if q.rationale else ""))
    lines += ["", f"[Current draft]({draft_url})"]
    return "\n".join(lines)


def blocked(stage: str, reason: str, action: str, resume_stage: str) -> str:
    resume = _stage_def(resume_stage).resume_action
    return "\n".join(
        [
            f"## Blocked during {STAGE_TITLES.get(stage, stage).lower()}",
            f"**Next action**: {action}, then choose **Resume "
            f"{STAGE_TITLES.get(resume_stage, resume_stage).lower()}** {_moves(Status.BLOCKED, resume)}.",
            f"Reason: {reason}",
        ]
    )


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _resolution_body(
    summary: str, decisions: list[dict[str, str]], follow_ups: list[str], developer: str
) -> list[str]:
    """The part of a resolution comment shared by both outcomes: who decided what.

    ``decisions`` are dicts with id, question, decision, decided_by ("developer" or "claude") and
    basis (how the coordinator knows: asked in the session, told Claude, or only Claude's say-so).
    What Claude did stays in the session transcript.
    """
    lines = [summary.strip()] if summary.strip() else []
    if decisions:
        lines += [
            *([""] if lines else []),
            "**Decisions**",
            "| | Decision | Decided by | How we know |",
            "|---|---|---|---|",
        ]
        for d in decisions:
            who = developer if d["decided_by"] == "developer" else "Claude"
            lines.append(
                f"| {d['id']} | **{_cell(d['question'])}** {_cell(d['decision'])} | {_cell(who)} | "
                f"{_cell(d.get('basis', ''))} |"
            )
    if follow_ups:
        lines += [*([""] if lines else []), "**Left for people**", *[f"- {f}" for f in follow_ups]]
    return lines


def resolved(
    *,
    resume_stage: str,
    summary: str,
    decisions: list[dict[str, str]],
    follow_ups: list[str],
    developer: str,
) -> str:
    title = STAGE_TITLES.get(resume_stage, resume_stage)
    ready = STATUS_NAMES[_stage_def(resume_stage).ready]
    lines = [
        f"## Blocker resolved: {title.lower()} resumes",
        f"Back in **{ready}**; {title.lower()} restarts by itself.",
        "",
        *_resolution_body(summary, decisions, follow_ups, developer),
    ]
    return "\n".join(lines)


def _next_steps(ticket: str, steps: list[dict[str, str]]) -> list[str]:
    """The steps a person takes now, each with what to paste or choose ready to copy."""
    lines = ["", "**What to do now**"]
    for n, st in enumerate(steps, 1):
        kind, text = st["kind"], st["text"].strip()
        if kind == "jira_comment":
            head = f"**{n}. Add this comment to {ticket}.**"
        elif kind == "jira_action":
            action = next(
                (a for a, name in DEFAULT_ACTION_NAMES.items() if name.lower() == text.lower()), None
            )
            route = route_for_action(Status.BLOCKED, action) if action else None
            head = (
                f"**{n}. Choose {text} in Jira**"
                + (f" (moves into **{STATUS_NAMES[route.target]}**)" if route else "")
                + "."
            )
        elif kind == "command":
            head = f"**{n}. Run this command.**"
        else:
            head = f"**{n}.** {text}"
        lines += ["", f"{head} {st.get('why', '')}".rstrip()]
        if kind in ("jira_comment", "command"):
            lines += ["```text", text, "```"]
    return lines


def unresolved(
    *,
    ticket: str = "",
    next_steps: list[dict[str, str]] | None = None,
    resume_stage: str,
    reason: str,
    decisions: list[dict[str, str]],
    follow_ups: list[str],
    developer: str,
) -> str:
    title = STAGE_TITLES.get(resume_stage, resume_stage).lower()
    resume = _stage_def(resume_stage).resume_action
    lines = [
        "## Blocker not resolved",
        f"**Back in Blocked**: {reason.strip()}",
        *(_next_steps(ticket, next_steps) if next_steps else []),
        "",
        f"Then choose **Resume {title}** {_moves(Status.BLOCKED, resume)}, or "
        f"**Request resolution** {_moves(Status.BLOCKED, Action.REQUEST_RESOLUTION)} to retry.",
    ]
    body = _resolution_body("", decisions, follow_ups, developer)
    return "\n".join([*lines, *(["", *body] if body else [])])


def waiting(stage: str, reason: str, action: str) -> str:
    title = STAGE_TITLES.get(stage, stage).lower()
    return "\n".join(
        [
            f"## Waiting before {title}",
            f"**Next action**: {action}",
            f"Not started: {reason}. The ticket stays in {STATUS_NAMES[_stage_def(stage).ready]}.",
        ]
    )


def claude_unavailable(stage: str, kind: str, worker_id: str) -> str:
    title = STAGE_TITLES.get(stage, stage)
    if kind == "auth":
        why = "Claude Code is not signed in on this machine."
        faster = f"run `claude auth login` in a terminal on `{worker_id}`."
    else:
        why = "the Claude usage limit has been reached."
        faster = "nothing; it carries on once the limit resets."
    return "\n".join(
        [
            f"## {title} paused: waiting for Claude",
            f"**To resume**: {faster}",
            f"Why: {why}",
        ]
    )


def internal_error(stage: str, run_id: str, worker_id: str, key: str) -> str:
    # The error text itself stays in the coordinator log: it can name local paths or carry
    # fragments of data that do not belong on a ticket.
    return "\n".join(
        [
            f"## {STAGE_TITLES.get(stage, stage)} stopped: coordinator error",
            f"**Next action**: on `{worker_id}`, run `coordinator recover {key} --resume` "
            "(`coordinator logs` shows the error).",
        ]
    )


def _failed_checks(results: Iterable[CheckResult]) -> list[str]:
    """Only failures go on the ticket; passing results stay in the logs and reports."""
    failed = [r for r in results if r.conclusion != "passed"]
    if not failed:
        return []
    return [
        "",
        "**Failed checks**:",
        *(
            f"- {f'[{r.name}]({r.url})' if r.url else r.name} ({r.source}/{r.target}): {r.conclusion}"
            for r in failed
        ),
    ]


def code_gate(
    candidate_no: int,
    pr_url: str,
    candidate: str,
    review_url: str,
    verification_url: str,
    checks: list[CheckResult],
    findings: list[Finding],
    unverified: list[str],
    overlap: list[OverlapFinding],
    *,
    base: str = "main",
    merge_conflicts: list[dict[str, Any]] | None = None,
    deviations: list[DeviationRecord] | None = None,
    claude_resolves: bool = False,
    reproduction: dict[str, Any] | None = None,
    proposal: bool = False,
) -> str:
    """The code decision only. Acceptance is its own step with its own comment (acceptance_ready)."""
    lines = [
        f"## Candidate c{candidate_no} ready for code review",
        "**To approve**: approve the PR on GitHub (independent reviewer), then choose **Approve code** "
        f"{_moves(Status.CODE_REVIEW, Action.APPROVE_CODE)}.",
        _change(Status.CODE_REVIEW, Action.REQUEST_CODE_CHANGES),
        f"PR: {pr_url} · `{candidate}` · [Review]({review_url}) · [Verification]({verification_url})",
    ]
    lines += _failed_checks(checks)
    lines += reproduction_lines(reproduction, base)
    lines += conflicts_section(merge_conflicts or [], base, claude_resolves=claude_resolves)
    lines += deviations_section(deviations or [], Status.CODE_REVIEW, proposal=proposal)
    if unverified:
        lines += ["", f"**Not independently verified** (check in acceptance): {', '.join(unverified)}"]
    if overlap:
        lines += ["", "**Overlapping work**: " + "; ".join(f"{o.warning_id} {o.other}" for o in overlap)]
    return "\n".join(lines)


def _clip(text: str, limit: int = 700) -> str:
    """Shorten long text at a sentence (or word) boundary, never mid-word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    if end < limit // 2:
        end = cut.rfind(" ")
    return cut[: end + 1].rstrip() + " …"


def _findings(findings: list[Finding], limit: int, clip: int = 300) -> list[str]:
    return [f"- **{f.id}** ({f.severity.value}): {_clip(f.description, clip)}" for f in findings[:limit]]


def _deviation_lines(devs: list[DeviationRecord]) -> list[str]:
    out = []
    for d in devs:
        why = (
            "asked for by the developer"
            if d.requested
            else "not asked for: Claude went beyond the specification"
        )
        where = f"; changes {d.criterion_id}" if d.criterion_id else ""
        out.append(f"- **{d.id}** ({why}{where}): {d.summary}")
    return out


def deviations_section(devs: list[DeviationRecord], here: Status, *, proposal: bool = False) -> list[str]:
    """Working differences from the approved specification: a question, never a failure.

    Approving the code accepts them (with a release proposal the specification is rewritten before
    release preparation, with no new refinement round); one named in a change request goes back to
    development.
    """
    if not devs:
        return []
    lines = ["", "**Deviations from the specification**:", *_deviation_lines(devs)]
    example = f"`{devs[0].id}: follow the specification`"
    if here is Status.CODE_REVIEW:
        accepted = (
            "Approving the code accepts them (the specification is updated before release)."
            if proposal
            else "Approving the code accepts them as built."
        )
        return [
            *lines,
            f"{accepted} To reject one, request code changes and name it ({example}).",
        ]
    return [
        *lines,
        f"To reject one, name it in a comment ({example}) before choosing **Submit implementation changes**.",
    ]


def spec_amended(revision: int, url: str, accepted: list[DeviationRecord]) -> str:
    lines = [
        f"## Specification v{revision:03d}: accepted deviations included",
        f"[Specification v{revision:03d}]({url}) is the approved version.",
        "",
        *_deviation_lines(accepted),
    ]
    return "\n".join(lines)


def reproduction_lines(rep: dict[str, Any] | None, base: str) -> list[str]:
    """A bug fix's regression tests on the base branch without the fix (reported, never a failure)."""
    if not rep:
        return []
    state = rep.get("state")
    if state == "reproduced":
        text = f"**Bug reproduced** on `{base}` without the fix: the tests catch it."
    elif state == "not_reproduced":
        text = (
            f"**Bug not reproduced**: the tests also pass on `{base}` without the fix. Check the "
            "regression test."
        )
    elif state == "no_tests":
        text = "**No regression test**: this bug fix adds or changes no test files."
    else:
        text = f"**Reproduction not checked**: {rep.get('detail', 'the check could not run')}."
    return ["", text]


def conflicts_section(
    conflicts: list[dict[str, Any]], base: str, *, claude_resolves: bool = False
) -> list[str]:
    """Flag textual conflicts. They are resolved when the PR is merged, never a failure (or, with
    ``claude_resolves``, by the next development run if someone asks for changes)."""
    if not conflicts:
        return []
    lines = ["", "**Merge conflicts** (resolve when merging the PR):"]
    for c in conflicts:
        paths = ", ".join(c["paths"])
        if c["with"] == base:
            lines.append(f"- latest `{base}`: {paths}")
        else:
            lines.append(f"- {c['with']}'s candidate (not merged yet): {paths}")
    if claude_resolves:
        lines.append(f"Or request changes: Claude merges the latest `{base}` and resolves it.")
    return lines


def verification_failed(
    pr_url: str,
    candidate_no: int,
    candidate: str,
    review_url: str,
    verification_url: str,
    checks: list[CheckResult],
    findings: list[Finding],
    problems: list[str],
    *,
    base: str = "main",
    merge_conflicts: list[dict[str, Any]] | None = None,
    deviations: list[DeviationRecord] | None = None,
    claude_resolves: bool = False,
) -> str:
    serious = [f for f in findings if f.severity in (Severity.BLOCKER, Severity.MAJOR)]
    why = [f"- **R{i}**: {p}" for i, p in enumerate(problems, 1)]
    lines = [
        f"## Verification failed for candidate c{candidate_no} `{candidate[:12]}`",
        "**To fix it**: choose **Submit implementation changes** "
        f"{_moves(Status.CHANGES_REQUESTED, Action.SUBMIT_IMPLEMENTATION_CHANGES)}. "
        "To change what is built instead, choose **Revise scope** "
        f"{_moves(Status.CHANGES_REQUESTED, Action.REVISE_SCOPE)}.",
        f"PR: {pr_url} · [Review]({review_url}) · [Verification]({verification_url})",
        "",
        "**Why**:",
        *why,
    ]
    if serious:
        lines += _findings(serious, 15)
    others = [f for f in findings if f not in serious]
    if others:
        lines.append(f"- Other findings (in the reports): {', '.join(f.id for f in others)}")
    lines += _failed_checks(checks)
    lines += deviations_section(deviations or [], Status.CHANGES_REQUESTED)
    lines += conflicts_section(merge_conflicts or [], base, claude_resolves=claude_resolves)
    return "\n".join(lines)


def acceptance_ready(
    key: str,
    candidate_no: int,
    pr_url: str,
    *,
    worker_id: str,
    local_app: bool,
    try_command: bool,
    guide_url: str | None,
    proposal: bool = False,
) -> str:
    """Posted when a ticket enters Acceptance review: the product decision, then how to try it."""
    into = STATUS_NAMES[Status.READY_RELEASE_PREPARATION if proposal else Status.READY_RELEASE]
    then = (
        "Claude then writes a release proposal."
        if proposal
        else "Then merge the PR; the ticket moves to Done when the merge is seen."
    )
    lines = [
        f"## Ready for acceptance (candidate c{candidate_no})",
        f"**To accept**: choose **Accept delivery** (moves into **{into}**). {then}",
        _change(Status.ACCEPTANCE_REVIEW, Action.REQUEST_ACCEPTANCE_CHANGES),
        "",
    ]
    if local_app:
        lines.append(f"Running on `{worker_id}` (`delivery preview {key}` reopens it).")
    if try_command:
        lines.append(f"Try it on your machine: `delivery try {key}`")
    lines.append(f"[PR]({pr_url})" + (f" · [Acceptance guide]({guide_url})" if guide_url else ""))
    return "\n".join(lines)


def release_gate(
    url: str,
    revision: int,
    candidate: str,
    note: str = "",
    merged_early: str | None = None,
    pr_number: int | None = None,
) -> str:
    if merged_early:
        after = f" PR #{pr_number} is already merged (`{merged_early[:12]}`): approving makes it the release."
    else:
        after = " Then merge the PR; the ticket moves to Done when the merge is seen."
    return "\n".join(
        [
            f"## Release proposal v{revision:03d} ready",
            _approve(Status.RELEASE_REVIEW, Action.APPROVE_RELEASE) + after,
            _change(Status.RELEASE_REVIEW, Action.REQUEST_RELEASE_CHANGES),
            f"[Release proposal]({url}) · candidate `{candidate}`",
            *(["", note] if note else []),
        ]
    )


def done(
    release_commit: str,
    environment: str,
    pr_number: int | None,
    merged_by: str | None,
    provenance: str,
    *,
    flagged: bool = False,
    deviations: list[str] | None = None,
) -> str:
    by = f" by {merged_by}" if merged_by else ""
    lines = [
        "## Released: Done",
        f"PR #{pr_number} merged{by} as `{release_commit}` in `{environment}`.",
    ]
    if flagged:
        lines.append(f"**Look at this**: {provenance}")
    if deviations:
        lines.append(f"Deviations accepted with the code: {', '.join(deviations)}.")
    return "\n".join(lines)


def candidate_ready(
    candidate_no: int,
    sha: str,
    pr_url: str,
    *,
    merge_conflicts: list[dict[str, Any]] | None = None,
    base: str = "main",
    session_open: bool = False,
    resolved: dict[str, Any] | None = None,
) -> str:
    starts = "once the developer closes the Claude session" if session_open else "next"
    lines = [
        f"## Candidate c{candidate_no} ready for verification",
        f"Verification starts {starts}. PR: {pr_url} · commit `{sha}`",
    ]
    if resolved:
        lines.append(
            f"Latest `{base}` merged in; Claude resolved conflicts in {', '.join(resolved['paths'])}."
        )
    if merge_conflicts:
        lines.append(
            f"`{base}` conflicts in {', '.join(p for c in merge_conflicts for p in c['paths'])} and was not "
            "merged in: resolve when merging the PR."
        )
    return "\n".join(lines)


def follow_up(
    candidate_no: int,
    sha: str,
    url: str,
    requests: list[str],
    was_in: str,
    moved: bool,
    *,
    ended: bool = False,
) -> str:
    """A candidate pushed from the open development session (``ended``: as it closed)."""
    starts = "starting now" if ended else "starting when the developer closes the session"
    lines = [
        f"## Follow-up change: candidate c{candidate_no}",
        f"Commit `{sha}`: {url}. Asked in the open session:",
        *[f"- {r}" for r in requests],
        (
            f"Moved from {was_in} back to Ready for verification ({starts}); earlier approvals do not "
            "cover it."
            if moved
            else f"Review and verification run on it ({starts}); earlier approvals do not cover it."
        ),
    ]
    return "\n".join(lines)


def follow_up_revision(stage: str, replaces: str, requests: list[str]) -> str:
    """The summary of a revision published from a session left open after its stage. Approving is
    a move, so it says plainly that the next move approves this revision, not the one it replaces."""
    m = GATE_TOKEN.match(replaces)
    old = f"v{int(m.group('rev')):03d}" if m else replaces
    return "\n".join(
        [
            f"**Follow-up revision** replacing {old}: the next move approves this revision. Changed:",
            *[f"- {r}" for r in requests],
        ]
    )


def overlap_warning(f: OverlapFinding, assignees: dict[str, str | None], here: str) -> str:
    other = f.other if f.ticket == here else f.ticket
    lines = [
        f"## Overlap warning: {f.warning_id}",
        f"{here} and {other} ({assignees.get(other) or 'unassigned'}) overlap: **{f.kind.value}**.",
        *[f"- {d}" for d in f.details[:10]],
    ]
    if f.severity is OverlapSeverity.HIGH:
        lines.append("Higher risk: agree which merges first, or choose **Revise scope** on one.")
    return "\n".join(lines)


def handover(stage: str, state: str, artefacts: dict[str, str], next_action: str) -> str:
    lines = [f"## Handover checkpoint ({STAGE_TITLES.get(stage, stage)})", f"**Next action**: {next_action}"]
    if stage in STAGE_TITLES and state.startswith("blocked"):
        resume = _stage_def(stage).resume_action
        lines.append(
            f"The new owner chooses **Resume {STAGE_TITLES[stage].lower()}** "
            f"{_moves(Status.BLOCKED, resume)}."
        )
    lines += ["", f"State: {state}", *[f"- {k}: {v}" for k, v in sorted(artefacts.items())]]
    return "\n".join(lines)
