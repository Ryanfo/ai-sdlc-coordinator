---
name: verify-release
description: Verify a human-recorded release against the approved release proposal using observed smoke evidence. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

> Ports: `PORT` and `E2E_PORT` are already exported for this run. Run commands as plain
> `npm run …` / `npx …` without `VAR=value` prefixes (prefixed commands are denied).

# Procedure: verify-release

contract_id: `delivery.verify-release/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

The working directory is checked out at the released commit recorded by a human. Confirm
the release behaves as the approved release proposal says, using observed evidence only.

## Steps

1. Read the approved release proposal and `release_record.json` in the inputs (released
   commit, environment, merged PR, provenance established by the coordinator) and the
   coordinator's smoke check results in `coordinator_checks.json`.
2. Run the proposal's smoke steps that are runnable locally, using `envelope.ports`.
3. Write `release-verification.md` in `output.artifact_dir` from
   `${CLAUDE_PLUGIN_ROOT}/templates/release-verification.md`.

## Browser tests

Browser (Playwright) end-to-end tests cannot launch Chromium inside the worker sandbox on
macOS. The coordinator runs them itself on the candidate and on the integration tree; read
`coordinator_checks.json` and the logs in `check-logs/` under the inputs directory and cite
them as the e2e evidence. Run unit and component tests yourself.

## Result

`procedure`: `verify-release`. `artifacts`:
`[{"path": "release-verification.md", "kind": "release_verification"}]`. One `evidence`
entry per acceptance criterion you could observe. Any failed smoke step is a `blocker`
finding. Do not modify tracked files.
