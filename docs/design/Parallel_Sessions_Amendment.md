# Amendments for parallel Claude Code sessions

1 October 2026 | Addendum to Claude_Code_AI_SDLC_Build_Handoff.md

This document contains amendments only. Where it conflicts with the original handoff or onboarding documents, these amendments take precedence. Implement parallel execution in the initial build, not as a later pilot extension.

## 1 Replace the single execution restriction

Replace every requirement for one active execution per coordinator with:

> One local coordinator can manage any number of independent Claude Code sessions concurrently across eligible tickets. There is no framework-imposed numerical session limit, default cap or requirement that one ticket finish before another starts.

Retain one active coordinator process per configured developer identity on the registered machine. That coordinator is the supervisor for multiple child sessions. Developers must not start additional coordinator processes to obtain parallel execution.

The existing cross-machine restriction remains: this amendment does not introduce a distributed atomic claim service or permit competing supervisors for the same identity. Multiple developers can run their own supervisors and work on different tickets simultaneously.

Remove `max_parallel_runs = 1` from configuration and remove the corresponding validation and scheduler assumptions. Do not replace it with a different number, hidden worker-pool maximum, semaphore cap or an optional setting that silently defaults to a numerical cap.

Machine capacity and Claude subscription availability remain real operational constraints. Surface actual resource exhaustion, permission errors and provider usage limits transparently; preserve affected executions for recovery. These conditions do not justify imposing a fixed session-count ceiling or enabling paid API fallback. Provide manual pause/resume controls for dispatch when needed.

## 2 Replace the sequential scheduler

Amend sections 5, 17 and 19 of the handoff:

- Discover and dispatch all currently eligible independent tickets without waiting for an unrelated execution to complete.
- Use asynchronous subprocess supervision or an equivalent nonblocking design. A long Claude session must not block Jira polling, heartbeats, cancellation monitoring, publication or other sessions.
- Retain assignee, project, supported type, ready status, input and approval checks for each ticket separately.
- Maintain deterministic discovery ordering for reproducible logs, without using that ordering to serialise whole-ticket execution.
- Scope execution claims and local locks to individual tickets/runs. Only one state-mutating stage may own a ticket at a time; this is a correctness rule, not a global session limit.
- Prevent repeated polls, retries or recovery from launching duplicate attempts for the same ticket/stage/input revision.
- Human review, clarification or a blocker on one ticket must not hold unrelated tickets.
- Apply API backoff to the affected integration/request stream as necessary without freezing all child-process supervision.

The ticket workflow and seven stage procedures remain the same. Independent review must still use fresh processes with no author-session reuse. Different tickets can be in different stages concurrently. Do not concurrently run dependent stages against the same mutable ticket state.

## 3 Isolate every execution

Amend sections 10, 11, 13 and 14:

Each active execution requires its own:

- Run ID, stage/attempt identity and input snapshot.
- Managed working directory/worktree and frozen source reference.
- Claude session identity, process group and tool/permission profile.
- Output location, logs, recovery journal and publication operations.
- Temporary directories and test artefacts.
- Test ports, databases, browser profiles and other mutable fixtures where applicable.

Never run two writers in the same worktree or against the same branch. Ticket branch names must remain unique and recoverable. Read-only verification contexts may share immutable inputs, but their generated caches and outputs must not corrupt another run.

Allocate ports and mutable test resources explicitly rather than assuming every session can use the application's default port. Clean up only resources owned by that run. Killing or recovering one child process must not terminate another ticket's session.

Git worktrees can share repository metadata. Protect short critical operations such as worktree creation, ref updates, committing and shared metadata changes with appropriate repository-operation locks. These locks must not cover a whole Claude execution or become a hidden global session limit. Dependency installation and caches also need isolation or safe shared access.

## 4 Add shared overlap detection

Extend planning, development preflight and verification to identify overlapping work across developers and sessions.

Each plan must publish a machine-readable change footprint alongside its human-readable plan:

