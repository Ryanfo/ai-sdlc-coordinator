# How people decide, and the request comment

**A decision is a Jira move.** Approving, asking for changes, answering questions and resuming
all happen by choosing the action in Jira (or dragging the card): no comment is needed, and
there is no token to copy. Rules:

- The move counts when it is made by someone allowed to decide, after the revision or candidate
  it decides on was posted. Who may decide is `[approvals] jira_account_ids`. Empty (the default)
  means anyone who can move the ticket. An automated transition never counts.
- A comment alone never starts work.
- Comments are what you want to say, in your own words. Everything written on the ticket since a
  revision was posted reaches Claude with the next stage: as what to change after a change
  request, as answers after Submit answers, or as notes after an approval ("fine, but call it
  Download"). Numbering is optional: `F2: ...` or `D1: ...` lines keep their ID, and a comment
  without them is one item.
- Asking for changes without writing anything is fine: Claude asks what to change (in the open
  session, or as questions in Jira).
- If a kept-open session publishes a newer revision while the ticket is in review, its comment
  says so, and the next move approves that newer revision.

| Situation | Choose | Comment (optional unless noted) |
|---|---|---|
| Answer questions | Submit <stage> answers | Your answers, in your own words (`Q2: ...` to name a question) |
| Approve specification / plan | Approve specification / Approve plan | none |
| Change specification / plan | Request specification / plan changes | What to change |
| Approve code (after an independent GitHub review with CI passing) | Approve code | none; approving also accepts any deviations listed |
| Request code changes | Request code changes, then Submit implementation changes | What to change; `D1: follow the specification` changes a deviation back. Unresolved PR review conversations are included too |
| Accept delivery | Accept delivery | none |
| Request behavioural changes | Request acceptance changes, then Submit implementation changes | What to change |
| Fix only some findings (R-items are always fixed) | Submit implementation changes | Say which, e.g. `only F1 and F3` |
| Re-verify the same candidate (no code change) | Submit follow-up changes (from Changes requested) | none |
| Change scope after review | Revise scope | What to change |
| Tell Claude something for its next session | Resume, Submit … as usual | `FOR CLAUDE` or `FOR CLAUDE development` + your note |
| Tell every Claude session on every ticket | nothing | `FOR CLAUDE project` + your note |
| Create tickets Claude proposed | nothing | **Required**: `CREATE TICKETS <token>` + the IDs (below) |

## Deviations from the specification

Something changed during development that works but is not what the approved specification
says. The review lists them as `D1`, `D2`… and they never fail verification. Approving the code
(and accepting the delivery) accepts them as built: the specification is not rewritten, and the
ticket's Done comment names them. To have one changed back instead, request code changes and name it in a
comment with what to do (`D2: keep the specification's wording`). A deviation nobody names is
left as it is.

## Notes for Claude

Not a decision: every following Claude session of that stage (or of every stage, without a stage
name) gets the note as input, oldest first, from the assignee or an approver. It never starts
work by itself; choose the Jira action as usual. Stage names: `refinement`, `planning`,
`development`, `verification`, `release`.

```text
FOR CLAUDE development
The e2e failure is the date picker's timezone; use the fixed clock in tests/clock.ts.
```

Guidance for every ticket. When the same correction keeps coming up, write it once: the
coordinator adds it to the project's guidance file (the `delivery/guidance` branch of the
application repository) and confirms on the ticket. Every Claude session on every ticket and
every developer's machine reads it. Edit or remove entries there; `delivery guidance` shows it.

```text
FOR CLAUDE project
Dates go through src/lib/dates.ts; never call new Date() in components.
```

## Review comments on the pull request

When changes to a candidate are submitted, the PR's unresolved review conversations, and written
reviews, from since the candidate was published reach development as `G1`, `G2`… items next to
anything written in Jira. Resolve a conversation on GitHub to leave it out. When the PR comments
say it all, just choose Request code changes.

## The request comment

This is not a decision, so it keeps a fixed first line with the token of the revision it is
about. The coordinator shows the token in the review comment that proposes tickets
(`delivery inspect <KEY>` lists every gate's token too). There is no comment for releasing: the
merge of the PR is the release, which the coordinator reads from GitHub.

Proposed tickets. A specification may propose slices of a ticket too big for one delivery, and
a spike's findings may propose follow-up work, as `S1`, `S2`… in their review comment. Nothing
is created until someone asks, with that revision's token (`SPEC`, or the findings' `PLAN`) and
the IDs to create. A note after an ID goes into that ticket's description. They are created in
Backlog, unassigned and linked to this ticket; this ticket carries on as it is.

```text
CREATE TICKETS PILOT-123-SPEC-v2
S1
S3: call it Export to CSV
```

## What is never accepted

- A move made before the revision or candidate it decides on was posted, or for a revision that
  a newer one has replaced.
- When approvers are listed: a move made by an account not on the list.
- An automated transition (no human author).
- A comment alone: it never decides anything.
- Code approval without an independent, non-author GitHub approval on the current PR head with
  required CI passing.
