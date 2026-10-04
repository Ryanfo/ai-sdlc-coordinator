---
name: investigate-ticket
description: Answer a spike's question by investigating the codebase and options, and write versioned findings with a recommendation and proposed follow-up tickets. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

> Ports: `PORT` and `E2E_PORT` are already exported for this run. Run commands as plain
> `npm run …` / `npx …` without `VAR=value` prefixes (prefixed commands are denied).

# Procedure: investigate-ticket

contract_id: `delivery.investigate-ticket/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

This ticket is a spike (`work_kind: spike`): its approved specification asks a question rather
than describing something to build. Write findings revision `output.next_revision` that answers
it well enough for the team to decide what to do next. People review the findings like a plan;
accepting them completes the spike. Nothing you do here is merged.

## Steps

1. Read the approved specification in `approved_artefacts`: the question, why it matters, what
   a useful answer contains (its acceptance criteria) and any time box or exclusions. Read
   `linked_tickets`, `attachments` and `designs` it refers to.
2. If `prior_drafts` holds earlier findings, revise them: keep what was not challenged and
   address every numbered item in `feedback_items` and `selected_comments`.
3. Investigate. Read the code in the working directory (a disposable checkout of the base
   branch), its tests and documentation. You may run commands and try small experiments in
   `output.artifact_dir` or your temporary directory to measure or prove a point; do not edit
   tracked files, and say what you ran.
4. Write `output.artifact_dir/findings.md` from `${CLAUDE_PLUGIN_ROOT}/templates/findings.md`:
   the short answer first, the options you compared with their trade-offs, a recommendation,
   the evidence (code references, measurements, what you tried), and risks and unknowns.
5. When the recommendation means more work, propose it as tickets in `proposed_tickets`
   (`S1`, `S2`...): each a `summary` and a `description` that is a usable brief (problem, scope,
   exclusions, numbered acceptance criteria). Leave `issue_type` empty for a story. They are
   created only if someone asks for them.
6. One `evidence` entry per acceptance criterion: `met` when the findings answer it (say
   where), `not_met` when they cannot and why.

## Result

`procedure`: `investigate-ticket`. `artifacts`: `[{"path": "findings.md", "kind": "plan"}]`.
`outcome`: `completed`, or `needs_clarification` when the question is ambiguous enough that an
answer would mislead (still write your best findings so far). No `footprint`.
