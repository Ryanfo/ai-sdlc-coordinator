"""Jira comment bodies (markdown subset converted to ADF on publication).

Every comment names the exact artefact revision via an immutable commit link, the token
the human must use, a copyable template and the next human action.
"""

from __future__ import annotations

from collections.abc import Iterable

from delivery.feedback import (
    DecisionKind,
    answer_template,
    approve_template,
    change_template,
    record_release_template,
)
from delivery.models import CheckResult, Finding, Question
from delivery.overlap import OverlapFinding, Severity

STAGE_TITLES = {
    "refinement": "Refinement",
    "planning": "Planning",
    "development": "Development",
    "verification": "Verification",
    "release_preparation": "Release preparation",
    "release_verification": "Release verification",
}


def _session_line(run_id: str, worker_id: str) -> str:
    return f"Run `{run_id}` on worker `{worker_id}`."


def started(
    stage: str,
    run_id: str,
    worker_id: str,
    reason: str,
    moved_by_hand: str | None = None,
    models: dict[str, str | None] | None = None,
) -> str:
    text = (
        f"**{STAGE_TITLES.get(stage, stage)} started.** {_session_line(run_id, worker_id)}\nInput: {reason}"
    )
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


def spec_gate(token: str, url: str, revision: int, summary: str, approvers: str) -> str:
    return "\n".join(
        [
            f"## Specification v{revision:03d} ready for review: {token}",
            f"[Read specification v{revision:03d}]({url}) (pinned to the exact commit).",
            "",
            summary,
            "",
            f"**To approve** ({approvers}): add this comment, then choose **Approve specification**.",
            "```",
            approve_template(DecisionKind.APPROVE_SPEC, token),
            "```",
            "**To request changes**: add this comment with numbered items, then choose "
            "**Request specification changes**.",
            "```",
            change_template(DecisionKind.CHANGE_SPEC, token),
            "```",
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
        f"**To approve** ({approvers}): add this comment, then choose **Approve plan**.",
        "```",
        approve_template(DecisionKind.APPROVE_PLAN, token),
        "```",
        "**To request changes**: comment, then choose **Request plan changes**.",
        "```",
        change_template(DecisionKind.CHANGE_PLAN, token),
        "```",
    ]
    return "\n".join(lines)


def questions(round_token: str, draft_url: str, qs: list[Question], who: str, stage: str) -> str:
    lines = [
        f"## Questions: {round_token}",
        f"{STAGE_TITLES.get(stage, stage)} needs answers before it can continue. "
        f"[Current draft]({draft_url}).",
        "",
    ]
    for q in qs:
        lines.append(f"- **{q.id}** {q.question}" + (f" _(why: {q.rationale})_" if q.rationale else ""))
    lines += [
        "",
        f"**Who answers**: {who}. Copy the template below into one or more comments, answer each "
        f"question, then choose **Submit {STAGE_TITLES.get(stage, stage).lower()} answers**.",
        "```",
        answer_template(round_token, [q.id for q in qs]),
        "```",
        "A comment alone does not restart work; the Submit answers action does.",
    ]
    return "\n".join(lines)


def blocked(stage: str, reason: str, action: str, resume_stage: str) -> str:
    return "\n".join(
        [
            f"## Blocked during {STAGE_TITLES.get(stage, stage).lower()}",
            f"**Reason**: {reason}",
            "",
            f"**Next action**: {action}",
            f"When resolved, choose **Resume {STAGE_TITLES.get(resume_stage, resume_stage).lower()}** "
            "(only after any previous worker has stopped).",
        ]
    )


def waiting(stage: str, reason: str, action: str) -> str:
    return "\n".join(
        [
            f"## Waiting before {STAGE_TITLES.get(stage, stage).lower()}",
            f"**Not started**: {reason}",
            f"**Next action**: {action}",
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
) -> str:
    lines = [
        f"## Candidate ready for code review: {code_token}",
        f"PR: {pr_url} · candidate `{candidate}`",
        f"[Independent review]({review_url}) · [Verification report]({verification_url})",
        "",
        *_checks_table(checks),
    ]
    if provenance:
        lines += ["", f"CI integration provenance: {provenance}"]
    if findings:
        lines += ["", "**Non-blocking findings**:"]
        lines += [f"- {f.id} ({f.severity.value}): {f.description[:300]}" for f in findings[:15]]
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
        "current head, with required CI passing. Then add this comment and choose **Approve code**.",
        "```",
        approve_template(DecisionKind.APPROVE_CODE, code_token),
        "```",
        "To request changes: comment, then choose **Request code changes**.",
        "```",
        change_template(DecisionKind.CHANGE_CODE, code_token),
        "```",
        "**After code approval, acceptance** (product decision against the original brief): add "
        "this comment, then choose **Accept delivery**.",
        "```",
        approve_template(DecisionKind.ACCEPT_DELIVERY, accept_token),
        "```",
        "To request behavioural changes: comment, then choose **Request acceptance changes**.",
        "```",
        change_template(DecisionKind.CHANGE_ACCEPTANCE, accept_token),
        "```",
        "Any new commit on the PR supersedes these tokens.",
    ]
    return "\n".join(lines)


def verification_failed(
    code_token: str,
    pr_url: str,
    candidate: str,
    review_url: str,
    verification_url: str,
    checks: list[CheckResult],
    findings: list[Finding],
    reasons: list[str],
) -> str:
    lines = [
        f"## Verification failed for candidate `{candidate[:12]}`",
        f"PR: {pr_url} · [Independent review]({review_url}) · [Verification report]({verification_url})",
        "",
        "**Why**:",
        *[f"- {r}" for r in reasons],
        "",
        *_checks_table(checks),
    ]
    if findings:
        lines += ["", "**Findings** (use these IDs when submitting changes):"]
        lines += [f"- {f.id} ({f.severity.value}): {f.description[:300]}" for f in findings[:25]]
    lines += [
        "",
        "**Next action**: review the findings. To fix within the approved scope choose **Submit "
        "implementation changes** (optionally first comment `SUBMIT CHANGES "
        f"{code_token}` with the F-IDs to address). To change scope choose **Revise scope**.",
    ]
    return "\n".join(lines)


def release_gate(
    release_token: str, url: str, revision: int, candidate: str, approvers: str, environment: str
) -> str:
    return "\n".join(
        [
            f"## Release proposal v{revision:03d} ready: {release_token}",
            f"[Read release proposal]({url}) · accepted candidate `{candidate}`",
            "",
            f"**To approve** ({approvers}): comment, then choose **Approve release**.",
            "```",
            approve_template(DecisionKind.APPROVE_RELEASE, release_token),
            "```",
            "To request changes: comment, then choose **Request release changes**.",
            "```",
            change_template(DecisionKind.CHANGE_RELEASE, release_token),
            "```",
            "**After approval, a human merges the PR and performs the release.** Then record it and "
            "choose **Record release**:",
            "```",
            record_release_template(release_token, environment),
            "```",
            "The coordinator never merges or deploys.",
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


def candidate_ready(candidate_no: int, sha: str, pr_url: str, summary: str) -> str:
    return "\n".join(
        [
            f"## Implementation candidate c{candidate_no} ready for verification",
            f"PR: {pr_url} · commit `{sha}`",
            "",
            summary,
            "",
            "Fresh independent review and verification start automatically.",
        ]
    )


def follow_up(candidate_no: int, sha: str, url: str, requests: list[str], was_in: str, moved: bool) -> str:
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
        f"The ticket moved from {was_in} back to Ready for verification; review and verification start again."
        if moved
        else "Verification of the new candidate starts automatically."
    )
    return "\n".join(lines)


def overlap_warning(f: OverlapFinding, assignees: dict[str, str | None], here: str) -> str:
    other = f.other if f.ticket == here else f.ticket
    sev = "Sequencing decision needed" if f.severity is Severity.BLOCK else "Overlap warning"
    rev = "; ".join(
        f"{k}: plan v{v.get('plan')} @ {str(v.get('commit'))[:12]}" for k, v in sorted(f.revisions.items())
    )
    lines = [
        f"## {sev}: {f.warning_id}",
        f"{here} and {other} ({assignees.get(other) or 'unassigned'}) overlap: **{f.kind.value}**.",
        *[f"- {d}" for d in f.details[:20]],
        f"Inspected: {rev}.",
        "",
    ]
    if f.severity is Severity.BLOCK:
        lines += [
            "Path comparison cannot prove independence. A human decides the order. Comment one of:",
            "```",
            f"OVERLAP {f.warning_id} PROCEED",
            f"OVERLAP {f.warning_id} WAIT {other}",
            f"OVERLAP {f.warning_id} RESCOPE",
            "```",
            "then choose **Resume** for the paused stage.",
        ]
    else:
        lines.append("Parallel work may continue. Integration scrutiny will be applied before merge.")
    return "\n".join(lines)


def handover(stage: str, state: str, artefacts: dict[str, str], next_action: str) -> str:
    lines = [f"## Handover checkpoint ({STAGE_TITLES.get(stage, stage)})", f"State: {state}", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(artefacts.items())]
    lines += ["", f"**Next action**: {next_action}"]
    return "\n".join(lines)
