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
2. For every acceptance criterion, decide whether the code and tests demonstrably meet it.
   Record one `evidence` entry per criterion with `status` `met`, `not_met` or `unverified`.
3. Record `findings` for defects, missing tests, scope creep, standards violations,
   security or accessibility problems, and test weakening. Severity:
   `blocker` (criterion not met, broken behaviour, unsafe), `major` (should fix before
   acceptance), `minor`, `info`.
4. Read `related_work` and the integration notes in the inputs. Flag behavioural
   interactions with other in-flight tickets (`related_tickets`). Never claim that
   non-overlapping file paths prove independence.
5. Write `review.md` in `output.artifact_dir` from `${CLAUDE_PLUGIN_ROOT}/templates/review.md`.

## Rules

You cannot run commands and must not modify the working directory. A completed review is
a proposal for humans and the coordinator, not an approval.

## Result

`procedure`: `review-ticket`. `artifacts`: `[{"path": "review.md", "kind": "review"}]`.
`outcome` is `completed` even when you find defects; findings carry the verdict.
