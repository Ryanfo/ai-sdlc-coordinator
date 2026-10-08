# Plan: moving the ticket is the approval

> **Superseded in part (8 Oct 2026):** release preparation, release review and release verification no longer exist. The release is the human merge of the PR: Accept delivery moves the ticket to Ready for release, and the coordinator checks the merge and moves it to Done. References below to those stages, their statuses, the release proposal, `RECORD RELEASE` and `amend-spec` are historical. See `docs/jira-workflow-setup.md` and `docs/user-guide.md`.

Status: built (2026-10-07). See "Decisions are moves" in Implementation_Decisions.md.

## Goal

Today a human decision needs two things: a token comment (`APPROVE PLAN SDLC-12-PLAN-v002`)
**and** the matching Jira transition. Without the comment the ticket waits in its Ready status
and the coordinator posts "add the comment" (see `gates.evaluate_human_gate`, the
`WAITING` branch). After this change:

- **Approving is moving the ticket.** Approve specification / Approve plan / Approve code /
  Accept delivery / Approve release, done by an allowed person after the revision was
  published, is the whole decision. No comment.
- **Asking for changes is the move too.** Choose Request ... changes; a plain comment saying
  what to change is welcome but optional (no token, no `F1:` numbering). With no comment Claude
  asks what to change.
- **Answering questions is free text**, then Submit answers. No `ANSWERS` template.
- Coordinator comments stop telling people to paste decision lines, so the Jira thread loses
  the code blocks and the human approval comments.

## Why this is safe without the comment

The comment did three jobs. Each is already covered, or can be, by the transition itself:

| Job the comment did | What covers it now |
| --- | --- |
| Who decided | The transition's author in Jira history (`StatusChange.author_account_id`), checked against `[approvals] jira_account_ids` (empty = anyone) exactly as today. |
| Which revision was approved | The transition time: it must come after the gate was published (`entry.created >= gate.published_at`, already checked). The current, non-superseded gate is the one approved. For code, the PR head must still equal the verified candidate (`_code_evidence`, unchanged). |
| Not automation / not the coordinator | The coordinator never performs a human route (`workflow.coordinator_route` refuses it), so a human route in history was made by a person even when the coordinator uses the same Jira account. Transitions with no author are still rejected. |

One thing the token did that time cannot: prove the person looked at *that* revision when a
kept-open spec/plan/release session publishes a new revision while the ticket sits in review.
Per "flag, don't block", the plan is to state the approved revision in the next stage's
"started" comment ("Approved: plan v003") and add a warning when a newer revision was published
in the few minutes before the move. No block.

## Decisions (confirmed 2026-10-07)

1. **Change requests: the move alone.** Request ... changes needs no comment. Any plain human
   comments since the revision was published (and, for code, unresolved PR conversations) go to
   Claude as the feedback. If there are none, the stage still starts and Claude finds out what
   to change: in an interactive session it asks the developer (AskUserQuestion); otherwise it
   returns questions and the ticket goes to Needs clarification (answered in free text, below).
   Nothing waits on a comment.
2. **Answers to questions: free text.** Any human comment after the questions were posted is
   the answer set; `Q1:` prefixes optional, Claude maps free text to questions. The
   `ANSWERS <round token>` line goes. Submit answers with no comment → the stage resumes and
   Claude asks again only if it still can't proceed.
3. **Deviations: approving code accepts them.** Approve code (or Accept delivery) accepts every
   open deviation not named in a change request. `ACCEPT DEVIATIONS` disappears, and release
   preparation no longer waits on "undecided" deviations. To reject one, request changes and
   mention it (D2) in a comment.
4. **Comments left during review, then approved, reach Claude** as reviewer notes for the next
   stage (context, not change requests), so "approved, but call it X" isn't lost.

Unchanged on purpose: `FOR CLAUDE` notes, `FOR CLAUDE project` guidance, `CREATE TICKETS`
(a request, not an approval) and the optional `RECORD RELEASE` override (the release is normally
read from the GitHub merge already). Jira needs **no workflow changes**: same statuses and
transitions.

