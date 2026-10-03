# Operations: running, stopping, recovering and handing over

## The supervisor

`delivery run` is a single foreground process per Jira site and developer identity. It holds an
OS lock (`<state_dir>/locks/supervisor-<identity>.lock`) and a control socket. Each poll:

1. Reconciles unfinished runs (only on start-up and on request).
2. Searches Jira for tickets assigned to you in the six *Ready for…* statuses.
3. For each eligible ticket, in deterministic order (ready time, then key), validates how it got
   there (intake) and launches it as its own task. Ordering is for reproducible logs only; it
   never serialises execution.

There is no session-count limit. Machine capacity and subscription limits are real: if you need
to slow down, pause new launches (`delivery dispatch pause`); running sessions continue.

## Run states

| State | Meaning | What happens next |
|---|---|---|
| discovered / starting | Prepared; start transition being made | Restarted automatically after a crash (revalidated first) |
| running | Claude session working | Heartbeat every `heartbeat_seconds`; stops if the ticket is reassigned, moved, cancelled or its inputs change |
| publishing | Decision made; committing, commenting, transitioning | Every step journaled; uncertain steps reconciled by marker before any retry |
| awaiting_human | Published; ticket is in a review or paused status | Nothing until a human acts |
| interrupted | Stopped mid-work | Not held: resumes on the next supervisor start. Held: needs `delivery recover <KEY> --resume` |
| blocked | Ticket moved to Blocked with a reason | A human fixes the cause and chooses Resume in Jira |
| failed | A definite failure the coordinator could not publish around | `delivery inspect` explains; fix, then `delivery recover` |
| completed | Release verified, ticket Done | — |

## Stopping

- **Everything:** `Ctrl-C` (or SIGTERM). Every child process group is terminated, every run is
  checkpointed as interrupted (not held), nothing is published, and the lock is released.
  The next `delivery run` resumes those runs with fresh sessions from durable inputs.
- **One ticket:** `delivery stop <KEY>`. Only that session stops; it is held until you resume
  it. Other sessions keep running.
- **New work only:** `delivery dispatch pause`, later `delivery dispatch resume`.

## Recovering

```bash
delivery recover PILOT-123 --config ~/delivery.local.toml
```

Recovery never assumes. For each unfinished operation it first asks Jira, GitHub or Git whether
it already happened (comment marker, PR on the branch, commit trailer `Delivery-Op:`, current
status), attaches to what exists, and only then sends what is missing. Comments, PRs and
transitions are never blindly retried; there is no force push. `--resume` continues held work.

A corrupt local record (for example a torn write after a power cut) blocks only that ticket;
`delivery status` shows it as CORRUPT. Inspect the run directory before deleting anything; the
journal is append-only so the last good state is visible.

## Handing over a ticket

```bash
delivery handover PILOT-123 --config ~/delivery.local.toml
```

Stops and checkpoints that ticket only, moves it to Blocked with its resume stage recorded, and
posts a handover comment listing the current artefacts. Then reassign it in Jira. The new
owner's supervisor rebuilds everything from Jira and Git; it does not need your local logs or
Claude session. A stale heartbeat never authorises a takeover.

## Cancelling

Cancel in Jira at any time. The supervisor notices at the next heartbeat, terminates that ticket's
session, publishes nothing further and marks the run cancelled. Other sessions are unaffected.

## Where to look

```text
<state_dir>/
  supervisor/<identity>/supervisor.json   dispatch pause, socket, last poll
  supervisor/<identity>/events.jsonl
  runs/<KEY>/<run-id>/
    snapshot.json                         current run record
    events.jsonl                          append-only events and publication intents/results
    inputs/envelope-<procedure>.json      exactly what the worker was given
    output/<procedure>/                   what the worker wrote
    claude-<procedure>.json               CLI outcome, plugins, permission denials
    logs/claude-<procedure>.jsonl         full CLI stream (secrets redacted)
    logs/<target>-<check>.log             coordinator check output
    settings-<procedure>.json             the generated permission profile
  intake/<KEY>/                           one-off explanation comments for waiting tickets
  locks/                                  supervisor, ticket and repository locks
<worktree_root>/<KEY>/<run-id>/           worktrees (removed when a run finishes)
<worktree_root>/_repo.git                 the coordinator's own clone
```

All directories are created with mode 0700 and files 0600. Do not commit anything from here.

## Common situations

| Situation | Action |
|---|---|
| Subscription limit reached | Tickets block with "usage limit". Wait for the reset, then Resume each in Jira |
| Claude login expired | `claude auth login` in a terminal, then Resume |
| Base branch moved after code approval | The code gate blocks (head or CI no longer current). Request code changes → Submit implementation changes merges the base into the candidate (no rebase) and re-verifies |
| Merge conflict with the base or another ticket | Flagged in the candidate, code-review and verification comments, never a failure. Resolve it in the PR when merging (for example GitHub's Resolve conflicts); release verification accepts the approved candidate plus merges of the base and lists the files the resolution changed. Any other commit added to the PR is still refused |
| Verification failed and the next step is unclear | `delivery inspect <KEY>`: every reason and finding in full, failed check output, Claude logs and what each Jira action available now does |
| Overlap blocks development | Read the overlap comment, decide `OVERLAP <id> PROCEED/WAIT/RESCOPE`, then Resume development |
| Remote ticket branch diverged | The ticket blocks. Reconcile the branch by hand (never force-push), then Resume |
| Jira offline | Polling backs off; nothing is mutated; publication resumes when Jira is back |

## Delivery branches and documentation consolidation

`delivery/<KEY>` branches are retained for audit and are never deleted or merged automatically.
Revision files are append-only; corrections are new revisions. If the team wants approved
specifications, plans or ADRs in the main documentation, a human opens a normal PR that copies
the chosen revisions into `docs/` on the base branch and reviews it like any other change. The
coordinator never does this, so approved code SHAs are never moved by documentation commits.
