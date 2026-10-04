---
name: refine-ticket
description: Draft or revise a versioned specification from a Jira brief, human answers and change requests. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

# Procedure: refine-ticket

contract_id: `delivery.refine-ticket/v1`

Read the shared contract at `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then
the envelope at `$envelope`.

## Goal

Produce specification revision `output.next_revision` for `ticket_key`, ready for a human
specification review, or ask the questions that block a usable specification.

## Steps

1. Read `brief`, `selected_comments`, every file in `attachments` and `designs` (designs and screenshots
   are part of the brief) and every file in `prior_drafts`. If a prior draft
   exists, revise it: keep what was not challenged, apply every answer and every numbered
   feedback item (`F1`...), and record each change in the revision history.
2. Read the application's standards from the working directory (`CLAUDE.md`, `docs/`) and
   enough of the codebase to describe current behaviour accurately. Do not design the
   implementation; that is the plan stage.
3. Write the specification to `output.artifact_dir/specification.md` using the template
   `${CLAUDE_PLUGIN_ROOT}/templates/specification.md` (for `work_kind: bug`, use
   `${CLAUDE_PLUGIN_ROOT}/templates/bug-specification.md` instead: steps to reproduce, actual and
   expected behaviour, and an acceptance criterion that a regression test reproduces the bug;
   read the code to confirm where the behaviour comes from, and use `linked_tickets` for what
   was meant to happen; for `work_kind: spike`, use
   `${CLAUDE_PLUGIN_ROOT}/templates/spike-specification.md`: the question to answer, why it
   matters, what a useful answer contains as acceptance criteria, and the time box. A spike is
   investigated, not built). It must contain: problem/outcome,
   scope and exclusions, numbered acceptance criteria (`AC1`, `AC2`...), non-functional
   needs, constraints, dependencies, assumptions, open questions and revision history.
   Preserve the original brief in the "Original brief" section verbatim. List every
   attachment you used under "Designs and attachments" with what it defines, and turn
   what the designs require into acceptance criteria rather than leaving it implied.
4. Decide the outcome:
   - `needs_clarification` when an answer would materially change scope or acceptance
     criteria and you cannot make a safe, explicit assumption. Still write the full draft,
     marking the open points. Number questions `Q1`, `Q2`... (restart at Q1 each round) and
     give each a one-sentence rationale.
   - A question the brief or a comment explicitly marks as open (for example "Open
     question: ...") belongs to the humans: ask it, unless `selected_comments` already
     answer it. Do not replace it with your own assumption, even a reasonable one; you may
     propose a default in the question's rationale.
   - `completed` when the specification is reviewable. Non-material unknowns become
     explicit assumptions.
   - `blocked` only if the brief is unusable (for example empty) or inputs are inconsistent.
5. Add one `evidence` entry per acceptance criterion with `status: defined` and the
   specification path.
6. If the brief is too big for one delivery (several independent outcomes that could each ship
   and be accepted on their own), still write the full specification, and propose slices in
   `proposed_tickets` (`S1`, `S2`...): each a `summary` and a `description` that is a usable
   brief (problem, scope, exclusions, numbered acceptance criteria). Say in the specification's
   Scope section that slices were proposed. They are created only if someone asks for them, and
   the reviewers may then narrow this ticket to the first slice.
7. If `fast_track` is true (a small change), also write `output.artifact_dir/plan.md` from
   `${CLAUDE_PLUGIN_ROOT}/templates/plan.md` and fill `footprint` in the result, exactly as the
   `plan-ticket` procedure describes (`${CLAUDE_PLUGIN_ROOT}/skills/plan-ticket/SKILL.md`, steps
   3 to 6). The specification's approval approves this plan too, so keep it to what the
   specification asks. If the change turns out not to be small, write no plan and say so in
   `summary`: planning then runs as usual.

## Result

`procedure`: `refine-ticket`. `artifacts`: `[{"path": "specification.md", "kind": "specification"}]`,
plus `{"path": "plan.md", "kind": "plan"}` on the fast track.
Do not modify any file in the working directory.
