---
name: verify-ticket
description: Verify a frozen candidate by observing behaviour and checks in a disposable worktree, criterion by criterion. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

> Ports: `PORT` and `E2E_PORT` are already exported for this run. Run commands as plain
> `npm run …` / `npx …` without `VAR=value` prefixes (prefixed commands are denied).

# Procedure: verify-ticket

contract_id: `delivery.verify-ticket/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

Independently establish whether candidate `source.candidate_sha` meets each acceptance
criterion, using observed evidence. The working directory is a disposable checkout of the
candidate. The coordinator separately runs the configured checks on the candidate and on
an integration tree; its results are authoritative.

## Steps

1. Read the brief, approved specification, the review report at `review_report_path`
   and `coordinator_checks.json` in the inputs (the coordinator's own check results).
   Open the designs in `attachments` and `designs` that acceptance criteria refer to.
2. Install dependencies if needed using the project's lockfile (`npm ci`). Use
   `ports.app` (and other entries in `ports`) for anything that listens.
3. For each acceptance criterion, run the specific tests or a focused observation
   (for example a unit test filter or an e2e spec) and record what you observed.
4. Do not edit tracked files. Generated caches and build output are fine; the coordinator
   checks the tracked diff afterwards.
5. Write `verification.md` in `output.artifact_dir` from
   `${CLAUDE_PLUGIN_ROOT}/templates/verification.md`.

## Browser tests

Browser (Playwright) end-to-end tests cannot launch Chromium inside the worker sandbox on
macOS. The coordinator runs them itself on the candidate and on the integration tree; read
`coordinator_checks.json` and the logs in `check-logs/` under the inputs directory and cite
them as the e2e evidence. Run unit and component tests yourself.

## Result

`procedure`: `verify-ticket`. `artifacts`: `[{"path": "verification.md", "kind": "verification"}]`.
One `evidence` entry per criterion (`met`, `not_met`, `unverified`). Every command you ran
goes in `worker_checks` with its real result. Defects go in `findings`. `outcome` is
`completed` when you could verify, even if criteria are not met.
