# Jira workflow setup (administrator)

Version 1.1 · matches delivery-platform 0.1.0 · audience: Jira administrator and delivery lead

This is the shipped version of the original setup instructions (`docs/design/Jira_Workflow_Setup_Instructions.md`),
aligned with what the coordinator actually checks. Configure it once per project, then run
`delivery workflow inspect` to verify it before anyone runs a supervisor.

The parallel-sessions amendment needs **no extra statuses or columns**: many tickets simply sit
in the existing statuses at the same time.

## 1. Project

Create **Jira Software → Kanban → Company-managed**, for example *AI SDLC Pilot* with key `PILOT`
(needs *Administer Jira*). For an existing project, copy its workflow and scheme first; never
edit a workflow shared with unrelated teams.

**Plan tier.** On Jira Free you cannot configure permission schemes or roles, so approver-only
and worker-only transitions cannot be enforced by Jira. The coordinator still validates every
decision (authorised comment author **and** transition author), but say so in the setup profile.
Strict worker/human separation needs a paid plan and a separate worker account.

## 2. People

| Role | Needs |
|---|---|
| Administrator | Workflow, field, scheme and board configuration (setup only) |
| Developer (runs a supervisor) | Browse, comment, transition, edit issue properties; assignable |
| Approver | Comment and perform approval transitions; listed in each developer's config |
| GitHub reviewer | A human other than the PR author |
| Release owner | Merges, releases, records the release |

The supervisor authenticates as the developer by default. Jira then records the developer's
account for both coordinator and human transitions; the coordinator never performs a human
route, so this is safe for the pilot but cannot be enforced by Jira itself.

## 3. Statuses

The coordinator resolves statuses **by these exact names** (`delivery workflow inspect` prints the
IDs). Names must be unique in the project.

| Status | Category | Kind |
|---|---|---|
| Backlog | To Do | human |
| Ready for refinement | To Do | ready (machine queue) |
| Refining | In Progress | agent active |
| Specification review | In Progress | human review |
| Ready for planning | To Do | ready |
| Planning | In Progress | agent active |
| Plan review | In Progress | human review |
| Ready for development | To Do | ready |
| Developing | In Progress | agent active |
| Ready for verification | To Do | ready |
| Verifying | In Progress | agent active |
| Code review | In Progress | human review |
| Acceptance review | In Progress | human review |
| Changes requested | In Progress | human review |
| Needs clarification | In Progress | paused |
| Blocked | In Progress | paused |
| Ready for release preparation | To Do | ready |
| Preparing release | In Progress | agent active |
| Release review | In Progress | human review |
| Ready for release | To Do | human release gate (not a machine queue) |
| Ready for release verification | To Do | ready |
| Verifying release | In Progress | agent active |
| Done | Done | terminal (set resolution) |
| Cancelled | Done | terminal (set resolution) |

## 4. Transitions

Create a dedicated workflow **AI SDLC v1**. The create transition leads to Backlog. Do **not**
add global "any status" transitions. Use exactly these names (they are the coordinator's
defaults; if you must rename, put the new names under `[workflow.actions]` in every developer's
config).

### Main lifecycle

| From | Name | To | Performed by |
|---|---|---|---|
| Backlog | Submit for refinement | Ready for refinement | human |
| Ready for refinement | Start refinement | Refining | coordinator |
| Refining | Complete refinement | Specification review | coordinator |
| Specification review | Approve specification | Ready for planning | approver |
| Ready for planning | Start planning | Planning | coordinator |
| Planning | Complete planning | Plan review | coordinator |
| Plan review | Approve plan | Ready for development | approver |
| Ready for development | Start development | Developing | coordinator |
| Developing | Complete development | Ready for verification | coordinator |
| Ready for verification | Start verification | Verifying | coordinator |
| Verifying | Complete verification | Code review | coordinator |
| Code review | Approve code | Acceptance review | approver |
| Acceptance review | Accept delivery | Ready for release preparation | approver |
| Ready for release preparation | Start release preparation | Preparing release | coordinator |
| Preparing release | Complete release preparation | Release review | coordinator |
| Release review | Approve release | Ready for release | release owner |
| Ready for release | Record release | Ready for release verification | release owner |
| Ready for release verification | Start release verification | Verifying release | coordinator |
| Verifying release | Complete release verification | Done | coordinator |

### Changes and failed verification

| From | Name | To |
|---|---|---|
| Specification review | Request specification changes | Ready for refinement |
| Plan review | Request plan changes | Ready for planning |
| Verifying | Verification failed | Changes requested |
| Code review | Request code changes | Changes requested |
| Acceptance review | Request acceptance changes | Changes requested |
| Changes requested | Submit implementation changes | Ready for development |
| Changes requested | Revise scope | Ready for refinement |
| Release review | Request release changes | Ready for release preparation |

### Pausing and resuming

From **each of the six agent-active statuses** add `Ask questions → Needs clarification` and
`Block stage → Blocked` (same names from every status are fine; the coordinator matches name
**and** destination).

