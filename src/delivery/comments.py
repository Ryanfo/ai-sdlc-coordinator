"""Jira comment bodies (markdown subset converted to ADF on publication).

Every comment names the exact artefact revision via an immutable commit link, the token
the human must use, a copyable template and the next human action.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from delivery.feedback import (
    DecisionKind,
    answer_template,
    approve_template,
    change_template,
    record_release_template,
)
from delivery.models import CheckResult, Finding, Question, Severity
from delivery.overlap import OverlapFinding
from delivery.overlap import Severity as OverlapSeverity

STAGE_TITLES = {
    "refinement": "Refinement",
    "planning": "Planning",
    "development": "Development",
    "verification": "Verification",
    "release_preparation": "Release preparation",
    "release_verification": "Release verification",
}


NOTE_HINT = (
    "To guide the next Claude session, first add a comment that starts with `FOR CLAUDE` "
    "(or `FOR CLAUDE development` for one stage) followed by what it should know or do."
)


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
            NOTE_HINT,
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
    *,
    base: str = "main",
    merge_conflicts: list[dict[str, Any]] | None = None,
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
    lines += conflicts_section(merge_conflicts or [], base)
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


def conflicts_section(conflicts: list[dict[str, Any]], base: str) -> list[str]:
    """Flag textual conflicts. They are resolved when the PR is merged, never a failure."""
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
        f"PR: {pr_url} · [Independent review]({review_url}) · [Verification report]({verification_url})",
        "",
        "**Why it failed**:",
        *why,
        *([checks_line] if checks_line else []),
        "",
        "**What to do next** (pick one):",
        f"- **Fix it in this ticket**: choose **Submit implementation changes**. Development gets "
        f"every R- and F-item below and publishes candidate c{candidate_no + 1}, which is reviewed "
        "and verified again.",
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
        "- **Change what is being built**: choose **Revise scope** (back to refinement).",
        "- **Verify the same candidate again**: choose **Submit follow-up changes**. The code does "
        "not change, so this only helps when the cause was outside this candidate (a flaky check, "
        f"or {base} or another ticket changed since).",
        NOTE_HINT,
    ]
    if serious:
        lines += ["", "**Blocking findings** (blocker/major):", *_by_author(serious, 15)]
    if minor:
        lines += ["", "**Other findings** (minor/info):", *_by_author(minor, 15)]
    lines += conflicts_section(merge_conflicts or [], base)
    lines += [
        "",
        *_checks_table(checks),
        "",
        "Reviewer and verifier work independently, so their findings can overlap. Full text is "
        f"in the linked reports. On the developer's machine `delivery inspect {key}` shows this "
        "outcome with the check logs and Claude session logs.",
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


def candidate_ready(
    candidate_no: int,
    sha: str,
    pr_url: str,
    summary: str,
    *,
    merge_conflicts: list[dict[str, Any]] | None = None,
    base: str = "main",
) -> str:
    lines = [
        f"## Implementation candidate c{candidate_no} ready for verification",
        f"PR: {pr_url} · commit `{sha}`",
        "",
        summary,
    ]
    if merge_conflicts:
        lines += [
            "",
            f"The latest `{base}` was not merged into this candidate because it conflicts in "
            f"{', '.join(p for c in merge_conflicts for p in c['paths'])}. Resolve that when "
            "merging the pull request.",
        ]
    lines += ["", "Fresh independent review and verification start automatically."]
    return "\n".join(lines)


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
    sev = "Sequencing decision needed" if f.severity is OverlapSeverity.BLOCK else "Overlap warning"
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
    if f.severity is OverlapSeverity.BLOCK:
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
