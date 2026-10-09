# Operations: running, stopping, recovering and handing over

## The supervisor

`delivery run` is a single process per Jira site and developer identity. `coordinator` starts it
in the background, in a private tmux server of its own (`tmux -L <socket>-coordinator`, session
`coordinator`) started from your terminal's environment, and attaches to it; closing the window
only detaches. It holds an OS lock (`<state_dir>/locks/supervisor-<identity>.lock`) and a
control socket. Everything it prints, and every warning and error with its traceback, also goes
to `<state_dir>/supervisor/<identity>/coordinator.log` (rotated at 5 MB, five kept). It records
when its code last changed and says when the code on disk is newer (`coordinator restart`).
Each poll:

1. Reconciles every unfinished run on start-up (and on request).
2. Searches Jira for tickets assigned to you in the six *Ready for…* statuses.
3. Resumes runs whose retry time has come (see *Retried by themselves* below).
4. For each eligible ticket, in deterministic order (ready time, then key), validates how it got
   there (intake) and launches it as its own task. Ordering is for reproducible logs only; it
   never serialises execution.

An unexpected error in a poll does not stop the supervisor: it is logged with its traceback,
alerted, and the next poll follows after a pause (15 seconds, doubling to 10 minutes). Running
sessions are separate tasks and carry on. If the process itself died (the Mac restarted, the
process was killed), the next start says so, alerts, and resumes what was interrupted.

There is no session-count limit. Machine capacity and subscription limits are real: new
sessions wait by themselves while the disk holding `worktree_root` has less than
`runtime.min_free_disk_gb` (default 5) free or macOS reports critical memory pressure
(`hold_on_memory_pressure`); running sessions carry on, and `delivery status` says why. To slow
down by hand, pause new launches (`delivery dispatch pause`). While sessions run, the Mac is kept
from idle sleep (`runtime.keep_awake`, `caffeinate -i`); closing the lid still sleeps it, and
runs interrupted that way resume afterwards.

### Retried by themselves

| Cause | What happens |
|---|---|
| Claude's API unavailable (overloaded, 5xx, connection lost) or a session that did not start | The run waits and is tried again after 2, 10 and 30 minutes, with its work so far. Only a fourth failure blocks the ticket |
| Jira or GitHub unreachable mid-run | Tried again after 1, 2, 5, then every 10 minutes until they answer; never blocks |
| A publication step whose outcome was uncertain | Reconciled by marker on the next poll |
| The ticket's description or a selected comment edited while Claude worked | Nothing is published from the old text; the stage starts again with the new text a minute later. Nobody needs to resume it |
| GitHub rate limits | The request is repeated after 20, 60 and 120 seconds; after that the run is retried as above |

### Claude Code updates

Claude Code updates itself, and the sandbox behaviour the worker profile relies on can change in
any release. `delivery doctor --claude-probe` records the Claude Code version (and session
mode) it passed on in `<state_dir>/claude-probe.json`. Every five minutes the supervisor compares
that with `claude --version`; when they differ, new sessions wait while it runs the same probe in
the background, and start once it passes. The probe asks Claude to try things, so a failure is
run once more before anything waits for it. A second failure keeps new sessions waiting and
alerts; it is tried again every half hour, and running `delivery doctor --claude-probe` (which
shows the details) ends the wait at once when it passes.
`claude.probe_on_version_change = false` turns this off.

### Alerts and notices

`[notifications] webhook_env` names an environment variable holding a Slack-compatible incoming
webhook; `desktop` (default on) shows the same as macOS notifications. They carry what concerns
the person running the coordinator: it stopped unexpectedly or hit an internal error, Claude
cannot be used, Claude Code failed its probe, the machine is short of room. Each alert is sent
at most once an hour. With `operational = "operator"`, notices about Claude's login or usage
limit and internal errors are not commented on tickets at all, which keeps a client's tickets
free of anything about the developer's machine. Comments about an internal error never include
the error text, which stays in the coordinator log.

### Local retention

Run logs (with the Claude transcripts, which contain the code Claude read) and worktrees stay on
this machine. Once a day the supervisor removes those of runs that finished more than
`runtime.retention_days` (default 30) ago, as `coordinator clean --older-than` would: unpushed
changes are saved as a patch first, and nothing unfinished, held or open for questions is
touched. `retention_days = 0` keeps everything until you clean by hand.

## Run states

| State | Meaning | What happens next |
|---|---|---|
| discovered / starting | Prepared; start transition being made | Restarted automatically after a crash (revalidated first) |
| running | Claude session working | Heartbeat every `heartbeat_seconds`; stops if the ticket is reassigned, moved or cancelled; starts again by itself if its inputs are edited |
| publishing | Decision made; committing, commenting, transitioning | Every step journaled; uncertain steps reconciled by marker before any retry |
| awaiting_human | Published; ticket is in a review or paused status | Nothing until a human acts |
| interrupted | Stopped mid-work | Not held: resumes on the next supervisor start, or on the poll once its retry time comes. Held: needs `delivery recover <KEY> --resume` |
| interrupted, waiting for Claude | Claude's login expired or the usage limit was reached | Resumes by itself once a one-word Claude probe works (checked every 1 to 15 minutes); new work waits meanwhile |
| blocked | Ticket moved to Blocked with a reason | A human fixes the cause and chooses Resume in Jira, or chooses Request resolution to clear it with Claude (a `resolution` run: it ends `completed` when the ticket returns to the stage that blocked, `blocked` when the cause could not be cleared) |
| failed | A definite failure the coordinator could not publish around | `delivery inspect` explains; fix, then `delivery recover` |
| completed | A resolution run that cleared its blocker (a released ticket has no run: the merge moves it to Done) | — |

## Stopping

