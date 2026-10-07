---
name: prepare-release
description: Prepare a versioned release proposal with notes, smoke and rollback instructions for an accepted candidate. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

# Procedure: prepare-release

contract_id: `delivery.prepare-release/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

Write release proposal revision `output.next_revision` for the accepted candidate
`source.candidate_sha`. Humans merge and release; you only propose.

## Steps

1. Read the approved specification, plan, review and verification reports in
   `approved_artefacts`, and any release feedback in `feedback_items` and `selected_comments`.
   If `changes_requested` is true and nothing says what to change, ask (stage contract,
   *Decisions, change requests and answers*).
2. Write `release.md` in `output.artifact_dir` from
   `${CLAUDE_PLUGIN_ROOT}/templates/release.md`: release notes for users, the exact
   candidate SHA, the release environment profile (`local-pilot` unless the envelope says
   otherwise), pre-merge checklist, step-by-step smoke checks a human or the coordinator can
   run, and rollback instructions.
3. Fill `release` in the result: `candidate_sha` (exactly `source.candidate_sha`),
   `smoke_steps` and `rollback_steps`.

## Result

`procedure`: `prepare-release`. `artifacts`: `[{"path": "release.md", "kind": "release"}]`.
Do not modify the working directory.
