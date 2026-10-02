---
name: implement-ticket
description: Implement an approved specification and plan in an isolated feature worktree with tests and evidence mapping. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

> Ports: `PORT` and `E2E_PORT` are already exported for this run. Run commands as plain
> `npm run …` / `npx …` without `VAR=value` prefixes (prefixed commands are denied).

# Procedure: implement-ticket

contract_id: `delivery.implement-ticket/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

Change the code in the working directory (the ticket's feature worktree) so that every
acceptance criterion in the approved specification is met, following the approved plan.

## Rules

- Scope is the approved specification. The plan is the agreed approach. Deviate from the
  plan only when necessary, and explain why in `summary` and `findings`.
- Follow the application's standards (`CLAUDE.md`, `docs/`): strict types, small
  meaningful functions, error handling, stable IDs, accessible UI where applicable.
- Write or update tests for every acceptance criterion. Never weaken, skip or delete
  existing tests to make them pass. Never add blanket lint or type-check suppressions.
- Do not edit `.github/`, `.claude/`, `CLAUDE.md`, `.mcp.json` or `docs/delivery/`.
- Do not commit, push or change Git configuration: the coordinator commits your changes.
- Browser e2e tests cannot launch inside your sandbox on macOS; write or update them, run
  unit/component tests yourself, and leave e2e execution to the coordinator's checks.
- Use the ports in `envelope.ports` for anything that listens (dev server, e2e tests): set
  `PORT` from `ports.app`. Never assume a default port; other sessions run concurrently.
- `selected_comments` may contain requested changes (`F1`...) or verification findings.
  Address each one and say how in `evidence` or `summary`.

## Steps

0. If `prior_work` is set, an earlier session stopped part-way and its unfinished changes are
   already in the working copy. Read `prior_work.session_tail_path` to see where it stopped,
   inspect the changes (`git status`, `git diff`), keep what is right and finish the remaining
   work. Do not start again from scratch, and do not repeat a step that the tail shows failing
   the same way more than once: try a different approach or report the blocker.
1. Read the approved specification and plan from `approved_artefacts`, and open the designs
   in `attachments` and `designs` they refer to. Match layout, copy and states shown there; where a design
   and the specification disagree, follow the specification and note it.
2. Implement in small steps. Run the relevant tests and type checks as you go using the
   project's scripts (for example `npm run test:unit`, `npm run typecheck`).
3. Before finishing, run the configured checks listed in `configured_checks` that you can
   run locally and record what you ran in `worker_checks` honestly.
4. Optionally write `implementation-notes.md` (kind `doc`) in `output.artifact_dir`.

## Result

`procedure`: `implement-ticket`. List changed files in `artifacts` with repository-relative
paths and kinds `code` or `test`. One `evidence` entry per acceptance criterion naming the
test that demonstrates it (`status: met` only if you saw that test pass). Use `blocked` if
the plan cannot be implemented safely, and `needs_clarification` for a material ambiguity.