- Ticket, owner, stage, plan revision and source commit.
- Expected files/directories and components.
- Shared interfaces, domain models, schemas, migrations or dependencies affected.
- Known ticket dependencies and sequencing requirements.

Publish the footprint into the application repository with an immutable reference from Jira. Keep a compact current reference in the shared ticket execution record. Do not keep this information solely in a developer's local journal.

The coordinator executes only its developer's assigned tickets, but reads relevant active tickets and published change footprints across the configured project/repository for coordination. Assignee filtering must not hide other developers' overlapping work from this check.

Compare footprints:

1. When publishing/reviewing a plan.
2. Immediately before implementation begins.
3. At checkpoints when actual changed paths become available.
4. When a candidate PR is published or changes.
5. Before accepting integration evidence or progressing towards release.

Use deterministic file/component comparison for known overlaps. A fresh reviewer may identify behavioural dependencies not captured by paths, but must not claim that file comparison proves independence. Record the time and revisions inspected.

Local unpublished changes on another machine are not immediately visible. Simultaneous starts and evolving plans mean this is advisory detection, not an atomic file lock or a guarantee that every conflict will be caught before coding.

## 5 Flag overlap in Jira and the review evidence

| Finding | Required behaviour | Visible record |
|---|---|---|
| Same component or file, apparently independent edits | Warn; parallel work may proceed | Deduplicated Jira comment linking both tickets, plus plan/PR warning |
| Clear interface, schema, migration or prerequisite dependency | Pause the dependent ticket for a human sequencing decision | Blocked reason, originating stage and proposed dependency |
| Actual changed paths overlap with another published candidate | Refresh the warning and request targeted integration scrutiny | Jira update and independent review/verification report |
| Uncertain behavioural interaction | Make uncertainty explicit; escalate when material | Review finding with affected tickets and required human action |

Comments must identify the tickets, assignees, overlapping paths/components, inspected revisions and recommended next action. Mirror material findings on both tickets where permissions permit. Use stable warning IDs so polling does not flood comments.

Humans decide whether to proceed, change scope or establish an order. Record dependency links where appropriate. Do not turn every same-file warning into a compulsory block. Conversely, do not automatically dismiss a material dependency merely to maintain parallel throughput.

When a dependency clears, revalidate source references and approvals before resuming. Where planning or implementation needs revision, return to the appropriate ready/review path; do not continue with stale assumptions.

## 6 Test integration before merge

Amend sections 9, 16, 20 and 21:

- Do not rely solely on Git reporting a textual merge conflict.
- Verify each candidate against the latest relevant base branch and known interacting changes before merge.
- Keep candidate-only checks distinct from checks of the combined integration tree. Record the candidate head, base commit, actual tested commit/tree and check producer.
- For GitHub CI using a temporary merge commit, validate its association with the intended head/base pair instead of incorrectly requiring every CI check to run directly on the PR head SHA.
- Require appropriate up-to-date branch/integration checks. A merge queue may be used when available, but is not a prerequisite for parallel local sessions; configure any required merge-group CI deliberately.
- Test behavioural compatibility of overlapping changes, including shared interfaces and data assumptions, even when Git can merge the files cleanly.
- Human authorisation for merge and release remains unchanged. The coordinator does not acquire merge/deployment privileges through this amendment.

If integrating another ticket requires a code change or conflict resolution, preserve the original work, create a new candidate and repeat the applicable checks and reviews. Supersede code, acceptance and release approvals tied to the old candidate. Specification/plan approvals change only if their approved content or scope changes.

Under the existing no-force-push rule, a merge from the updated base into the feature branch is preferable to silently rewriting published history. Material conflict decisions must reach a human; an agent must not guess between conflicting requirements.

## 7 Make recovery multi-session aware

Amend sections 11 and 12:

