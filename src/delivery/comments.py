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
    "release_verification": "Release verification",
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


def _approve(who: str, source: Status, action: Action, what: str = "To approve") -> str:
    return (
        f"**{what}** ({who}): choose **{DEFAULT_ACTION_NAMES[action]}** {_moves(source, action)}. "
        "No comment needed."
    )


def _change(source: Status, action: Action, what: str = "To request changes") -> str:
    return (
        f"**{what}**: choose **{DEFAULT_ACTION_NAMES[action]}** {_moves(source, action)}. Say what "
        "to change in a comment if you like; Claude reads every comment written since this was "
        "posted, and asks you when there is none."
    )


def _ready_to_move(source: Status, action: Action, when: str) -> str:
    return f"**Ready to move into {_into(source, action)}** {when}."


def _stage_def(stage: str) -> StageDef:
    return STAGES[Stage(stage)]


def _session_line(run_id: str, worker_id: str) -> str:
    return f"Run `{run_id}` on worker `{worker_id}`."


def started(
    stage: str,
    run_id: str,
    worker_id: str,
    reason: str,
    moved_by_hand: str | None = None,
) -> str:
    text = (
        f"**{STAGE_TITLES.get(stage, stage)} started.** {_session_line(run_id, worker_id)}\nInput: {reason}"
    )
    if moved_by_hand:
        text += (
            f"\nMoved into {moved_by_hand} by hand. Leave tickets in their Ready status; the "
            "coordinator moves them within a minute."
        )
    return text


def design_drift(stage: str, frames: list[tuple[str, str]]) -> str:
    listed = "\n".join(f"- [{name}]({url})" for name, url in frames)
    return (
        "**To adopt the new Figma design**, choose Revise scope (or request specification changes).\n"
        f"The design changed since the specification was written. {STAGE_TITLES.get(stage, stage)} "
        f"keeps building the approved version. Changed frames:\n{listed}"
    )


def proposals_section(token: str, proposals: Sequence[ProposedTicket]) -> list[str]:
    """Tickets Claude proposes (slices or follow-ups): created only when someone asks."""
    if not proposals:
        return []
    lines = [
        "",
        "**To create proposed tickets** in Backlog (optional): comment this with the IDs to create.",
        "```",
        f"{DecisionKind.CREATE_TICKETS.value} {token}",
        ", ".join(t.id for t in proposals),
        "```",
    ]
    for t in proposals:
        first = t.description.strip().splitlines()[0] if t.description.strip() else ""
        lines.append(f"- **{t.id}** {t.summary}" + (f": {_clip(first, 200)}" if first else ""))
    return lines


def spec_gate(
    token: str,
    url: str,
    revision: int,
    summary: str,
    approvers: str,
    *,
    plan: tuple[int, str] | None = None,
    fast_track_note: str = "",
    proposals: Sequence[ProposedTicket] = (),
) -> str:
    lines = [
        f"## Specification v{revision:03d} ready for review",
        _approve(approvers, Status.SPECIFICATION_REVIEW, Action.APPROVE_SPECIFICATION),
        _change(Status.SPECIFICATION_REVIEW, Action.REQUEST_SPECIFICATION_CHANGES),
        f"[Specification v{revision:03d}]({url})",
    ]
    if plan:
        lines.append(
            f"Fast track: [plan v{plan[0]:03d}]({plan[1]}) was written with it. Approving the "
            "specification approves the plan too, so development starts next."
        )
    elif fast_track_note:
        lines.append(f"Fast track not used: {fast_track_note}.")
    lines += ["", summary]
    lines += proposals_section(token, proposals)
    return "\n".join(lines)


def findings_gate(
    token: str,
    url: str,
    revision: int,
    summary: str,
    approvers: str,
    proposals: Sequence[ProposedTicket] = (),
) -> str:
    """A spike's findings, reviewed in Plan review. Accepting them completes the spike."""
    lines = [
        f"## Findings v{revision:03d} ready for review",
        _approve(approvers, Status.PLAN_REVIEW, Action.APPROVE_PLAN, "To accept the findings")
        + " The spike then closes as Done.",
        _change(Status.PLAN_REVIEW, Action.REQUEST_PLAN_CHANGES, "To ask for more investigation"),
        f"[Findings v{revision:03d}]({url})",
        "",
        summary,
    ]
    lines += proposals_section(token, proposals)
    return "\n".join(lines)