- **Everything:** `coordinator stop`, or `Ctrl-C` where it is shown (SIGINT, SIGTERM and SIGHUP,
  so also closing a terminal it runs in directly). Every child process group is terminated,
  every run is checkpointed as interrupted (not held), nothing is published, and the lock is
  released. The next start resumes those runs with fresh sessions from durable inputs; a
  development run continues in its worktree and is told about the changes already there.
  `coordinator restart` does both, to pick up new code.
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

A run that fails inside the coordinator (an internal error) is held, comments on the ticket with
this command (not the error, unless `[notifications] operational = "operator"`, which skips the
comment), alerts, and records the traceback in its `events.jsonl` and the coordinator log.

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
  supervisor/<identity>/supervisor.json   dispatch pause, socket, last poll, code stamp,
                                          waiting for Claude
  supervisor/<identity>/events.jsonl
  supervisor/<identity>/coordinator.log   what the coordinator printed, warnings, errors
  coordinator-tmux.conf                   the coordinator's own tmux server settings
  runs/<KEY>/<run-id>/
    snapshot.json                         current run record
    events.jsonl                          append-only events and publication intents/results
    inputs/envelope-<procedure>.json      exactly what the worker was given
    output/<procedure>/                   what the worker wrote
    claude-<procedure>.json               CLI outcome, plugins, permission denials
    logs/claude-<procedure>.jsonl         full CLI stream (secrets redacted)
    logs/<target>-<check>.log             coordinator check output
    settings-<procedure>.json             the generated permission profile
    leftover-<worktree>.patch             unpushed changes `coordinator clean` saved
  intake/<KEY>/                           one-off explanation comments for waiting tickets
  acceptance/<KEY>.json, acceptance/<KEY>/ the app running for a ticket in Acceptance review
  guidance/                               project guidance notes added from this machine
  proposals/<KEY>.json                    tickets created with CREATE TICKETS
  stale/, reminders/                      out-of-date and reminder comments already posted
  reverts/<KEY>.json                      `delivery revert` results
  try/<KEY>-<pid>/                        the script and log of a `delivery try`
  locks/                                  supervisor, ticket and repository locks
<worktree_root>/<KEY>/<run-id>/           worktrees (removed when a run finishes;
                                          `coordinator clean` removes any left behind)
<worktree_root>/<KEY>/acceptance-c<n>/    the approved candidate during Acceptance review
<worktree_root>/_repo.git                 the coordinator's own clone
<worktree_root>/_guidance/                short-lived worktree for the guidance branch
```

All directories are created with mode 0700 and files 0600. Do not commit anything from here.

## Common situations

| Situation | Action |
|---|---|
| Subscription limit reached | Nothing blocks: the run waits, new work waits, and both carry on by themselves after the reset |
| Claude login expired | `claude auth login` in a terminal; waiting runs carry on within a minute or two |
| The coordinator stopped unexpectedly | `coordinator status` says so; `coordinator logs` has the error; `coordinator` starts it again |
| Switched to another application repository | `delivery setup` changes it and offers to move the old `worktree_root` aside (it holds the coordinator's clone of the old repository, which the coordinator will not use); then `coordinator restart`. If old tickets still have runs there, finish or close them first |
| Worktrees and logs pile up | `coordinator clean` lists what finished runs left and removes it (`--older-than DAYS` for old logs) |
| Base branch moved after code approval | The code gate blocks (head or CI no longer current). Request code changes → Submit implementation changes merges the base into the candidate (no rebase) and re-verifies |
| Merge conflict with the base or another ticket | Flagged in the candidate, code-review and verification comments, never a failure. Resolve it in the PR when merging (for example GitHub's Resolve conflicts); the Done comment says the approved candidate was merged with the base and lists the files the resolution changed. Any other commit added to the PR is flagged there as unapproved changes (the ticket still moves to Done: the merge has happened). Or request code changes: the next development run merges the latest base and a short Claude session resolves a conflict with it (left out and still flagged if it cannot) |
| Main moved on while a candidate waits for review | The ticket gets one "may be out of date" comment when the new commits touch the same files or conflict. Submit follow-up changes verifies the same candidate again on the latest base |
| A released change must come out | `delivery revert <KEY> --reason "…"`: a revert PR to review and merge, and a linked Bug for the rework |
| Reviews wait too long | `delivery team` shows every ticket by what it waits on; reminders comment after `[reminders] after_hours` |
| Verification failed and the next step is unclear | `delivery inspect <KEY>`: every reason and finding in full, failed check output, Claude logs and what each Jira action available now does |
| Overlap warning | Work continues. For a higher-risk one (shared interface, schema, migration or dependency) agree which ticket merges first; Revise scope or a `FOR CLAUDE` note if one ticket should change approach |
| Remote ticket branch diverged | The ticket blocks. Reconcile the branch by hand (never force-push), then Resume |
| Jira or GitHub offline | Polling backs off; nothing is mutated; publication resumes when Jira is back. If the application repository cannot be cloned or fetched when the supervisor starts (no network, GitHub down, expired credentials), it keeps running and logs "could not reach the application repository (…); retrying in Ns", retrying with the same backoff (15s doubling to 10 minutes). Nothing is reconciled or started until it succeeds; Ctrl-C or SIGTERM still stops it. Fix credentials if the error names them; the next retry picks it up |

## Delivery branches and documentation consolidation

`delivery/<KEY>` branches are retained for audit and are never deleted or merged automatically.
Revision files are append-only; corrections are new revisions. If the team wants approved
specifications, plans or ADRs in the main documentation, a human opens a normal PR that copies
the chosen revisions into `docs/` on the base branch and reviews it like any other change. The
coordinator never does this, so approved code SHAs are never moved by documentation commits.
