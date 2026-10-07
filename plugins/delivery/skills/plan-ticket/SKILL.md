---
name: plan-ticket
description: Produce a versioned implementation plan and machine-readable change footprint from an approved specification. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

# Procedure: plan-ticket

contract_id: `delivery.plan-ticket/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

Produce plan revision `output.next_revision` that implements exactly the approved
specification, plus a change footprint the coordinator uses to detect overlapping work.

## Steps

1. Read the approved specification in `approved_artefacts` (kind `specification`). It is
   the scope. Do not add scope. If it cannot be implemented as written, ask a question.
   Open the designs in `attachments` and `designs` that the specification refers to, so the plan's
   components, states and tests match them.
2. If `prior_drafts` contains an earlier plan, revise it and apply every item in
   `feedback_items` and every request in `selected_comments`. If `changes_requested` is true
   and nothing says what to change, ask (stage contract, *Decisions, change requests and
   answers*).
3. Study the codebase in the working directory: architecture, conventions, test layout,
   scripts. Follow the application's standards in `CLAUDE.md` and `docs/`.
4. Write `output.artifact_dir/plan.md` using `${CLAUDE_PLUGIN_ROOT}/templates/plan.md`:
   affected files, interfaces, ordered implementation steps, a criterion-to-test mapping
   (every `AC` maps to at least one named test), risks, rollback implications. For
   `work_kind: bug`, the first step is the regression test that reproduces the bug (named, and
   failing before the fix), then the root cause as far as you can tell from the code, then the
   fix.
5. If an architectural decision is needed, also write `adr-001.md` (kind `architecture`)
   from `${CLAUDE_PLUGIN_ROOT}/templates/adr.md`.
6. Fill `footprint` in the result:
   - `paths`: every file or directory glob you expect to create, modify or delete.
   - `components`: named components/modules affected.
   - `interfaces`, `domain_models`, `schemas`, `migrations`, `dependencies`: shared
     contracts this change touches (exported functions, types, storage formats, packages).
   - `ticket_dependencies`: ticket keys this work must follow, if any.
   - `sequencing_notes`: anything a human should know about ordering.
7. Read `related_work`. If another ticket's footprint overlaps yours, say so in the plan's
   "Coordination" section and in `findings` (severity `info` or `major`, with
   `related_tickets`). Path comparison never proves independence; mention behavioural
   interactions you can see.

## Result

`procedure`: `plan-ticket`. `artifacts` must include `plan.md` (kind `plan`). `footprint` is
required for `completed`. Do not modify the working directory.