def spike_done(revision: int, url: str, token: str, proposals: Sequence[ProposedTicket], moved: bool) -> str:
    lines = [
        f"## Spike complete: findings v{revision:03d} accepted",
        f"[The findings]({url}) are the result. Nothing to build or release.",
    ]
    if not moved:
        lines.insert(
            1,
            "**Move it to Done by hand**: this Jira workflow has no **Complete spike** transition "
            "(Ready for development to Done).",
        )
    if proposals:
        lines += [
            "",
            "**To create the proposed follow-up tickets** (any time, even after Done): comment this.",
            "```",
            f"{DecisionKind.CREATE_TICKETS.value} {token}",
            ", ".join(t.id for t in proposals),
            "```",
        ]
    return "\n".join(lines)


def fast_track_plan(url: str, revision: int) -> str:
    return "\n".join(
        [
            f"## Plan v{revision:03d} approved with the specification",
            f"Nothing to do: development starts by itself (moving into "
            f"**{_into(Status.PLANNING, Action.USE_APPROVED_PLAN)}**). "
            f"[Plan v{revision:03d}]({url}) was approved with the specification.",
        ]
    )


def plan_gate(
    url: str,
    footprint_url: str,
    revision: int,
    summary: str,
    approvers: str,
    overlap: list[OverlapFinding],
) -> str:
    lines = [
        f"## Plan v{revision:03d} ready for review",
        _approve(approvers, Status.PLAN_REVIEW, Action.APPROVE_PLAN),
        _change(Status.PLAN_REVIEW, Action.REQUEST_PLAN_CHANGES),
        f"[Plan v{revision:03d}]({url}) · [change footprint]({footprint_url})",
        "",
        summary,
    ]
    if overlap:
        lines += ["", "**Overlap with other in-flight work** (advisory):"]
        lines += [
            f"- {o.warning_id}: {o.kind.value} with {o.other} ({', '.join(o.details[:3])})" for o in overlap
        ]
    return "\n".join(lines)


def questions(draft_url: str, qs: list[Question], who: str, stage: str) -> str:
    answers = _stage_def(stage).answers_action
    title = STAGE_TITLES.get(stage, stage)
    lines = [
        "## Questions",
        f"**To answer** ({who}): reply in a comment, in your own words (one comment or several), "
        f"then choose **Submit {title.lower()} answers** {_moves(Status.NEEDS_CLARIFICATION, answers)}.",
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
            f"**Next action**: {action}",
            f"Then choose **Resume {STAGE_TITLES.get(resume_stage, resume_stage).lower()}** "
            f"{_moves(Status.BLOCKED, resume)} once any previous worker has stopped.",
            f"**Reason**: {reason}",
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
        f"Nothing to do: back in **{ready}**, {title.lower()} starts again within a minute.",
        "",
        *_resolution_body(summary, decisions, follow_ups, developer),
    ]
    return "\n".join(lines)


def _next_steps(ticket: str, steps: list[dict[str, str]]) -> list[str]:
    """The steps a person takes now, each with what to paste or choose ready to copy."""
    lines = ["", "**What to do now**"]
    for n, st in enumerate(steps, 1):
        kind, text, who = st["kind"], st["text"].strip(), st.get("who") or "the developer"
        if kind == "jira_comment":
            head = f"**{n}. Add this comment to {ticket}** ({who})."
        elif kind == "jira_action":
            action = next(
                (a for a, name in DEFAULT_ACTION_NAMES.items() if name.lower() == text.lower()), None
            )
            route = route_for_action(Status.BLOCKED, action) if action else None
            head = (
                f"**{n}. Choose {text} in Jira**"
                + (f" (moves into **{STATUS_NAMES[route.target]}**)" if route else "")
                + f" ({who})."
            )
        elif kind == "command":
            head = f"**{n}. Run this command** ({who})."
        else:
            head = f"**{n}.** {text} ({who})."
        lines += ["", f"{head} {st.get('why', '')}".rstrip()]
        if kind in ("jira_comment", "command"):
            lines += ["```text", text, "```"]
        lines.append(f"Checked: {st.get('verified_by', '')}")
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
        f"When the cause is dealt with, choose **Resume {title}** {_moves(Status.BLOCKED, resume)}, or "
        f"**Request resolution** {_moves(Status.BLOCKED, Action.REQUEST_RESOLUTION)} to try again.",
    ]
    body = _resolution_body("", decisions, follow_ups, developer)
    return "\n".join([*lines, *(["", *body] if body else [])])