## Design

### Gate evaluation (`src/delivery/gates.py`)

Rewrite `evaluate_human_gate` around the transition:

- Inputs: gate, entry (the status change), review/approve/change status IDs, approvers,
  comments (only for change requests).
- Keep: superseded gate → REJECTED; wrong source status → REJECTED; transition before
  publication → REJECTED; no author → REJECTED; author not allowed → REJECTED.
- Approve target → APPROVED with transition evidence. No comment lookup.
- Change target → CHANGES_REQUESTED, with the plain human comments since
  `gate.published_at` as items (new `feedback.review_comments`; may be empty). The WAITING
  outcome and `allow_empty_change` go.
- Remove: `approve_kind`/`change_kinds` parameters, the CONFLICT outcome (approval and change
  comments both present can no longer happen: the move decides), the "edited approval comment"
  check, `excluded_comment_ids`.

`DecisionEvidence` (`models.py`) drops `comment_id/author/digest/updated`; transition fields
become required. Records already stored in the Jira property still carry the old keys and the
model is `extra="forbid"`, so add a `mode="before"` validator that discards those four keys
(one documented shim; old gates load, new ones never write them).

### Comment parsing (`src/delivery/feedback.py`)

- New `review_comments(comments, since, allowed_authors)` → human comments after `since`, oldest
  first, excluding coordinator comments (`delivery-op:` marker), `FOR CLAUDE project`,
  `CREATE TICKETS` and `RECORD RELEASE`. Each becomes one feedback item `F1, F2...` in order;
  a comment that already uses `F1:` / `D2:` lines keeps them (so D-items still name
  deviations).
- Delete: approve/change/submit/accept-deviations/revise-scope `DecisionKind`s,
  `approve_template`, `change_template`, `collect_feedback` (replaced), `CANDIDATE_TOKEN`,
  `ANSWERS`, `ROUND_TOKEN`, `answer_template`, `collect_answers` (replaced by
  `review_comments` since the questions were posted).
- `parse_decision`/`decisions` remain only for `CREATE TICKETS` and `RECORD RELEASE`.

### Intake (`src/delivery/intake.py`)

- `_JIRA_GATES` / `_gate_route` / `_gate_eval`: drop the decision kinds.
- `catch_up_gates`: an approve transition by an allowed person after publication approves the
  pending gate (no comment).
- `_redecide` (Blocked after an invalid decision → Resume): the approver's Resume is the
  decision; drop the comment lookup.
- `_req_scope_revision`: plain comments since entering Changes requested.
- `_req_implementation_changes`: plain comments since entering Changes requested replace
  `SUBMIT CHANGES` selection. Everything (R-items, F-items, G-items from the PR, plus the new
  comments) goes to development; a comment can say "only F2" and Claude follows it. The
  "no feedback items selected" WAIT goes: with no items, development starts and asks.
- `_req_acceptance`, `_req_release_record`: approval part becomes transition-only.
- `_deviation_decisions`: decision 3 — on code approval mark open deviations accepted unless a
  change request named them; release preparation's wait goes.
- `_resume` (Needs clarification): comments since the questions were posted are the answers;
  none is not a WAIT (the stage resumes). The "edited after Submit answers" check goes.
- `_resolution_request`: message about "comment with the current token" goes.

### Stages, skills and deviations

- Stage skills (`refine-ticket`, `plan-ticket`, `implement-ticket`, `prepare-release` SKILL.md and
  `references/stage-contract.md`): a change request may arrive with no items; the skill then
  asks the developer what to change (interactive) or returns questions, never guesses. Answers
  arrive as free text keyed to the round, not by Q-ID.

- `stages.publish_amendment`: evidence is the Approve code / Accept delivery transition, not an
  `ACCEPT DEVIATIONS` comment; provenance header `accepted_in_comment` → `accepted_by_transition`.
