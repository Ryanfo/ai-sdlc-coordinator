# Completion report

Date: 1 October 2026 · delivery-platform 0.1.0 · Claude Code 2.1.278 · macOS (Darwin 24.6)

This report separates what is **implemented**, **tested locally**, **tested against live
services** and **still blocked**. Nothing below is claimed as working against live Jira or
GitHub until it has been run there.

## Summary

| Milestone | Status |
|---|---|
| M0 discovery and decisions | Done (`docs/design/Implementation_Decisions.md`) |
| M1 deterministic core, supervisor, journal, locks, parallel dispatch | Implemented and tested locally |
| M2 Jira and Git/GitHub adapters | Implemented; Git tested against real repositories; Jira/GitHub adapters tested against HTTP mocks and a fake `gh`. **Not yet run against the live Jira project or GitHub repository** |
| M3 plugin and headless runner | Implemented; real Claude probe READY; all seven procedures run with the real CLI |
| M4 clarification and specification approval | Implemented and tested locally; real refinement with a clarification round |
| M5 planning, implementation, overlap checkpoints 1–3 | Implemented and tested locally; real planning and implementation |
| M6 verification, human gates, integration evidence, CI provenance | Implemented and tested locally; real review/verify with candidate and integration-tree checks |
| M7 release proposal, merge provenance, release verification | Implemented and tested locally; real release preparation and verification to Done |
| M8 recovery and hardening | Implemented and tested locally (failure injection) |
| M9 live pilot | **Blocked on Jira project/workflow setup and GitHub repository creation** |

## Tested locally

`uv run pytest -q`: 172 tests pass in about 2.5 minutes (Python 3.14; unit tests also pass on 3.11).
`ruff` and `mypy --strict` are clean. Integration tests use a fake Jira that enforces the
setup-document workflow and models lost responses, a fake GitHub, **real Git repositories**, and
a fake `claude` executable that speaks the real stream-json protocol and enforces the
`--restricted` directory confinement.

| Area | Demonstrated |
|---|---|
| Lifecycle | One ticket from Backlog through clarification, spec change request (v3), plan, PR, verification, code/acceptance gates, release proposal, human merge, release record, Done |
| Parallel sessions | 12 tickets alive at once (barrier proves concurrency); slow, blocked, failed and waiting tickets do not hold others; repeated polls never launch a duplicate writer; separate worktrees, ports, processes and branches; static guard against any session cap |
| Ownership | Other assignee ignored; mover irrelevant; second supervisor refused (OS lock across processes) |
| Human decisions | Current vs stale token, unauthorised comment or transition, automated transition, edited and conflicting decisions, duplicate approvals, wrong resume action, re-decision after an approver resumes |
| GitHub gates | Author self-approval rejected; stale approval on old head; changes-requested blocks; check runs and commit statuses; missing/pending/skipped/neutral; wrong producer; duplicate names; new commit after approval invalidates the candidate |
| Verification | Failing coordinator check moves to Changes requested with findings; fix produces candidate c2 and supersedes tokens; behavioural conflict caught only in the integration tree while Git merges cleanly |
| Overlap | Two developers' supervisors: deduplicated warnings mirrored on both tickets; shared interface blocks the later ticket until `OVERLAP … PROCEED` and Resume; CI merge-commit provenance verified/stale/mismatch |
| Release | Merge commit and squash provenance; later unrelated merges accepted; unrelated SHA rejected; unapproved commit merged is flagged; failing smoke blocks; coordinator never merges or deploys |
| Recovery | Lost comment, transition and PR-creation responses reconcile without duplicates; graceful shutdown mid-session resumes two runs independently; corrupt journal blocks only its ticket; Jira offline backs off; handover and stop of one ticket while others run |
| Worker safety | Envelope outside allowed dirs is unreadable; forged contract ID, missing plugin, traversal and symlink artefact paths rejected; protected paths blocked; credentials and provider variables stripped |

## Tested with the real Claude Code CLI (subscription)

- `delivery doctor --claude-probe`: plugin loaded, procedure executed, planted secret not
  readable, `git push` did not land, writes outside allowed dirs and to the read-only worktree
  blocked, `gh` and network denied (5 CLI-recorded denials); an `npm run` test script may write
  build caches and run a local server. READY.
- Real procedures against a copy of the pilot app with real Git and the pilot app's real gates
  (Jira and GitHub faked because the live project does not exist yet):
  - refine-ticket: asked the brief's open question with a proposed default; after answers,
    produced specification v002 grounded in the codebase.
  - plan-ticket: plan v001 with a criterion-to-test mapping and a footprint declaring the shared
    `TaskQuery` interface.
  - all seven procedures to Done: see "Live procedure run" below.

## Live procedure run

All seven real procedures, real supervisor, real Git, the pilot app's real gates; Jira and
GitHub simulated; humans scripted. Final run: Backlog to **Done in 12.5 minutes**. Artefacts are
in [examples/](examples/).

| Stage | Real result |
|---|---|
| Refinement | Asked the brief's explicit open question (round R1) with a proposed default; after the answer produced specification v002 grounded in the codebase |
| Planning | Plan v001 mapping every criterion to named unit, component and e2e tests; footprint declaring the shared `TaskQuery` interface |
| Development | 5 files, about 86 lines, matching the footprint exactly; one coordinator commit with `Delivery-Op` marker; PR opened |
| Verification | Coordinator: lint, typecheck, unit, build, e2e passed on the candidate **and** on the integration tree. Fresh review and verify sessions each found AC1–AC4 met; zero permission denials in the verifier |
| Release preparation | Release proposal v001 naming the accepted candidate SHA, smoke and rollback steps |
| Release verification | Merge-commit provenance confirmed (merged tree equals the approved candidate); smoke evidence; ticket Done |

Defects found only by the real runs, all fixed and covered by tests or the probe:

| Defect | Fix |
|---|---|
| Envelope written outside the directories `--restricted` allows, so the worker could not read it (the coordinator correctly rejected the output) | Envelopes live in the run's read-only inputs directory; the fake `claude` now enforces the same confinement |
| refine-ticket replaced an explicitly open question with an assumption | Procedure: explicitly open questions are always asked unless already answered |
| Edit deny rules become sandbox write bans, so verification could not write `node_modules`/`dist` (release verification correctly blocked rather than claim success) | Verifier has no worktree deny; file tools still cannot edit; tracked changes rejected afterwards |
| Tests could not bind a local port; `allowMachLookup` disables sandbox auto-allow; `VAR=value` prefixes need approval | `allowLocalBinding` for implementer/verifier; no mach lookup, so browser e2e runs in coordinator checks with logs shared to the verifier; ports exported in the environment |
| `claude auth status` reported an expired standalone session as logged in | Doctor states that only `--claude-probe` proves a working session |
| Version range `<3` parsed as 0.0.0 | Version parser fixed (unit test) |

## Still blocked or manual

| Item | Needed from |
|---|---|
| Jira project, workflow, resume field, account IDs, plan tier | Jira administrator / you |
| `delivery workflow inspect` and `delivery doctor` against the live project | After the above |
| Pilot GitHub repository (public) with branch protection | Your confirmation to create it |
| Independent GitHub reviewer | A second person |
| Second developer identity for cross-developer evidence | A second Jira user running a supervisor |
| Live M9 scenario and its evidence links | All of the above |

## Known limitations (by design)

See [security-boundary.md](security-boundary.md): single Jira identity cannot be separated by
Jira; no distributed lock across machines; advisory overlap detection; coordinator-run checks
execute repository code unsandboxed; no workflow-creation helper (setup is done in the Jira UI
and validated read-only by `delivery workflow inspect`).