def waiting(stage: str, reason: str, action: str) -> str:
    title = STAGE_TITLES.get(stage, stage).lower()
    return "\n".join(
        [
            f"## Waiting before {title}",
            f"**Next action**: {action}",
            f"Not started: {reason}. The ticket stays in {STATUS_NAMES[_stage_def(stage).ready]} and "
            f"{title} starts by itself within a minute of that. Do not move it.",
        ]
    )


def claude_unavailable(stage: str, kind: str, worker_id: str) -> str:
    title = STAGE_TITLES.get(stage, stage)
    if kind == "auth":
        why = "Claude Code is not signed in on this machine (the login is missing or expired)."
        faster = f"run `claude auth login` in a terminal on `{worker_id}`."
    else:
        why = "the Claude subscription usage limit has been reached. No paid API fallback is used."
        faster = "nothing; it carries on once the limit resets."
    return "\n".join(
        [
            f"## {title} paused: waiting for Claude",
            f"**To speed it up**: {faster}",
            f"Why: {why} Nothing is needed in Jira: {title.lower()} continues automatically on "
            f"`{worker_id}` once Claude works again, and new tickets wait too.",
        ]
    )


def internal_error(stage: str, run_id: str, worker_id: str, key: str) -> str:
    # The error text itself stays in the coordinator log: it can name local paths or carry
    # fragments of data that do not belong on a ticket.
    return "\n".join(
        [
            f"## {STAGE_TITLES.get(stage, stage)} stopped: coordinator error",
            f"**Next action**: on `{worker_id}`, run `coordinator recover {key} --resume`. "
            "`coordinator logs` shows the error.",
            f"The ticket stays where it is. {_session_line(run_id, worker_id)}",
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
    reviewers: str,
    *,
    base: str = "main",
    merge_conflicts: list[dict[str, Any]] | None = None,
    deviations: list[DeviationRecord] | None = None,
    claude_resolves: bool = False,
    reproduction: dict[str, Any] | None = None,
) -> str:
    """The code decision only. Acceptance is its own step with its own comment (acceptance_ready)."""
    devs = deviations or []
    lines = [
        f"## Candidate c{candidate_no} ready for code review",
        f"**To approve the code**: an independent human ({reviewers}) approves the PR on GitHub at "
        "the current head with required CI passing, then anyone allowed to decide chooses **Approve "
        f"code** {_moves(Status.CODE_REVIEW, Action.APPROVE_CODE)}. No comment needed. Acceptance is "
        "the next step and gets its own comment.",
        _change(Status.CODE_REVIEW, Action.REQUEST_CODE_CHANGES)
        + " Unresolved PR review conversations are included too.",
        f"PR: {pr_url} · candidate `{candidate}` · [Independent review]({review_url}) · "
        f"[Verification report]({verification_url})",
    ]
    lines += _failed_checks(checks)
    lines += reproduction_lines(reproduction, base)
    lines += conflicts_section(merge_conflicts or [], base, claude_resolves=claude_resolves)
    lines += deviations_section(devs, Status.CODE_REVIEW)
    if findings:
        lines += ["", "**Non-blocking findings** (in the review): " + ", ".join(f.id for f in findings)]
    if unverified:
        lines += ["", f"**Not independently verified** (check during acceptance): {', '.join(unverified)}"]
    if overlap:
        lines += ["", "**Integration scrutiny requested** for overlapping work:"]
        lines += [f"- {o.warning_id}: {o.other} ({', '.join(o.details[:3])})" for o in overlap]
    lines += ["", "Any new commit on the PR needs verifying again before the code can be approved."]
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


def deviations_section(devs: list[DeviationRecord], here: Status) -> list[str]:
    """Working differences from the approved specification: a question, never a failure.

    Approving the code accepts them (the specification is rewritten before release preparation,
    no new refinement round); one named in a change request goes back to development.
    """
    if not devs:
        return []
    lines = [
        "",
        "**Deviations from the approved specification** (not failures; is each acceptable?):",
        *_deviation_lines(devs),
    ]
    if here is Status.CODE_REVIEW:
        return [
            *lines,
            "**If acceptable**: nothing extra to do. Approving the code accepts them, and Claude "
            "updates the specification before release preparation (no new refinement round).",
            "**If not**: choose **Request code changes** and name each in a comment (for example "
            f"`{devs[0].id}: follow the specification`); development changes it back.",
        ]
    return [
        *lines,
        "**If not acceptable**: name each in a comment (for example "
        f"`{devs[0].id}: follow the specification`) before choosing **Submit implementation "
        "changes**. One nobody names is left as is, and accepted when the code is approved.",
    ]


def spec_amended(revision: int, url: str, accepted: list[DeviationRecord], summary: str) -> str:
    lines = [
        f"## Specification v{revision:03d}: accepted deviations included",
        f"[Specification v{revision:03d}]({url}) now includes the deviations below and is the approved "
        "specification. No new refinement or planning round.",
        "",
        *_deviation_lines(accepted),
    ]
    if summary:
        lines += ["", f"**What changed**: {_clip(summary)}"]
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
            f"**Bug not reproduced**: the tests also pass on `{base}` without the fix, so they may "
            "not catch the bug. Check the regression test."
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
    lines = ["", "**Merge conflicts** (flagged, not a failure; resolve them in the PR when you merge):"]
    for c in conflicts:
        paths = ", ".join(c["paths"])
        if c["with"] == base:
            lines.append(f"- the latest `{base}` (`{str(c['sha'])[:12]}`): {paths}")
        else:
            lines.append(
                f"- {c['with']}'s candidate `{str(c['sha'])[:12]}` (not merged yet): {paths}. "
                "Whichever merges second resolves it."
            )
    if claude_resolves:
        lines.append(
            f"Or have Claude do it: the next development run (any change request) merges the latest "
            f"`{base}` first and resolves the conflict with it."
        )
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
        "**To fix it** (the usual next step): choose **Submit implementation changes** "
        f"{_moves(Status.CHANGES_REQUESTED, Action.SUBMIT_IMPLEMENTATION_CHANGES)}. Development gets "
        "every R- and F-item and the PR's unresolved review conversations, and publishes candidate "
        f"c{candidate_no + 1}.",
    ]
    if findings:
        lines.append(
            "To fix only some findings, say which in a comment first (for example `only F2 and F3`); "
            "R-items are always included."
        )
    lines += [
        "Other options: **Revise scope** to change what is built "
        f"{_moves(Status.CHANGES_REQUESTED, Action.REVISE_SCOPE)}, or **Submit follow-up changes** to "
        f"verify the same candidate again {_moves(Status.CHANGES_REQUESTED, Action.SUBMIT_FOLLOW_UP)} "
        f"(only useful if the cause was outside the code, such as a flaky check or a change on {base}).",
        f"PR: {pr_url} · [Independent review]({review_url}) · [Verification report]({verification_url})",
        "",
        "**Why it failed**:",
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
    candidate: str,
    pr_url: str,
    *,
    worker_id: str,
    local_app: bool,
    try_command: bool,
    guide: str,
    guide_url: str | None,
) -> str:
    """Posted when a ticket enters Acceptance review: the product decision, then how to try it."""
    lines = [
        f"## Code approved: ready for acceptance (candidate c{candidate_no})",
        f"**To accept** (product decision against the brief): choose **Accept delivery** "
        f"{_moves(Status.ACCEPTANCE_REVIEW, Action.ACCEPT_DELIVERY)}. No comment needed.",
        _change(Status.ACCEPTANCE_REVIEW, Action.REQUEST_ACCEPTANCE_CHANGES),
        "**Try it**:",
    ]
    if local_app:
        lines.append(
            f"- On `{worker_id}` the candidate is running and open in the browser "
            f"(`delivery preview {key}` opens it again)."
        )
    if try_command:
        lines.append(f"- On your machine: `delivery try {key}` runs candidate c{candidate_no} and opens it.")
    lines.append(f"- Code: {pr_url} at `{candidate[:12]}`")
    if guide:
        link = f" ([full guide]({guide_url}))" if guide_url else ""
        lines += ["", f"**What to check**{link}:", "", guide.strip()]
    return "\n".join(lines)


