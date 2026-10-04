# Human decision templates

Generated from `src/delivery/feedback.py` (the parser uses the same grammar). The coordinator
posts each template with the **current** token in Jira; copy it from there. Rules:

- Put the template on the first line of the comment; Jira code blocks are fine.
- Then perform the matching Jira action. A comment alone never starts work.
- Tokens are bound to one revision or candidate. A newer revision supersedes older tokens.
- Who may decide is `[approvals] jira_account_ids`. Empty (the default) means anyone who can
  comment on and move the ticket. With a list, approvals come from a listed approver, who also
  makes the transition. Either way an automated transition never counts.
- Do not edit a decision comment; add a new one. Edited decisions are treated as conflicts.
- Several comments can answer one round; later answers to the same question win.

| Situation | Comment | Then choose |
|---|---|---|
| Answer questions | `ANSWERS PILOT-123-REFINE-R1 + Q1:, Q2:` | Submit <stage> answers |
| Approve specification | `APPROVE SPEC PILOT-123-SPEC-v2` | Approve specification |
| Change specification | `CHANGE SPEC PILOT-123-SPEC-v2 + F1:, F2:` | Request specification changes |
| Approve plan | `APPROVE PLAN PILOT-123-PLAN-v1` | Approve plan |
| Change plan | `CHANGE PLAN PILOT-123-PLAN-v1 + F1:` | Request plan changes |
| Approve code (after a GitHub review) | `APPROVE CODE PILOT-123-CODE-c2` | Approve code |
| Request code changes | `CHANGE CODE PILOT-123-CODE-c2 + F1:` (and `D1:` for a deviation to change back; no items needed when the PR's review comments say it all) | Request code changes |
| Accept deviations from the specification | `ACCEPT DEVIATIONS PILOT-123-CODE-c2` (+ `D1, D3` to accept only some) | Submit follow-up changes (from Code review), or the next action you choose |
| Accept delivery | `ACCEPT DELIVERY PILOT-123-ACCEPT-c2` | Accept delivery |
| Request behavioural changes | `CHANGE ACCEPTANCE PILOT-123-ACCEPT-c2 + F1:` | Request acceptance changes |
| Select which findings to fix (optional; R-items are always fixed) | `SUBMIT CHANGES PILOT-123-CODE-c2 + F1:, F3:` (and `D2:` to change a deviation back) | Submit implementation changes |
| Re-verify the same candidate (no code change) | none | Submit follow-up changes (from Changes requested) |
| Tell Claude something for its next session | `FOR CLAUDE` or `FOR CLAUDE development` + your note | Resume, Submit … as usual |
| Tell every Claude session on every ticket | `FOR CLAUDE project` + your note | nothing: it is added to the project guidance |
| Create tickets Claude proposed (slices or a spike's follow-ups) | `CREATE TICKETS PILOT-123-SPEC-v2 + S1, S3` (or the findings' `PLAN` token) | nothing: they are created in Backlog |
| Change scope after review | `REVISE SCOPE PILOT-123-SPEC-v3 + F1:` | Revise scope |
| Approve release proposal | `APPROVE RELEASE PILOT-123-RELEASE-v1` | Approve release |
| Change release proposal | `CHANGE RELEASE PILOT-123-RELEASE-v1 + F1:` | Request release changes |
| Record a release by hand (optional: the coordinator records the PR merge itself) | `RECORD RELEASE PILOT-123-RELEASE-v1 + commit:, environment:` | Record release |

## Examples

Clarification answers:

```text
ANSWERS PILOT-123-REFINE-R1
Q1: <your answer>
Q2: <your answer>
```

Specification change request (one or more numbered items):

```text
CHANGE SPEC PILOT-123-SPEC-v2
F1: <requested change>
F2: <another change>
```

Approval:

```text
APPROVE SPEC PILOT-123-SPEC-v2
```

Release record. Not needed for an ordinary release: once the PR is merged, the coordinator
reads the merge commit from GitHub and chooses Record release itself. If this comment is on the
ticket when the release is recorded, its commit is verified instead of the merge commit.

```text
RECORD RELEASE PILOT-123-RELEASE-v1
commit: <released commit SHA on the base branch>
environment: local-pilot
merged-pr: <PR number>
```

Deviations from the specification (something changed during development that works but is
not what the approved specification says). The review lists them as `D1`, `D2`… and they
never fail verification. Accept all of them, or only the ones listed:

```text
ACCEPT DEVIATIONS PILOT-123-CODE-c2
D1, D3
```

Claude then rewrites the specification to include them and publishes it as the approved
revision, with no new refinement or planning round. To have one changed back instead, name it
in the change request with what to do (`D2: keep the specification's wording`). A deviation
nobody names is left as it is, but release preparation waits until each one is decided.

Guidance for Claude. Not a decision and needs no token: every following Claude session of
that stage (or of every stage, without a stage name) gets the note as input, oldest first, from
the assignee or an approver. It never starts work by itself; choose the Jira action as usual.
Stage names: `refinement`, `planning`, `development`, `verification`, `release`.

```text
FOR CLAUDE development
The e2e failure is the date picker's timezone; use the fixed clock in tests/clock.ts.
```

Review comments on the pull request. When changes to a candidate are submitted, the PR's
unresolved review conversations, and written reviews, from since the candidate was published
reach development as `G1`, `G2`… items next to your `F` items. Resolve a conversation on GitHub
to leave it out. A `CHANGE CODE` comment needs no items of its own when the PR comments say it
all:

```text
CHANGE CODE PILOT-123-CODE-c2
See the review comments on the pull request.
```

Guidance for every ticket. When the same correction keeps coming up, write it once: the
coordinator adds it to the project's guidance file (the `delivery/guidance` branch of the
application repository) and confirms on the ticket. Every Claude session on every ticket and
every developer's machine reads it. Edit or remove entries there; `delivery guidance` shows it.

```text
FOR CLAUDE project
Dates go through src/lib/dates.ts; never call new Date() in components.
```

Proposed tickets. A specification may propose slices of a ticket too big for one delivery,
and a spike's findings may propose follow-up work, as `S1`, `S2`… in their review comment.
Nothing is created until someone asks, with that revision's token and the IDs to create. A note
after an ID goes into that ticket's description. They are created in Backlog, unassigned and
linked to this ticket; this ticket carries on as it is.

```text
CREATE TICKETS PILOT-123-SPEC-v2
S1
S3: call it Export to CSV
```

## What is never accepted

- Free text such as "looks good, approved": approval needs the exact token.
- A token for an older revision or candidate.
- When approvers are listed: a decision by an account not on the list, or a transition made by one.
- An automated transition (no human author).
- Both an approval and a change request for the same token: a human resolves the conflict.
- Code approval without an independent, non-author GitHub approval on the current PR head with required CI passing.