- `deviations.accepted` → replaced by "open deviations of the approved candidate minus those
  named in change requests". Module docstring rewritten.
- `Intake.selected` for approvals: the reviewer-note comments (decision 4) instead of the token
  comment.

### Jira comments (`src/delivery/comments.py`)

Rewrite each gate comment to lead with the move, e.g. plan:

> **Plan v002 ready for review.** To approve, move it to **Ready for development**
> (Approve plan). To change it, comment what you want, then choose **Request plan changes**.
> [Plan v002](…) · [change footprint](…)

Applies to `spec_gate`, `findings_gate`, `plan_gate`, `code_gate` (keeps the "approve the PR on
GitHub first" line), `acceptance_ready`, `release_gate`, `questions`, `deviations_section`,
`back_to_code_review`, `verification_failed` (drop the `SUBMIT CHANGES` block). Tokens leave the
headings; they stay internal (`GateRecord.token` still names gates, supersession and
publication IDs). `started` names the approved revision and carries the "newer revision" warning.

### Everything else that tells people to comment

- `resolution.py`: the "Decision tokens now" context and `check_next_steps`: a next step that
  is an approval is a Jira action, not a comment; comment steps only for `RECORD RELEASE` /
  `CREATE TICKETS`. `plugins/delivery/skills/resolve-blocker/SKILL.md` and
  `diagnose-ticket/SKILL.md` updated to match.
- `explain.py`, `reminders.py`, `console`/`office` texts: any "waiting for the comment" wording.
- Docs: `docs/human-templates.md` (shrinks to FOR CLAUDE, CREATE TICKETS, RECORD RELEASE),
  `README.md`, `docs/pilot-runbook.md`, `docs/jira-workflow-setup.md`; a new entry in
  `docs/design/Implementation_Decisions.md` recording this decision and why. The original
  Build Handoff stays as history with a pointer to the new entry.

## Execution order

1. **Core**: `feedback.review_comments`, new `evaluate_human_gate`, `DecisionEvidence` + shim;
   unit tests in `tests/unit/test_feedback_gates.py` rewritten.
2. **Intake**: gates, catch-up, redecide, change requests, implementation changes, scope
   revision, acceptance, release approval.
3. **Deviations** (decision 3), **answers** (decision 2) and the stage skills' "no items: ask" rule.
4. **Comments** and every other human-facing text; `tests/unit/test_comments.py`.
5. **Tests**: `tests/harness.py` `decide()` becomes "optionally comment, then transition";
   update ~30 integration tests; add the cases below.
6. **Dead-code sweep**: `grep` for every removed `DecisionKind`, template and token regex;
   ruff/mypy/pytest clean.
7. **Docs** and the decisions record.
8. **Live**: restart the coordinator, then walk one pilot ticket on SDLC from refinement to
   Done using moves only; confirm the Jira thread has no human decision comments.

## New tests

- Approve move with no comment → APPROVED, next stage starts.
- Approve by a non-approver (when a list is set) → REJECTED/Blocked; anyone when the list is empty.
- Move before the revision was published → REJECTED.
- Gate superseded by a newer revision → that move approves the newer one; warning when it was
  published just before.
- Request changes with a plain comment → CHANGES_REQUESTED, comment text is the item; with no
  comment → the stage starts with no items and the envelope says "ask what to change"; code
  review with only PR conversations → proceeds.
- Submit answers with free-text comments → answers reach the stage; with none → stage resumes.
- Coordinator's own comments (marker) never count as feedback, even from the same account.
- Missed poll (ticket already moved past Ready) → caught up from history.
- Approve code with open deviations → accepted, spec amended before release preparation;
  deviation named in a change request → changed back.
- Old shared record with comment evidence still loads.

## Rollout

- In-flight tickets: those waiting for an approval comment start on the next poll after the
  restart; Blocked "invalid decision" tickets resume with Resume alone.
- Needs a coordinator restart (running supervisor keeps old code).
- No Jira admin work.