def release_gate(
    url: str,
    revision: int,
    candidate: str,
    approvers: str,
    environment: str,
    note: str = "",
    merged_early: str | None = None,
    pr_number: int | None = None,
) -> str:
    if merged_early:
        after = (
            f" **PR #{pr_number} was already merged (`{merged_early[:12]}`) before this approval.** "
            "The merged code is the accepted candidate, so approving makes that merge the release: "
            f"the coordinator records it in `{environment}` (moving into "
            f"**{_into(Status.READY_RELEASE, Action.RECORD_RELEASE)}**) and verifies. "
            "If the release should not stand, request changes instead."
        )
    else:
        after = (
            f" Then a human merges the PR: that is the release, which the coordinator records in "
            f"`{environment}` (moving into **{_into(Status.READY_RELEASE, Action.RECORD_RELEASE)}**) "
            "and verifies. It never merges or deploys."
        )
    return "\n".join(
        [
            f"## Release proposal v{revision:03d} ready",
            _approve(approvers, Status.RELEASE_REVIEW, Action.APPROVE_RELEASE) + after,
            _change(Status.RELEASE_REVIEW, Action.REQUEST_RELEASE_CHANGES),
            f"[Release proposal]({url}) · accepted candidate `{candidate}`",
            *(["", note] if note else []),
        ]
    )


