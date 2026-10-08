---
name: resolve-blocker
description: Clear the blocker that stopped a delivery ticket, working with the developer in this session. Invoked by the delivery coordinator when a developer asks for a Blocked ticket to be resolved.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

> Ports: `PORT` and `E2E_PORT` are already exported for this run. Run commands as plain
> `npm run …` / `npx …` without `VAR=value` prefixes (prefixed commands are denied).

# Procedure: resolve-blocker

contract_id: `delivery.resolve-blocker/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

A stage of this ticket stopped and the ticket is Blocked. The developer who runs the
coordinator asked for it to be resolved with you. Find out why it blocked and clear the cause so
that the stage (`resolution.blocked_stage`) can run again. The coordinator moves the ticket back
to that stage when you finish; if you could not clear the cause it goes back to Blocked with what
you found. Either way, the coordinator writes who made each decision on the ticket. Jira is for
decisions only: what you did stays in this session's transcript.

A person is in this session. Use that: it is what makes this different from a normal stage.

## Steps

1. Read `resolution.briefing_path`. It has the blocker as the coordinator recorded it, **how the
   coordinator reads this ticket** (its gates, every request comment already on the ticket and
   whether the coordinator uses, supersedes, ignores or does not recognise it), the blocked
   run's result and transcript tail, failed check output and the recent comments.
   `human-templates.md` next to it (`inputs/`) explains how people decide (by moving the
   ticket) and the exact format of the two request comments. If `prior_work` is set, the blocked stage left unfinished changes
   in the working copy; read `prior_work.session_tail_path` to see where it stopped.
2. Find the cause, not a cause. Read the evidence the coordinator used (the reason names it) and
   the ticket state above before you read anything else: most blockers that need a person are a
   move the coordinator rejected (made by someone not allowed to decide, or before the current
   revision was posted) or a request comment it read differently from how its author meant it
   (a CREATE TICKETS comment with a quote or sentence before the request line).
   Say what you checked.
3. Decide whether you can clear it from here (see *What you can and cannot change*). If you can,
   do it in small steps and run the relevant tests or checks to show it worked.
4. **Ask the developer** with the AskUserQuestion tool whenever the cause or the fix depends on a
   choice that is theirs to make: which of two reasonable fixes, which commit is deployed,
   whether to change behaviour the specification leaves open. Ask one decision at a time, offer
   the options you found with a recommendation, and wait. Ask *before* you act on it. Do not ask
   what you can find out yourself, and do not ask for approval of every step.
5. When the cause is cleared, or you are sure it cannot be cleared from here, tell the developer
   in a few lines what you found and did, then write the result. Ask every question first: the
   session is closed when you hand over the result.

## What you can and cannot change

- You work in the ticket's feature worktree. `resolution.code_changes_carried` says whether
  changes you make there are carried into the stage that blocked. When it is false, do not change
  files: explain what must change and return `blocked` (see below), or fix the cause by other
  means that the developer does for themselves and you confirm.
- Never change the approved specification or plan, or the acceptance criteria, to make a blocker
  go away. If the real fix is a change of scope, return `blocked` and say that the developer
  should use **Revise scope** (or request specification or plan changes) in Jira, and why.
- Do not edit `.github/`, `.claude/`, `CLAUDE.md`, `.mcp.json` or `docs/delivery/`. Do not
  commit, push or change Git configuration: the coordinator commits. Do not weaken, skip or
  delete tests to make them pass.
- You cannot reach Jira or GitHub. The coordinator posts to Jira. If the cause is outside this
  worktree (the coordinator's configuration, a GitHub setting, a credential), tell the developer
  exactly what to do and return `blocked`; do not look for workarounds.

## Recording decisions (this is how the ticket shows who decided)

Every choice that shaped the fix goes in `resolution.decisions`, each with an ID `D1`, `D2`, ...:

- `decided_by: "developer"` only for a choice the developer made in this session: they picked an
  option when you asked, or told you what to do. Put their words, or the option they picked, in
  `decision`. Never mark a choice as the developer's because they did not object.
- `decided_by: "claude"` for a choice you made yourself without asking, with the reason in
  `rationale`. Keep these few and small; a choice that matters is one to ask about.

The coordinator checks `developer` decisions against the session. A decision marked
`developer` that the session does not show is recorded in the ticket as Claude's.

## When a person has to act: next steps

If the cause is outside this working copy (a comment on the ticket, a Jira action, a command on
their machine), do not describe it loosely: return `blocked` with `resolution.next_steps`, the
exact steps in order. The coordinator posts them on the ticket as a numbered list with the text
ready to copy, so they must work exactly as written. It checks them before it lets you finish and
tells you here what is wrong; fix it and write the result again.

- `jira_comment`: the **whole comment**, character for character, and nothing else. Decisions
  (approving, asking for changes, answering, resuming) are never comments: they are
  `jira_action` steps. A comment is either what a person should write in their own words (what
  to change, an answer), or a request (`CREATE TICKETS <token>`) that
  starts with its request line with nothing before it: no quotes, no sentence around it, no
  "post this". Take the token from the gates in the briefing, never from memory or by counting
  up. A later request for the same token replaces an earlier one. Use the format in
  `human-templates.md`.
- `jira_action`: the action's name as Jira shows it on a Blocked ticket (`Resume release
  verification`). The ticket returns to Blocked when you finish, and the action must be one for
  the stage that paused.
- `command`: one command the developer runs, if it is not a Jira step.
- `other`: a sentence, for what has no exact form (for example that the scope needs revising and
  why).

Every step has `who` (usually the developer), `why` (what it changes) and `verified_by`: the check
you ran that shows it clears the cause, for instance the git command and its result. **Do not
return a step you have not checked.** If you cannot check it, you are not certain, so ask the
developer, look further, or say plainly in `blocker_reason` what is unknown. Order matters: put
the comment first, then the action that picks it up. If a person must act, the blocker is not
resolved, so it is `blocked`, never `completed`.

## Result

`procedure`: `resolve-blocker`. Put what you did in `resolution.actions` (one short sentence each,
in the order done, naming files where you changed them; it is kept in the session record, not
posted on the ticket) and anything optional for people in `resolution.follow_ups`.

- `outcome: completed`: the cause is cleared and the stage can run again, with nothing left for
  a person to do. `summary` says what the cause was and how it is cleared, in two or three
  sentences at most, for someone reading the ticket.
- `outcome: blocked`: it cannot be cleared from here. `blocker_reason` says what the cause is and
  why this session cannot clear it, briefly, without instructions (those are `next_steps`).
  `summary` is the same in brief. Changes you made in the working copy are kept for the stage
  either way, so leave it tidy.

Never return `needs_clarification`: questions belong in this session.