- Keep a supervisor record plus independent execution records, checkpoints and publication journals per run.
- Serialize writes to any shared journal/index safely; prefer separate per-run append-only records and atomic snapshots.
- Reconcile every unfinished run after restart, rather than assuming only one run can exist.
- Check child identity and process ownership before relaunching an interrupted attempt.
- Resume independent recoverable runs concurrently; a corrupt or ambiguous record blocks its ticket, not all unrelated work unless shared state integrity is compromised.
- A subscription limit affecting several sessions must be reported honestly for each affected run. Preserve successful outputs from other sessions.
- On graceful supervisor shutdown, stop/checkpoint all owned children and record pending remote reconciliation for each.
- Support stopping, cancelling, recovering and handing over one ticket while other sessions continue.

Retain stable operation markers and remote reconciliation. Concurrent operation handling must not create duplicate PRs, comments, transitions or branch publication. No shared Jira property write should be described as a guaranteed distributed lock.

## 8 Amend operator commands and onboarding

Update the implementation, root README and developer onboarding guide together:

| Interface | Amended requirement |
|---|---|
| `delivery run` | Supervisor runs multiple eligible tickets concurrently; no session-count cap |
| `delivery run --once` | One discovery/reconciliation cycle; dispatch all eligible independent tickets found in that cycle and supervise them to their stage outcomes, rather than process at most one execution |
| `delivery status` | List every session with ticket, stage, worker/session identity, state, start time and next action |
| `delivery inspect <ticket>` | Include overlap warnings, dependencies and frozen integration refs |
| `delivery recover <ticket>` | Recover only the selected ticket without restarting unrelated sessions |
| `delivery handover <ticket>` | Stop/checkpoint only the selected ticket before reassignment |
| New ticket-scoped stop control | Stop a selected child safely and preserve recoverable work |
| New dispatch pause/resume controls | Operator can pause new launches while existing sessions continue; no implicit numerical cap |

The one editable local configuration file remains. Explain that one supervisor can host many Claude sessions, each isolated. Remove every onboarding statement that limits a developer to one active ticket execution. Keep the restriction against competing supervisors for the same identity.

Jira statuses and board columns need no additional status solely to indicate parallelism. Show the execution/session identity in existing run summaries and operational views. All existing human gates still apply per ticket.

## 9 Add implementation tests and pilot evidence

Add tests for:

- Multiple eligible tickets start while another session is still running.
- No schema, dispatcher, executor or worker-pool code introduces a fixed numerical session ceiling.
- One blocked, slow, failed or awaiting-human ticket does not prevent unrelated sessions progressing.
- Duplicate polling cannot launch concurrent writers for the same ticket/run.
- Worktrees, outputs, logs, ports, fixtures and tracked branches remain isolated.
- Short repository locks protect shared metadata without serializing entire executions.
- Two developers publish overlapping plans and receive linked, deduplicated warnings.
- Direct dependencies block the affected ticket with an actionable sequencing decision.
- Same-file independent edits can continue after an explicit decision.
- Overlapping behavioural changes are checked even without textual merge conflicts.
- Integration evidence identifies the actual candidate/base/merge tree correctly.
- A new candidate invalidates the relevant approvals.
- Crash/restart reconciles several unfinished sessions independently.
- Cancelling or handing over one ticket leaves other sessions running.
- Resource/provider failures preserve all affected records and never trigger paid API fallback.

Demonstrate multiple real Claude sessions concurrently on separate tickets in the live pilot, plus a controlled overlapping-change scenario. Include both same-developer parallelism and cross-developer coordination where live access permits. Use deterministic fixtures for large-scale scheduling tests rather than consuming unnecessary subscription usage. The demonstration's sample size must not become an implementation limit.

## 10 Amendments to completion criteria

The initial delivery is complete only when parallel sessions, per-ticket isolation/recovery, visible overlap warnings and integration checks are implemented and tested. Do not report a sequential implementation as complete against this amendment.

Supply updated onboarding documentation and configuration examples without a session-count maximum. Report actual platform/resource limitations transparently, distinguishing them from framework restrictions. Keep all other ownership, human approval, artefact versioning, security and publication requirements from the original handoff.