def done(release_commit: str, environment: str, url: str, provenance: str) -> str:
    return "\n".join(
        [
            "## Release verified: Done",
            f"Released commit `{release_commit}` in `{environment}`. [Release verification]({url}).",
            f"Provenance: {provenance}",
        ]
    )


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
    starts = (
        "once the developer closes the Claude session, which stays open for further changes"
        if session_open
        else "by themselves"
    )
    lines = [
        f"## Implementation candidate c{candidate_no} ready for verification",
        f"Nothing to do: moving into **{_into(Status.DEVELOPING, Action.COMPLETE_DEVELOPMENT)}**, "
        f"where independent review and verification start {starts}.",
        f"PR: {pr_url} · commit `{sha}`",
    ]
    if resolved:
        lines.append(
            f"The latest `{base}` (`{str(resolved['sha'])[:12]}`) was merged in first; Claude resolved "
            f"the conflicts in {', '.join(resolved['paths'])}."
        )
    if merge_conflicts:
        lines.append(
            f"The latest `{base}` conflicts in "
            f"{', '.join(p for c in merge_conflicts for p in c['paths'])} and was not merged in. "
            "Resolve it when merging the pull request."
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
    starts = (
        "the developer has closed the session, so they start now"
        if ended
        else "they start once the developer closes the session"
    )
    lines = [
        f"## Follow-up change: candidate c{candidate_no}",
        f"Commit `{sha}`: {url}. Asked of Claude in the open session:",
        *[f"- {r}" for r in requests],
        "Earlier code and acceptance approvals do not cover it.",
        (
            f"The ticket moved from {was_in} back to Ready for verification; review and verification "
            f"run again ({starts})."
            if moved
            else f"Review and verification run on the latest candidate ({starts})."
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
            f"**Follow-up revision** replacing {old}: moving the ticket on now approves this "
            f"revision, not {old}. Changed in the open {STAGE_TITLES.get(stage, stage).lower()} session:",
            *[f"- {r}" for r in requests],
        ]
    )


def overlap_warning(f: OverlapFinding, assignees: dict[str, str | None], here: str) -> str:
    other = f.other if f.ticket == here else f.ticket
    lines = [
        f"## Overlap warning: {f.warning_id}",
        f"{here} and {other} ({assignees.get(other) or 'unassigned'}) overlap: **{f.kind.value}**.",
        *[f"- {d}" for d in f.details[:10]],
        "Work continues on both; verification tests each candidate with the other's, and merge "
        "conflicts are flagged to resolve when merging.",
    ]
    if f.severity is OverlapSeverity.HIGH:
        lines.append(
            "Higher risk (shared interface, schema or migration, or a dependency): agree which merges "
            "first. To rethink one ticket choose **Revise scope**; to tell its next Claude session about "
            "the other, comment starting with `FOR CLAUDE`."
        )
    return "\n".join(lines)


def handover(stage: str, state: str, artefacts: dict[str, str], next_action: str) -> str:
    lines = [f"## Handover checkpoint ({STAGE_TITLES.get(stage, stage)})", f"**Next action**: {next_action}"]
    if stage in STAGE_TITLES and state.startswith("blocked"):
        resume = _stage_def(stage).resume_action
        lines.append(
            f"Once reassigned, the new owner chooses **Resume {STAGE_TITLES[stage].lower()}** "
            f"{_moves(Status.BLOCKED, resume)}."
        )
    lines += ["", f"State: {state}", *[f"- {k}: {v}" for k, v in sorted(artefacts.items())]]
    return "\n".join(lines)