From both paused statuses add six resume transitions:

| Delivery resume stage | From Needs clarification | From Blocked | To |
|---|---|---|---|
| refinement | Submit refinement answers | Resume refinement | Ready for refinement |
| planning | Submit planning answers | Resume planning | Ready for planning |
| development | Submit development answers | Resume development | Ready for development |
| verification | Submit verification answers | Resume verification | Ready for verification |
| release_preparation | Submit release preparation answers | Resume release preparation | Ready for release preparation |
| release_verification | Submit release verification answers | Resume release verification | Ready for release verification |

### Cancel

From every unfinished status add **Cancel → Cancelled**. If a ticket is cancelled while an agent
is working, the coordinator detects it at its next heartbeat, terminates that one session and
publishes nothing further.

## 5. Delivery resume stage field

Create a **single-select** custom field *Delivery resume stage* with options exactly
`refinement`, `planning`, `development`, `verification`, `release_preparation`,
`release_verification`, scoped to the project's issue types. Add it to the issue screens (the
coordinator writes it; humans should not edit it). `delivery workflow inspect` prints its
`customfield_…` ID.

On each resume transition add a **Value field condition**: *Delivery resume stage* equals the
matching option. Jira then shows only the correct resume action. The coordinator sets the field
before pausing and clears it when the stage restarts. Without the field (or on plans where you
cannot add conditions) the coordinator still rejects a wrong resume: it moves the ticket to
Blocked with the correct resume stage and explains which action to use.

## 6. Conditions and validators (paid plans)

- Approval transitions (Approve specification/plan/code, Accept delivery, Approve release,
  Record release): restrict to the approver group or role.
- Submit/Resume transitions: assignee or approver.
- Start/Complete/Ask questions/Block stage/Verification failed: if you use a separate worker
  account, restrict them to it. Do **not** add *Only assignee* to worker transitions when the
  worker is a separate service account.
- A "comment required" validator cannot check the token; the coordinator does that.

## 7. Resolution

Set the resolution on **Done** and **Cancelled** (post-function or transition screen). Every
other status must have an empty resolution. There is no automatic reopen from Done.

## 8. Scheme and board

Create a workflow scheme mapping Story, Task and Bug (or only the pilot types) to *AI SDLC v1*,
associate it with the project and publish. Keep Epics on their own workflow.

Board columns (columns are visual only; the coordinator triggers on exact statuses):

| Column | Statuses |
|---|---|
| Backlog | Backlog |
| Ready | the six Ready for… statuses (not Ready for release) |
| Agent working | Refining, Planning, Developing, Verifying, Preparing release, Verifying release |
| Needs clarification | Needs clarification |
| Human review | Specification review, Plan review, Code review, Acceptance review, Release review, Changes requested |
| Blocked | Blocked |
| Ready for release | Ready for release |
| Done | Done, Cancelled |

Add a *My tickets* quick filter (`assignee = currentUser()`). No Jira Automation or webhooks are
needed: each developer's supervisor polls.

## 9. Verify with the coordinator

Any developer with a config (statuses can be left empty at first):

```bash
delivery workflow inspect --config ~/delivery.local.toml
```

It reports missing or duplicate status names, category problems, the resume field ID and, for
every status that currently has at least one ticket, the transitions actually offered compared
with this document (missing and unexpected, including global transitions). Create a throwaway
ticket and move it through the statuses to get full transition coverage, then:

```bash
delivery doctor --config ~/delivery.local.toml
```

## 10. Hand these values to each developer

- Site URL, project key, supported issue types, whether an opt-in label is used.
- The generated `[workflow.statuses]` block and the resume field ID.
- Approver account IDs and the GitHub reviewer logins.
- The application repository URL, base branch, check commands and CI job names.
- The chosen auth profile (secrets are exchanged separately, never in documents).

## 11. Acceptance checks

| Check | Expected | How |
|---|---|---|
| New ticket starts in Backlog | No work starts | `delivery run --dry-run` shows nothing for it |
| Assigned ticket moved to Ready for refinement | Picked up only by the assignee's supervisor | Another developer's dry run skips it ("assigned to another account") |
| Review, Needs clarification, Blocked | Never trigger work | Dry run |
| Change request from a review status | Returns to the right ready status with the feedback | Request specification changes after a `CHANGE SPEC` comment |
| Clarification from planning | Only *Submit planning answers* offered (with field conditions) | Inspect the available transitions |
| Wrong resume or missing approval | Refused by Jira, or the coordinator blocks with an explanation | Try it on a test ticket |
| Unauthorised approval | Rejected | Approve as a non-approver |
| GitHub review and CI | Must match the current PR head | Push a commit after approval: the coordinator blocks |
| Scope amendment | Downstream approvals superseded | Revise scope after acceptance |
| Done/Cancelled | Resolution set; no paused status sets one | Check issues |
| Ordinary developers | Need no admin rights | Doctor as a developer |
| Other projects | Unchanged | Check their workflow schemes |
