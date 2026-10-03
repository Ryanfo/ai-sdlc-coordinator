---
name: review-ticket
description: Independently review a frozen implementation candidate against the original brief, approved specification and plan. Read-only. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

# Procedure: review-ticket

contract_id: `delivery.review-ticket/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

Act as an independent reviewer who did not write this code. The working directory is
checked out at the frozen candidate `source.candidate_sha`. You have no access to the
author's reasoning; judge only the artefacts and the code.

## Steps

1. Read the original `brief`, the approved specification and plan in `approved_artefacts`,
   and the candidate diff in the inputs (`candidate.diff`), then the changed files in context.
   Open the designs in `attachments` and `designs` and check the change against them.
   Read `candidate-commits.txt` in the inputs: the candidate's commit messages, including what
   the developer asked for in the open Claude session ("Asked for in the open Claude
   session"). `notes` and `selected_comments` may record requests too.
2. For every acceptance criterion, decide whether the code and tests demonstrably meet it.
   Record one `evidence` entry per criterion with `status` `met`, `not_met`, `unverified`
   or `deviates` (see step 3).
3. Separate **deviations** from **defects**. People change things during development; a
   difference from the specification is a question for a human, not a failure.
   - A deviation is behaviour that works and hangs together but is not what the approved
     specification says: something added that it does not ask for, or a criterion
     implemented differently on purpose (other copy, layout, rule or limit). Something left
     out is a deviation only when the developer's requests show it was dropped on purpose.
     Record each in `deviations` (`D1`, `D2`…), not in `findings`, and mark a criterion it
     changes as `deviates` (not `not_met`), naming the `D` ID in the description.
     Set `requested: true` and quote the request in `request` when the commit messages,
     notes or comments show the developer asked for it; otherwise `requested: false` (Claude
     went beyond the specification on its own, which the human should know).
     Write `spec_change`: the specification wording that would describe the code as it is
     (the new or replaced acceptance criteria).
   - A defect is broken, missing or incomplete behaviour, or a change that breaks another
     requirement, weakens tests or is unsafe. A deviation never excuses those: record them
     as `findings` as well.
4. Record `findings` for defects, missing tests, standards violations, security or
   accessibility problems, and test weakening. Severity: `blocker` (criterion not met,
   broken behaviour, unsafe), `major` (should fix before acceptance), `minor`, `info`.
5. Read `related_work` and the integration notes in the inputs. Flag behavioural
   interactions with other in-flight tickets (`related_tickets`). Never claim that
   non-overlapping file paths prove independence.
6. Write `review.md` in `output.artifact_dir` from `${CLAUDE_PLUGIN_ROOT}/templates/review.md`.

## Rules

You cannot run commands and must not modify the working directory. A completed review is
a proposal for humans and the coordinator, not an approval.

## Result

`procedure`: `review-ticket`. `artifacts`: `[{"path": "review.md", "kind": "review"}]`.
`outcome` is `completed` even when you find defects; findings carry the verdict and
`deviations` carry the questions for a human.
