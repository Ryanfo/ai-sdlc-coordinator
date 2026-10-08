# Live pilot runbook (M9)

Goal: take a real ticket through the whole lifecycle against the real Jira project and GitHub
repository, plus concurrent sessions and a controlled overlap, and keep the evidence. Run on
the pilot project and repository only.

## Setup checklist (fill in)

| Item | Value | Done |
|---|---|---|
| Jira site URL | https://anddigitaltransformation.atlassian.net | ☑ |
| Project key | SDLC (team-managed; Story and Bug carry the delivery workflow) | ☑ |
| Jira plan (Free / Standard / Premium) | company paid plan | ☑ |
| Workflow configured per `docs/jira-workflow-setup.md`; `workflow verify` clean | 2 Oct 2026: 31 transitions walked, all routes match (resume actions not hidden by field conditions; coordinator enforces) | ☑ |
| Delivery resume stage field ID | customfield_11807 | ☑ |
| Developer account ID (supervisor identity) | 5b51e9f46a66cf638d2cb417; token in macOS Keychain (`delivery-jira`) | ☑ |
| Approver account ID(s) (not the developer) | pilot: the developer approves (accepted limitation; doctor warns) | ☑ |
| Second developer identity (for cross-developer overlap) | | ☐ |
| Pilot repository URL (public, generic task-list app) | https://github.com/Ryanfo/delivery-pilot-app | ☑ |
| Base branch protection: PR, 1 review, stale dismissal, required checks lint/typecheck/unit/build/e2e, up to date, no force push or deletion | applied 1 Oct 2026 (admins included) | ☑ |
| Independent GitHub reviewer login | | ☐ |
| `claude auth login` done; `doctor --claude-probe` READY | | ☐ |
| `delivery doctor` READY; `run --dry-run` clean | 2 Oct 2026 | ☑ |

## 1. Foundation (human-owned, before any feature ticket)

The pilot app foundation (`delivery-pilot-app`) is pushed to `main` by a human with its CI and
branch protection. Feature tickets never change `.github/` or protections.

## 2. Main ticket

Create in Backlog, assigned to the developer:

```text
Summary: Add title search to the task list
Problem: Users need to find a task quickly without scanning the whole list.
Scope: Search the checked-in synthetic task data only.
Excluded: External APIs, authentication and deployment.
AC1: Title search matches without case sensitivity.
AC2: Clearing the search restores the full list.
AC3: No matches show an explicit empty-results message.
AC4: Search can be used with a keyboard and has a visible label.
Open question: Should search include the description text as well as the title?
```

1. Create a second ticket assigned to someone else and submit it: confirm the developer's
   supervisor ignores it (`delivery inspect` shows "assigned to another account").
2. Submit for refinement. Expect a Questions comment for the open question. Answer it in a plain
   comment and choose Submit refinement answers.
3. Review specification v002. Write one change in a comment (for example an extra acceptance
   criterion) and choose Request specification changes; receive v003. Approve v003 by choosing
   Approve specification (no comment).
4. Review and approve the plan (note its footprint link).
5. Watch development open the PR, then verification run review, coordinator checks on the
   candidate and on the integration tree, verify, and CI.
6. Fault loop without breaking the real feature: use the dedicated fixture ticket in step 4
   below, not this ticket.
7. The independent reviewer approves the PR on GitHub; the approver chooses Approve code, then
   Accept delivery. Confirm the Jira thread has no human decision comments.
8. Accept delivery (with `[release] proposal = true`, approve the release proposal too). A human
   merges the PR. Within a poll the coordinator reads the merge commit from GitHub and moves
   the ticket to Done itself.
9. Confirm the Done comment says the merge contains the accepted candidate.

## 3. Concurrency

Create three small independent tickets (for example "show task count by status", "add a
priority badge", "add a due-date label"), all assigned to the developer, and submit them
together. Evidence: `delivery status` lists three sessions at once with different run IDs,
worktrees and ports; all three progress independently.

## 4. Controlled overlap and fault loop

- Two developers each take a ticket that changes `src/domain/filter.ts` (for example "filter by
  priority" and "filter by due date"). After both plans publish, both tickets carry the same
  deduplicated overlap warning. If both plans declare the `TaskQuery` interface, the warning
  is marked higher risk; neither ticket is paused.
- Fault loop: a fixture ticket "Add a deliberately failing example check" whose brief says the
  implementation must make `npm run test:unit` fail once. Verification moves it to Changes
  requested with findings; Submit implementation changes produces candidate c2.

## 5. Recovery

- Start a refinement, press Ctrl-C while the session runs, restart: the run resumes with a fresh
  session and one "started" comment.
- Recovery from an uncertain publication is exercised by the automated fixture tests
  (`tests/integration/test_recovery.py`); do not manufacture a production failure.

## Evidence to keep

For each ticket: Jira key and final status; links to specification/plan/review/verification and
release documents (commit-pinned); PR URL, candidate SHA(s), merge commit, released commit; CI
run URLs; `delivery status` output during concurrency; overlap comments; the release
verification comment. Record anything skipped and why in `docs/completion-report.md`.
