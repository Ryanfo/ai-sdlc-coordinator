"""Jira comment bodies (markdown subset converted to ADF on publication).

Every comment names the exact artefact revision via an immutable commit link, the token
the human must use, a copyable template and the next human action. Every comment that waits
for a human starts with the status the ticket is ready to move into, and each action it
offers says which status it moves the ticket into (taken from the workflow definition).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from delivery.feedback import (
    DecisionKind,
    answer_template,
    approve_template,
    change_template,
)
from delivery.models import CheckResult, DeviationRecord, Finding, ProposedTicket, Question, Severity
from delivery.overlap import OverlapFinding
from delivery.overlap import Severity as OverlapSeverity
from delivery.workflow import STAGES, STATUS_NAMES, Action, Stage, StageDef, Status, route_for_action

STAGE_TITLES = {
    "refinement": "Refinement",
    "planning": "Planning",
    "development": "Development",
    "verification": "Verification",
    "release_preparation": "Release preparation",
    "release_verification": "Release verification",
    "resolution": "Resolution",
}


NOTE_HINT = (
    "To guide the next Claude session, first add a comment that starts with `FOR CLAUDE` "
    "(or `FOR CLAUDE development` for one stage) followed by what it should know or do."
)


PR_COMMENTS_HINT = (
    "Unresolved review conversations on the pull request are included as G-items, so the "
    "request needs no F-items when the PR comments say it all; resolve a conversation on GitHub "
    "to leave it out."
)


def _into(source: Status, action: Action) -> str:
    """The status a Jira action moves the ticket into, by its display name."""
    route = route_for_action(source, action)
    if route is None:
        raise ValueError(f"the workflow has no {action.value} from {source.value}")
    return STATUS_NAMES[route.target]


def _moves(source: Status, action: Action) -> str:
    return f"(moves into **{_into(source, action)}**)"


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
    models: dict[str, str | None] | None = None,
    notes: int = 0,
) -> str:
    text = (
        f"**{STAGE_TITLES.get(stage, stage)} started.** {_session_line(run_id, worker_id)}\nInput: {reason}"
    )
    if notes:
        text += f"\nNotes for Claude: {notes} `FOR CLAUDE` comment{'s' if notes != 1 else ''} included"
    if models:
        names = {p: m or "Claude Code default" for p, m in models.items()}
        if len(set(names.values())) == 1:
            text += f"\nModel: {next(iter(names.values()))}"
        else:
            text += "\nModels: " + ", ".join(f"{p} {m}" for p, m in names.items())
    if moved_by_hand:
        text += (
            f"\nThis ticket was moved into {moved_by_hand} by hand before the coordinator picked it "
            "up. The decision that made it ready was checked as usual and the work has started. "
            "Next time, leave the ticket in its Ready status; the coordinator moves it within a minute."
        )
    return text


def design_drift(stage: str, frames: list[tuple[str, str]]) -> str:
    listed = "\n".join(f"- [{name}]({url})" for name, url in frames)
    return (
        f"**Design changed in Figma since the specification was written.** "
        f"{STAGE_TITLES.get(stage, stage)} is using the version the approved specification was "
        f"based on, so the build matches what was approved. Changed frames:\n{listed}\n"
        "To adopt the new design, choose Revise scope (or request specification changes) so the "
        "specification is updated from it."
    )


def proposals_section(token: str, proposals: Sequence[ProposedTicket]) -> list[str]:
    """Tickets Claude proposes (slices or follow-ups): created only when someone asks."""
    if not proposals:
        return []
    lines = ["", "**Proposed tickets** (nothing is created unless you ask):"]
    for t in proposals:
        first = t.description.strip().splitlines()[0] if t.description.strip() else ""
        lines.append(f"- **{t.id}** {t.summary}" + (f": {_clip(first, 200)}" if first else ""))
    lines += [
        "To create some or all of them in Backlog (unassigned, linked to this ticket), add this "
        "comment with their IDs. A note after an ID (`S2: call it Export`) goes into that ticket.",
        "```",
        f"{DecisionKind.CREATE_TICKETS.value} {token}",
        ", ".join(t.id for t in proposals),
        "```",
    ]
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
        f"## Specification v{revision:03d} ready for review: {token}",
        _ready_to_move(
            Status.SPECIFICATION_REVIEW,
            Action.APPROVE_SPECIFICATION,
            "once the specification is approved",
        ),
        f"[Read specification v{revision:03d}]({url}) (pinned to the exact commit).",
    ]
    if plan:
        lines.append(
            f"**Fast track**: [plan v{plan[0]:03d}]({plan[1]}) was written with this specification. "
            "Approving the specification approves this plan too, so development starts straight after "
            "it (no separate plan review)."
        )
    elif fast_track_note:
        lines.append(f"**Fast track not used**: {fast_track_note}.")
    lines += [
        "",
        summary,
        "",
        f"**To approve** ({approvers}): add this comment, then choose **Approve specification** "
        f"{_moves(Status.SPECIFICATION_REVIEW, Action.APPROVE_SPECIFICATION)}.",
        "```",
        approve_template(DecisionKind.APPROVE_SPEC, token),
        "```",
        "**To request changes**: add this comment with numbered items, then choose "
        "**Request specification changes** "
        f"{_moves(Status.SPECIFICATION_REVIEW, Action.REQUEST_SPECIFICATION_CHANGES)}.",
        "```",
        change_template(DecisionKind.CHANGE_SPEC, token),
        "```",
    ]
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
        f"## Findings v{revision:03d} ready for review: {token}",
        _ready_to_move(Status.PLAN_REVIEW, Action.APPROVE_PLAN, "once the findings are accepted")
        + " The coordinator then closes the spike (Done): there is nothing to build or release.",
        f"[Read findings v{revision:03d}]({url}) (pinned to the exact commit).",
        "",
        summary,
        "",
        f"**To accept the findings** ({approvers}): add this comment, then choose **Approve plan** "
        f"{_moves(Status.PLAN_REVIEW, Action.APPROVE_PLAN)}.",
        "```",
        approve_template(DecisionKind.APPROVE_PLAN, token),
        "```",
        "**To ask for more investigation**: add this comment with numbered items, then choose "
        f"**Request plan changes** {_moves(Status.PLAN_REVIEW, Action.REQUEST_PLAN_CHANGES)}.",
        "```",
        change_template(DecisionKind.CHANGE_PLAN, token),
        "```",
    ]
    lines += proposals_section(token, proposals)
    return "\n".join(lines)


def spike_done(revision: int, url: str, token: str, proposals: Sequence[ProposedTicket], moved: bool) -> str:
    lines = [
        f"## Spike complete: findings v{revision:03d} accepted",
        f"[The findings]({url}) are the result of this ticket; there is nothing to build or release.",
    ]
    if not moved:
        lines.append(
            "**Move it to Done by hand**: this Jira workflow has no **Complete spike** transition "
            "(Ready for development to Done), so the coordinator cannot close it."
        )
    if proposals:
        lines += [
            "",
            "Follow-up tickets were proposed: create them any time (even after Done) with this comment.",
            "```",
            f"{DecisionKind.CREATE_TICKETS.value} {token}",
            ", ".join(t.id for t in proposals),
            "```",
        ]
    return "\n".join(lines)


def fast_track_plan(token: str, url: str, revision: int, spec_token: str) -> str:
    return "\n".join(
        [
            f"## Plan v{revision:03d} approved with the specification (fast track): {token}",
            f"[Plan v{revision:03d}]({url}) was written with the specification and approved with "
            f"`{spec_token}`, so there is no separate plan review. **Moving into "
            f"{_into(Status.PLANNING, Action.USE_APPROVED_PLAN)}**: development starts by itself.",
        ]
    )


def plan_gate(
    token: str,
    url: str,
    footprint_url: str,
    revision: int,
    summary: str,
    approvers: str,
    overlap: list[OverlapFinding],
) -> str:
    lines = [
        f"## Plan v{revision:03d} ready for review: {token}",
        _ready_to_move(Status.PLAN_REVIEW, Action.APPROVE_PLAN, "once the plan is approved"),
        f"[Read plan v{revision:03d}]({url}) · [change footprint]({footprint_url})",
        "",
        summary,
    ]
    if overlap:
        lines += ["", "**Overlap with other in-flight work** (advisory):"]
        lines += [
            f"- {o.warning_id}: {o.kind.value} with {o.other} ({', '.join(o.details[:3])})" for o in overlap
        ]
    lines += [
        "",
        f"**To approve** ({approvers}): add this comment, then choose **Approve plan** "
        f"{_moves(Status.PLAN_REVIEW, Action.APPROVE_PLAN)}.",
        "```",
        approve_template(DecisionKind.APPROVE_PLAN, token),
        "```",
        "**To request changes**: comment, then choose **Request plan changes** "
        f"{_moves(Status.PLAN_REVIEW, Action.REQUEST_PLAN_CHANGES)}.",
        "```",
        change_template(DecisionKind.CHANGE_PLAN, token),
        "```",
    ]
    return "\n".join(lines)


def questions(round_token: str, draft_url: str, qs: list[Question], who: str, stage: str) -> str:
    answers = _stage_def(stage).answers_action
    lines = [
        f"## Questions: {round_token}",
        _ready_to_move(Status.NEEDS_CLARIFICATION, answers, "once these questions are answered"),
        f"{STAGE_TITLES.get(stage, stage)} needs answers before it can continue. "
        f"[Current draft]({draft_url}).",
        "",
    ]
    for q in qs:
        lines.append(f"- **{q.id}** {q.question}" + (f" _(why: {q.rationale})_" if q.rationale else ""))
    lines += [
        "",
        f"**Who answers**: {who}. Copy the template below into one or more comments, answer each "
        f"question, then choose **Submit {STAGE_TITLES.get(stage, stage).lower()} answers** "
        f"{_moves(Status.NEEDS_CLARIFICATION, answers)}.",
        "```",
        answer_template(round_token, [q.id for q in qs]),
        "```",
        "A comment alone does not restart work; the Submit answers action does.",
    ]
    return "\n".join(lines)


def blocked(stage: str, reason: str, action: str, resume_stage: str) -> str:
    resume = _stage_def(resume_stage).resume_action
    return "\n".join(
        [
            f"## Blocked during {STAGE_TITLES.get(stage, stage).lower()}",
            _ready_to_move(Status.BLOCKED, resume, "once the blocker below is resolved"),
            f"**Reason**: {reason}",
            "",
            f"**Next action**: {action}",
            f"When resolved, choose **Resume {STAGE_TITLES.get(resume_stage, resume_stage).lower()}** "
            f"{_moves(Status.BLOCKED, resume)} (only after any previous worker has stopped).",
            NOTE_HINT,
        ]
    )


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _resolution_body(
    summary: str, actions: list[str], decisions: list[dict[str, str]], follow_ups: list[str], developer: str
) -> list[str]:
    """The part of a resolution comment shared by both outcomes: what Claude did and who decided.

    ``decisions`` are dicts with id, question, decision, decided_by ("developer" or "claude") and
    basis (how the coordinator knows: asked in the session, told Claude, or only Claude's say-so).
    """
    lines = [summary.strip(), ""]
    lines.append("**What Claude did**")
    lines += [f"{n}. {a}" for n, a in enumerate(actions, 1)] or ["- No changes were made."]
    lines += ["", "**Decisions**"]
    if decisions:
        lines += ["| | Decision | Decided by | How we know |", "|---|---|---|---|"]
        for d in decisions:
            who = developer if d["decided_by"] == "developer" else "Claude"
            lines.append(
                f"| {d['id']} | **{_cell(d['question'])}** {_cell(d['decision'])} | {_cell(who)} | "
                f"{_cell(d.get('basis', ''))} |"
            )
    else:
        lines.append("None: Claude made no choices that needed deciding.")
    if follow_ups:
        lines += ["", "**Left for people**", *[f"- {f}" for f in follow_ups]]
    return lines


def resolved(
    *,
    resume_stage: str,
    run_id: str,
    worker_id: str,
    summary: str,
    actions: list[str],
    decisions: list[dict[str, str]],
    follow_ups: list[str],
    developer: str,
    questions_asked: int,
) -> str:
    title = STAGE_TITLES.get(resume_stage, resume_stage)
    ready = STATUS_NAMES[_stage_def(resume_stage).ready]
    lines = [
        f"## Blocker resolved: {title.lower()} resumes",
        f"**Back in {ready}**: {title.lower()} starts again by itself within a minute. "
        "Nothing to do in Jira.",
        "",
        *_resolution_body(summary, actions, decisions, follow_ups, developer),
        "",
        f"Claude asked {developer} {questions_asked} question{'s' if questions_asked != 1 else ''} "
        f"in the session. {_session_line(run_id, worker_id)}",
        NOTE_HINT,
    ]
    return "\n".join(lines)


def unresolved(
    *,
    resume_stage: str,
    run_id: str,
    worker_id: str,
    reason: str,
    actions: list[str],
    decisions: list[dict[str, str]],
    follow_ups: list[str],
    developer: str,
    questions_asked: int,
) -> str:
    title = STAGE_TITLES.get(resume_stage, resume_stage).lower()
    resume = _stage_def(resume_stage).resume_action
    lines = [
        "## Blocker not resolved",
        f"**Back in Blocked**: {reason.strip()}",
        "",
        *_resolution_body("What was found and tried:", actions, decisions, follow_ups, developer),
        "",
        f"Claude asked {developer} {questions_asked} question{'s' if questions_asked != 1 else ''} "
        f"in the session. {_session_line(run_id, worker_id)}",
        f"When the cause is dealt with, choose **Resume {title}** {_moves(Status.BLOCKED, resume)}, or "
        f"**Request resolution** {_moves(Status.BLOCKED, Action.REQUEST_RESOLUTION)} to try again.",
        NOTE_HINT,
    ]
    return "\n".join(lines)


def waiting(stage: str, reason: str, action: str) -> str:
    return "\n".join(
        [
            f"## Waiting before {STAGE_TITLES.get(stage, stage).lower()}",
            f"**Stays in {STATUS_NAMES[_stage_def(stage).ready]}**: "
            f"{STAGE_TITLES.get(stage, stage).lower()} starts by itself within a minute once the "
            "action below is done. Do not move the ticket.",
            f"**Not started**: {reason}",
            f"**Next action**: {action}",
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
            f"**Why**: {why}",
            f"Nothing is needed in Jira. The work so far is kept and {title.lower()} continues "
            f"automatically on `{worker_id}` as soon as Claude works again (the coordinator checks "
            "every few minutes). New tickets wait too.",
            f"**To speed it up**: {faster}",
        ]
    )


def internal_error(stage: str, run_id: str, worker_id: str, key: str) -> str:
    # The error text itself stays in the coordinator log: it can name local paths or carry
    # fragments of data that do not belong on a ticket.
    return "\n".join(
        [
            f"## {STAGE_TITLES.get(stage, stage)} stopped: coordinator error",
            "The coordinator hit an internal error and stopped this run. Nothing else was "
            f"published and the ticket stays where it is. {_session_line(run_id, worker_id)}",
            f"**Next action**: on `{worker_id}`, run `coordinator recover {key} --resume` to continue "
            "from where it stopped. `coordinator logs` shows the error.",
        ]
    )


def _checks_table(results: Iterable[CheckResult]) -> list[str]:
    rows = ["| Check | Target | Result | Commit |", "|---|---|---|---|"]
    for r in results:
        sha = (r.tree_sha or r.sha or "")[:12]
        name = f"[{r.name}]({r.url})" if r.url else r.name
        rows.append(f"| {name} | {r.source}/{r.target} | {r.conclusion} | {sha} |")
    return rows


def code_gate(
    code_token: str,
    accept_token: str,
    pr_url: str,
    candidate: str,
    review_url: str,
    verification_url: str,
    checks: list[CheckResult],
    findings: list[Finding],
    unverified: list[str],
    overlap: list[OverlapFinding],
    reviewers: str,
    provenance: str = "",
    *,
    base: str = "main",
    merge_conflicts: list[dict[str, Any]] | None = None,
    deviations: list[DeviationRecord] | None = None,
    approvers_only: bool = True,
    claude_resolves: bool = False,
    reproduction: dict[str, Any] | None = None,
) -> str:
    devs = deviations or []
    lines = [
        f"## Candidate ready for code review: {code_token}",
        _ready_to_move(Status.CODE_REVIEW, Action.APPROVE_CODE, "once the code is approved")
        + f" After that, **Accept delivery** moves it into "
        f"**{_into(Status.ACCEPTANCE_REVIEW, Action.ACCEPT_DELIVERY)}**.",
        *(
            [
                f"**It differs from the approved specification in {len(devs)} "
                f"way{'s' if len(devs) != 1 else ''} that work**: decide whether each deviation "
                "below is acceptable."
            ]
            if devs
            else []
        ),
        f"PR: {pr_url} · candidate `{candidate}`",
        f"[Independent review]({review_url}) · [Verification report]({verification_url})",
        "",
        *_checks_table(checks),
    ]
    if provenance:
        lines += ["", f"CI integration provenance: {provenance}"]
    lines += reproduction_lines(reproduction, base)
    lines += conflicts_section(merge_conflicts or [], base, claude_resolves=claude_resolves)
    lines += deviations_section(devs, code_token, Status.CODE_REVIEW, approvers_only=approvers_only)
    if findings:
        lines += ["", "**Non-blocking findings**:", *_by_author(findings, 15)]
    if unverified:
        lines += [
            "",
            f"**Not independently verified** (check during acceptance): {', '.join(unverified)}",
        ]
    if overlap:
        lines += ["", "**Integration scrutiny requested** for overlapping work:"]
        lines += [f"- {o.warning_id}: {o.other} ({', '.join(o.details[:3])})" for o in overlap]
    lines += [
        "",
        f"**Code review** ({reviewers}): an independent human must approve the PR on GitHub at the "
        "current head, with required CI passing. Then add this comment and choose **Approve code** "
        f"{_moves(Status.CODE_REVIEW, Action.APPROVE_CODE)}.",
        "```",
        approve_template(DecisionKind.APPROVE_CODE, code_token),
        "```",
        "To request changes: comment, then choose **Request code changes** "
        f"{_moves(Status.CODE_REVIEW, Action.REQUEST_CODE_CHANGES)}. {PR_COMMENTS_HINT}",
        "```",
        change_template(DecisionKind.CHANGE_CODE, code_token),
        "```",
        "**After code approval, acceptance** (product decision against the original brief): add "
        "this comment, then choose **Accept delivery** "
        f"{_moves(Status.ACCEPTANCE_REVIEW, Action.ACCEPT_DELIVERY)}.",
        "```",
        approve_template(DecisionKind.ACCEPT_DELIVERY, accept_token),
        "```",
        "To request behavioural changes: comment, then choose **Request acceptance changes** "
        f"{_moves(Status.ACCEPTANCE_REVIEW, Action.REQUEST_ACCEPTANCE_CHANGES)}.",
        "```",
        change_template(DecisionKind.CHANGE_ACCEPTANCE, accept_token),
        "```",
        "Any new commit on the PR supersedes these tokens.",
    ]
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


def _findings(findings: list[Finding], limit: int) -> list[str]:
    return [f"- **{f.id}** ({f.severity.value}): {_clip(f.description)}" for f in findings[:limit]]


def _by_author(findings: list[Finding], limit: int) -> list[str]:
    """Reviewer findings (F1-F99) and verifier findings (F101+), which may overlap."""
    review = [f for f in findings if int(f.id[1:]) < 100]
    verify = [f for f in findings if int(f.id[1:]) >= 100]
    lines: list[str] = []
    for title, group in (("reviewer", review), ("verifier", verify)):
        if group:
            lines += [f"From the {title}:", *_findings(group, limit)]
    return lines


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


def deviations_section(
    devs: list[DeviationRecord], code_token: str, here: Status, *, approvers_only: bool = True
) -> list[str]:
    """Working differences from the approved specification: a question, never a failure.

    Accepting one rewrites the specification (no new refinement round); rejecting one sends it
    to development. One left undecided is never changed back, but release preparation waits.
    """
    if not devs:
        return []
    accept = f"{DecisionKind.ACCEPT_DEVIATIONS.value} {code_token}"
    who = " (an approver)" if approvers_only else ""
    lines = [
        "",
        "**Deviations from the approved specification** (not failures; is each one acceptable?):",
        *_deviation_lines(devs),
    ]
    if here is Status.CODE_REVIEW:
        return [
            *lines,
            f"**If they are acceptable**{who}: add this comment, then choose **Submit follow-up "
            f"changes** {_moves(Status.CODE_REVIEW, Action.SUBMIT_FOLLOW_UP)}. Claude rewrites the "
            "specification to include them and publishes it as the approved revision, without a new "
            "refinement round. The code is not reviewed again: the ticket comes back to Code review "
            "with the same tokens. To accept only some, list their IDs on the lines below it.",
            "```",
            accept,
            "```",
            "**If not**: add this comment with what to do about each one, choose **Request code "
            f"changes** {_moves(Status.CODE_REVIEW, Action.REQUEST_CODE_CHANGES)}, then **Submit "
            "implementation changes**. A development session changes the code to follow the "
            "specification and the new candidate is verified.",
            "```",
            f"{DecisionKind.CHANGE_CODE.value} {code_token}",
            f"{devs[0].id}: <follow the specification: ...>",
            "```",
            "Release preparation does not start while a deviation is undecided.",
        ]
    return [
        *lines,
        f"**If they are acceptable**{who}: add this comment before choosing what happens "
        "next (list IDs on the lines below it to accept only some). Claude rewrites the "
        "specification to include them before development or verification runs again, without a "
        "new refinement round.",
        "```",
        accept,
        "```",
        "**If not**: name each one with what to do in the `SUBMIT CHANGES` comment (for example "
        f"`{devs[0].id}: follow the specification`) and choose **Submit implementation changes**: "
        "development changes the code to follow the specification. A deviation nobody names is "
        "left as it is.",
    ]


def spec_amended(
    revision: int,
    url: str,
    accepted: list[DeviationRecord],
    summary: str,
    next_steps: list[str],
) -> str:
    lines = [
        f"## Specification v{revision:03d}: accepted deviations included",
        f"[Read specification v{revision:03d}]({url}) (pinned to the exact commit). An approver "
        "accepted the deviations below, so this revision is the approved specification from now "
        "on, without a new refinement or planning round.",
        "",
        *_deviation_lines(accepted),
    ]
    if summary:
        lines += ["", f"**What changed in the specification**: {_clip(summary)}"]
    return "\n".join([*lines, *next_steps])


def back_to_code_review(
    code_token: str,
    accept_token: str,
    candidate_no: int,
    remaining: list[DeviationRecord],
    *,
    approvers_only: bool = True,
) -> list[str]:
    """After accepting deviations from Code review: the same candidate and tokens carry on."""
    return [
        "",
        _ready_to_move(Status.CODE_REVIEW, Action.APPROVE_CODE, "once the code is approved")
        + f" Candidate c{candidate_no} is unchanged, so the code review comment above still "
        f"applies with the same tokens: `{approve_template(DecisionKind.APPROVE_CODE, code_token)}` "
        f"then **Approve code**, and `{approve_template(DecisionKind.ACCEPT_DELIVERY, accept_token)}` "
        "then **Accept delivery**.",
        *deviations_section(remaining, code_token, Status.CODE_REVIEW, approvers_only=approvers_only),
    ]


def reproduction_lines(rep: dict[str, Any] | None, base: str) -> list[str]:
    """A bug fix's regression tests on the base branch without the fix (reported, never a failure)."""
    if not rep:
        return []
    tests = ", ".join(rep.get("tests", [])[:8])
    check = rep.get("check", "")
    state = rep.get("state")
    if state == "reproduced":
        text = (
            f"**Bug reproduced**: with only this candidate's tests ({tests}) on `{base}`, the `{check}` "
            "check fails, so the tests catch the bug this candidate fixes."
        )
    elif state == "not_reproduced":
        text = (
            f"**Bug not reproduced**: this candidate's tests ({tests}) also pass on `{base}` without "
            "the fix, so they may not catch the bug. Look at the regression test during review."
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
    lines = ["", "**Merge conflicts to resolve when merging** (flagged, not a failure):"]
    for c in conflicts:
        paths = ", ".join(c["paths"])
        if c["with"] == base:
            lines.append(f"- the latest `{base}` (`{str(c['sha'])[:12]}`): {paths}")
        else:
            lines.append(
                f"- {c['with']}'s candidate `{str(c['sha'])[:12]}` (not merged yet): {paths}. "
                f"Whichever of the two merges second resolves it."
            )
    lines.append(
        "The integration checks ran without the conflicting changes. Resolve the conflict in the "
        "pull request when you merge it (for example with GitHub's Resolve conflicts). Release "
        "verification accepts the approved candidate plus that merge and lists the files it "
        "changed for you to check."
    )
    if claude_resolves:
        lines.append(
            f"Or have Claude do it: the next development run (any change request) first merges the "
            f"latest `{base}` and resolves the conflict with `{base}` in a short session of its own. "
            "If it cannot, the merge is left out and the conflict stays flagged."
        )
    return lines


def verification_failed(
    code_token: str,
    pr_url: str,
    candidate_no: int,
    candidate: str,
    review_url: str,
    verification_url: str,
    checks: list[CheckResult],
    findings: list[Finding],
    problems: list[str],
    *,
    key: str = "",
    base: str = "main",
    merge_conflicts: list[dict[str, Any]] | None = None,
    deviations: list[DeviationRecord] | None = None,
    approvers_only: bool = True,
    claude_resolves: bool = False,
) -> str:
    serious = [f for f in findings if f.severity in (Severity.BLOCKER, Severity.MAJOR)]
    minor = [f for f in findings if f not in serious]
    failed = [c for c in checks if c.conclusion != "passed"]
    why = [f"- **R{i}**: {p}" for i, p in enumerate(problems, 1)]
    if serious:
        why.append(
            f"- {len(serious)} blocker/major finding{'s' if len(serious) != 1 else ''}: "
            + ", ".join(f.id for f in serious)
        )
    if failed:
        checks_line = f"Failed checks: {', '.join(f'{c.name} ({c.target})' for c in failed)}."
    else:
        checks_line = "All coordinator and CI checks passed." if checks else ""
    lines = [
        f"## Verification failed for candidate c{candidate_no} `{candidate[:12]}`",
        _ready_to_move(
            Status.CHANGES_REQUESTED, Action.SUBMIT_IMPLEMENTATION_CHANGES, "to fix it (the usual next step)"
        )
        + " The other options are below.",
        f"PR: {pr_url} · [Independent review]({review_url}) · [Verification report]({verification_url})",
        "",
        "**Why it failed**:",
        *why,
        *([checks_line] if checks_line else []),
        "",
        "**What to do next** (pick one):",
        "- **Fix it in this ticket**: choose **Submit implementation changes** "
        f"{_moves(Status.CHANGES_REQUESTED, Action.SUBMIT_IMPLEMENTATION_CHANGES)}. Development gets "
        f"every R- and F-item below, and the PR's unresolved review conversations as G-items, and "
        f"publishes candidate c{candidate_no + 1}, which is reviewed and verified again.",
    ]
    if findings:
        lines += [
            "To limit the findings it works on, first add this comment with the F-IDs to fix "
            "and a note on each (R-items are always included):",
            "```",
            f"{DecisionKind.SUBMIT_CHANGES.value} {code_token}",
            *(f"{f.id}: <what to do>" for f in (serious or minor)[:3]),
            "```",
        ]
    lines += [
        "- **Change what is being built**: choose **Revise scope** "
        f"{_moves(Status.CHANGES_REQUESTED, Action.REVISE_SCOPE)}.",
        "- **Verify the same candidate again**: choose **Submit follow-up changes** "
        f"{_moves(Status.CHANGES_REQUESTED, Action.SUBMIT_FOLLOW_UP)}. The code does "
        "not change, so this only helps when the cause was outside this candidate (a flaky check, "
        f"or {base} or another ticket changed since).",
        NOTE_HINT,
    ]
    if serious:
        lines += ["", "**Blocking findings** (blocker/major):", *_by_author(serious, 15)]
    if minor:
        lines += ["", "**Other findings** (minor/info):", *_by_author(minor, 15)]
    lines += deviations_section(
        deviations or [], code_token, Status.CHANGES_REQUESTED, approvers_only=approvers_only
    )
    lines += conflicts_section(merge_conflicts or [], base, claude_resolves=claude_resolves)
    lines += [
        "",
        *_checks_table(checks),
        "",
        "Reviewer and verifier work independently, so their findings can overlap. Full text is "
        f"in the linked reports. On the developer's machine `delivery inspect {key}` shows this "
        "outcome with the check logs and Claude session logs.",
    ]
    return "\n".join(lines)


def acceptance_ready(
    key: str,
    accept_token: str,
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
    """Posted when a ticket enters Acceptance review: how to try the candidate and what to check."""
    lines = [
        f"## Ready for acceptance: candidate c{candidate_no}",
        _ready_to_move(Status.ACCEPTANCE_REVIEW, Action.ACCEPT_DELIVERY, "once the delivery is accepted"),
        "The code is approved. Acceptance is the product decision: try it and check that it does "
        "what the brief asked for.",
        "",
        "**Try it locally**:",
    ]
    if local_app:
        lines.append(
            f"- On `{worker_id}`: the coordinator runs this exact candidate and opens it in the browser "
            f"there (`delivery preview {key}` opens it again)."
        )
    if try_command:
        lines.append(
            f"- On your own machine, with the delivery tools installed: `delivery try {key}` runs "
            f"candidate c{candidate_no} and opens it in your browser."
        )
    lines.append(f"- The code: {pr_url} at `{candidate[:12]}`.")
    if guide:
        link = f" ([full guide]({guide_url}))" if guide_url else ""
        lines += ["", f"**What to check**, written by verification{link}:", "", guide.strip()]
    lines += [
        "",
        "**To accept**: add this comment, then choose **Accept delivery** "
        f"{_moves(Status.ACCEPTANCE_REVIEW, Action.ACCEPT_DELIVERY)}.",
        "```",
        approve_template(DecisionKind.ACCEPT_DELIVERY, accept_token),
        "```",
        "**To ask for changes**: add this comment with numbered items, then choose **Request "
        f"acceptance changes** {_moves(Status.ACCEPTANCE_REVIEW, Action.REQUEST_ACCEPTANCE_CHANGES)}.",
        "```",
        change_template(DecisionKind.CHANGE_ACCEPTANCE, accept_token),
        "```",
    ]
    return "\n".join(lines)


def release_gate(
    release_token: str,
    url: str,
    revision: int,
    candidate: str,
    approvers: str,
    environment: str,
    note: str = "",
) -> str:
    return "\n".join(
        [
            f"## Release proposal v{revision:03d} ready: {release_token}",
            _ready_to_move(Status.RELEASE_REVIEW, Action.APPROVE_RELEASE, "once the proposal is approved")
            + " Once the PR is merged, the coordinator records the release and moves it into "
            f"**{_into(Status.READY_RELEASE, Action.RECORD_RELEASE)}**.",
            f"[Read release proposal]({url}) · accepted candidate `{candidate}`",
            "",
            *([note, ""] if note else []),
            f"**To approve** ({approvers}): comment, then choose **Approve release** "
            f"{_moves(Status.RELEASE_REVIEW, Action.APPROVE_RELEASE)}.",
            "```",
            approve_template(DecisionKind.APPROVE_RELEASE, release_token),
            "```",
            "To request changes: comment, then choose **Request release changes** "
            f"{_moves(Status.RELEASE_REVIEW, Action.REQUEST_RELEASE_CHANGES)}.",
            "```",
            change_template(DecisionKind.CHANGE_RELEASE, release_token),
            "```",
            "**After approval, a human merges the PR.** That is the release: the coordinator reads "
            f"the merge commit from GitHub, records it in `{environment}` (**Record release**) and "
            "verifies it. There is nothing to record by hand.",
            "The coordinator never merges or deploys.",
        ]
    )


def done(release_commit: str, environment: str, url: str, provenance: str) -> str:
    return "\n".join(
        [
            "## Release verified: Done",
            "Nothing more to do on this ticket.",
            f"Released commit `{release_commit}` in `{environment}`. [Release verification]({url}).",
            f"Provenance: {provenance}",
        ]
    )


def candidate_ready(
    candidate_no: int,
    sha: str,
    pr_url: str,
    summary: str,
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
        f"**Moving into {_into(Status.DEVELOPING, Action.COMPLETE_DEVELOPMENT)}**: independent review "
        f"and verification start {starts}. Nothing to do yet.",
        f"PR: {pr_url} · commit `{sha}`",
        "",
        summary,
    ]
    if resolved:
        lines += [
            "",
            f"The latest `{base}` (`{str(resolved['sha'])[:12]}`) was merged into this candidate first. "
            f"Claude resolved the conflicts in {', '.join(resolved['paths'])}; review and verification "
            "check the result like any other change.",
        ]
    if merge_conflicts:
        lines += [
            "",
            f"The latest `{base}` was not merged into this candidate because it conflicts in "
            f"{', '.join(p for c in merge_conflicts for p in c['paths'])}. Resolve that when "
            "merging the pull request.",
        ]
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
        f"Commit `{sha}`: {url}",
        "",
        "The developer kept the development session open after it finished and asked Claude for:",
        *[f"- {r}" for r in requests],
        "",
        "The coordinator pushed the change as a new candidate. Earlier code and acceptance "
        "approvals do not cover it.",
    ]
    lines.append(
        f"The ticket moved from {was_in} back to Ready for verification. Review and verification "
        f"run again; {starts}."
        if moved
        else f"Review and verification run on the latest candidate; {starts}."
    )
    return "\n".join(lines)


def follow_up_revision(stage: str, replaces: str, requests: list[str]) -> str:
    """The summary of a revision published from a session left open after its stage."""
    return "\n".join(
        [
            f"**Follow-up revision.** It supersedes {replaces}: an approval of that revision does not "
            "cover this one.",
            f"Changed in the open {STAGE_TITLES.get(stage, stage).lower()} session, as the developer asked:",
            *[f"- {r}" for r in requests],
        ]
    )


def overlap_warning(f: OverlapFinding, assignees: dict[str, str | None], here: str) -> str:
    other = f.other if f.ticket == here else f.ticket
    rev = "; ".join(
        f"{k}: plan v{v.get('plan')} @ {str(v.get('commit'))[:12]}" for k, v in sorted(f.revisions.items())
    )
    lines = [
        f"## Overlap warning: {f.warning_id}",
        f"{here} and {other} ({assignees.get(other) or 'unassigned'}) overlap: **{f.kind.value}**.",
        *[f"- {d}" for d in f.details[:20]],
        f"Inspected: {rev}.",
        "",
        "Work continues on both tickets. Verification tests each candidate together with the "
        "other's, and any merge conflict is flagged to resolve when merging.",
    ]
    if f.severity is OverlapSeverity.HIGH:
        lines.append(
            "This one is higher risk: both change the same shared interface, schema or migration, "
            "or one ticket depends on the other. Agree which merges first; to rethink one "
            "ticket choose **Revise scope**, and to tell its next Claude session about the "
            "other add a comment starting with `FOR CLAUDE`."
        )
    return "\n".join(lines)


def handover(stage: str, state: str, artefacts: dict[str, str], next_action: str) -> str:
    lines = [f"## Handover checkpoint ({STAGE_TITLES.get(stage, stage)})"]
    if stage in STAGE_TITLES and state.startswith("blocked"):
        resume = _stage_def(stage).resume_action
        lines.append(
            _ready_to_move(Status.BLOCKED, resume, "once reassigned")
            + f" The new owner chooses **Resume {STAGE_TITLES[stage].lower()}**."
        )
    lines += [f"State: {state}", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(artefacts.items())]
    lines += ["", f"**Next action**: {next_action}"]
    return "\n".join(lines)
