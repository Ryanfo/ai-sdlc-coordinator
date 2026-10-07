---
name: amend-spec
description: Rewrite the approved specification so it includes deviations a human accepted during development, without a new refinement round. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

# Procedure: amend-spec

contract_id: `delivery.amend-spec/v1`

Read the shared contract at `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then
the envelope at `$envelope`.

## Goal

The code differs from the approved specification in ways people accepted by approving the code
and accepting the delivery. Write
specification revision `output.next_revision`: the approved specification, changed only as
far as needed to describe the delivered behaviour for each accepted deviation. People have
already accepted these changes, so the coordinator publishes your revision as the new
approved specification; there is no further specification review.

## Inputs

- `approved_artefacts`: the current approved specification (the document you revise).
- `feedback_items`: the accepted deviations by ID (`D1`...). Each says what the code does
  differently, the acceptance criterion it affects (if any), whether the developer asked for
  it, and the specification wording the reviewer proposed.
- `review_report_path`: the independent review that found them.
- The working directory is the candidate's code (read-only). Read it where you need to
  describe the behaviour exactly (copy, labels, rules, limits).

## Steps

1. Read the approved specification, every accepted deviation and the review report. Open the
   designs in `attachments` and `designs` if a deviation concerns layout or copy.
2. Check each deviation against the code so the specification describes what is really built.
   Treat the reviewer's proposed wording as a starting point, not as final.
3. Write the whole specification to `output.artifact_dir/specification.md`, starting from the
   approved one:
   - Rewrite an acceptance criterion a deviation changes, keeping its ID (`AC3` stays `AC3`).
   - Add a criterion for new behaviour with the next free ID. Remove a criterion only when an
     accepted deviation dropped it, and say so in the revision history (do not renumber).
   - Update scope, exclusions and other sections the deviation touches.
   - Change nothing that no accepted deviation covers, and keep the "Original brief" section
     exactly as it is.
   - Add a revision history entry: "Amended during development: D1, D2… accepted by an
     approver", with one line per deviation saying what changed in the specification.
4. Add one `evidence` entry per acceptance criterion of the new revision, `status: defined`.

If a deviation cannot be described without contradicting another acceptance criterion that
no deviation changes, do not choose for the humans: return `blocked` with the conflict in
`blocker_reason`.

## Result

`procedure`: `amend-spec`. `artifacts`: `[{"path": "specification.md", "kind": "specification"}]`.
`summary` says in two to four sentences what changed in the specification. Do not modify
any file in the working directory.
