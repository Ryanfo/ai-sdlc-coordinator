# Developer onboarding for the local Jira driven AI SDLC

> **Superseded in part (8 Oct 2026):** release preparation, release review and release verification no longer exist. The release is the human merge of the PR: Accept delivery moves the ticket to Ready for release, and the coordinator checks the merge and moves it to Done. References below to those stages, their statuses, the release proposal, `RECORD RELEASE` and `amend-spec` are historical. See `docs/jira-workflow-setup.md` and `docs/user-guide.md`.

README blueprint for Claude Code to implement and verify | Version 1.0 | 1 October 2026

**This is the intended onboarding guide, not a claim that the coordinator package already exists.** Claude Code must turn this into the delivery repository's root README, replace source locations with real ones, implement the specified commands and verify the instructions from a clean developer setup before removing this notice.

> **Superseded in part (7 Oct 2026):** human decisions are now the Jira move alone, with no token
> comment; comments are plain feedback. See "Decisions are moves" in
> [Implementation_Decisions.md](Implementation_Decisions.md) and
> [docs/human-templates.md](../human-templates.md). The token-comment rules below are history.

## What you will run

You run a Python coordinator locally. It watches an agreed Jira project for tickets assigned to your Jira account that enter a ready status. It starts your local Claude Code, publishes versioned artefacts and a feature PR, records results in Jira and pauses for human decisions.

The coordinator and generic delivery plugin come from the delivery-platform repository. Product architecture, standards, specifications, plans, reports and code belong to the application repository. Each developer has one local config file and one active coordinator for their Jira identity. There is no central scheduler in this setup.

## Before installing

Confirm with your delivery lead:

- The Jira workflow is configured using Jira Workflow Setup Instructions. New pilot: Jira Software Kanban, company-managed.
- You can read/comment on the project and perform the intended transitions.
- Your account ID is known, and tickets can be assigned to you.
- You have GitHub access to push ticket branches and open PRs in the application repository.
- The base branch and its required CI/review gates are configured.
- A second human can review your PR; you cannot satisfy that gate by approving your own work.
- You have Python 3.11+, Git and a supported local Claude Code installation.
- For the greenfield React pilot, Node and the app's documented dependency tooling are available.

macOS and Linux are the intended v1 environments. Native Windows requires explicit support; use a documented WSL profile if supplied. Check organisational restrictions before using tokens or repositories locally.

## 1 Install the delivery framework

Claude Code must replace this section with verified commands for the actual delivered package. Expected source-install interface once implemented:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
delivery --help
```

Run these from the cloned delivery-platform repository, using its tested dependency/lockfile instructions. The installer must expose the `delivery` command. Link the real clone URL and the supported version in the finished README; do not leave a fabricated repository URL.

Keep the application checkout separate. The framework creates managed worktrees rather than using your current uncommitted app checkout. Do not put local credentials, state or worktrees inside tracked source directories.

## 2 Confirm Claude subscription login

Sign in through the installed Claude Code's supported interactive login and confirm the active account/auth method. Do not paste your subscription credential into the coordinator config.

Subscription mode must not have an overriding API key, auth token, API helper, alternate provider or incompatible gateway/Console profile. The finished doctor command must detect effective auth conflicts rather than relying only on the fact that you logged in previously. No paid API fallback is enabled. A separately explicit smoke test may consume a small amount of subscription usage.

The framework must load the delivery plugin and use tested unattended permissions. A permission error is a blocked task; do not solve it by bypassing all permissions. The README must explain the implemented local trust boundary, including credential files and any required isolation profile.

## 3 Create one local configuration file

Expected implemented command:

```bash
delivery init --config ./delivery.local.toml
```

Keep the file outside version control. Fill in:

| Setting | Your value |
|---|---|
| Jira site and project | Provided by project administrator |
| Developer Jira account ID | Your stable account ID, not display name |
| Worker ID | Name identifying this registered laptop |
| Workflow status/action mapping | Administrator-provided actual IDs and actions |
| Resume stage field ID | Administrator-provided custom field |
| Application repository/base branch | Team's real GitHub repo and branch |
| Checkout/worktree/state paths | Absolute local paths or documented config-relative paths |
| Plugin path | Delivery-platform plugin directory |
| Check commands/CI names | Actual application gates |
| Approver/reviewer identities | Agreed human gate owners |
| Release profile | Local pilot or a deliberately supported team profile |

Credentials are referenced by environment variable or supported secure credential profile, never embedded in this file. Config, generated operational state and journal files must be excluded from Git. There is only one human-edited config; logs, cached IDs and generated permission settings are runtime data.

## 4 Configure integration authentication

Use the Jira auth profile agreed for your site. For an ordinary site API-token profile, the coordinator expects `JIRA_EMAIL` and `JIRA_API_TOKEN` securely supplied to its process. Scoped tokens/gateway profiles use their documented adapter; do not assume the same base URL works for all token types.

For the selected GitHub `gh` profile, sign in with the installed GitHub CLI and verify the selected repository identity. A direct-token adapter is also possible if documented. The coordinator can use write credentials for publication, but Claude subprocesses must not inherit those credentials.

The finished README must provide verified auth setup commands without putting real tokens in shell history, screenshots, config examples or logs. Runtime access is distinct from administrator access used to create the workflow.

## 5 Run preflight and dry-run

Expected implemented commands:

```bash
delivery doctor --config ./delivery.local.toml
delivery workflow inspect --config ./delivery.local.toml
delivery run --dry-run --config ./delivery.local.toml
```

Doctor checks identity, Jira route mapping, repository, plugin/CLI capabilities, checks, protections and local execution profile. It is read-only unless you explicitly invoke the documented Claude smoke test. Dry-run discovers eligible tickets without invoking Claude, posting comments, pushing branches or moving tickets.

Resolve missing/ambiguous status IDs, unsupported permissions, authentication conflicts and CI configuration before starting. Do not enable unattended use merely because the app can list tickets.

## 6 Start your coordinator

Expected implemented foreground command:

```bash
delivery run --config ./delivery.local.toml
```

Keep the terminal, laptop and network available. The coordinator polls on the configured interval, so a status change is not necessarily instantaneous. An eligible ticket that became ready while you were offline can be discovered on restart.

Do not run a second coordinator for your identity, including on another laptop. A same-machine process lock prevents local duplication; cross-machine ownership is an explicit operating constraint in v1, not a guaranteed distributed lock.

## 7 Submit your first ticket

Create the ticket in Backlog with a useful brief, scope/exclusions, numbered acceptance criteria and known constraints. Assign it to yourself. Add the opt-in label if your project profile requires it. Choose Submit for refinement.

The coordinator picks it up because the status and your assignee identity match. An approver may move your ticket forward later; it remains your worker's responsibility while assigned to you.

Expect a linked draft specification, questions if needed, a plan for approval, an implementation PR, independent verification reports and human review gates. Technical artefacts are in the app repository; the Jira ticket carries the discussion and links to exact versions.

## 8 Answer questions and request changes

Questions appear in Jira comments, with a round token, question IDs, draft link and next action. Copy the provided answer template, answer in comments, then choose Submit answers for the originating stage. Do not answer in an abandoned Claude terminal and assume the next run will see it.

For a specification change, comment using the current document token and numbered feedback, then choose Request specification changes. The coordinator snapshots those comment IDs and asks Claude to revise the existing draft. You do not need to edit the specification in GitHub.

Missing/ambiguous tokens or answers leave the task paused with a clear corrective action. A comment alone does not start a new execution.

## 9 Review and approve

Read the exact linked revision and use the supplied decision token before the approval transition. Specification and plan approvals apply to those versions, not any future edit.

At Code review, obtain a qualifying independent human GitHub review and passing checks for the current PR head, then complete the Jira action. Acceptance review is a separate human check against the original brief and acceptance criteria. A fresh agent review cannot substitute for either human decision.

After acceptance, review the release proposal. Humans merge and perform the agreed pilot release, then record the release commit/environment. Only verified release evidence permits Done. New scope/code changes invalidate affected approvals.

## 10 Stop recover or hand over

Use Ctrl-C for a graceful stop. The coordinator must stop its owned Claude process, checkpoint progress and show any pending remote reconciliation.

Expected implemented diagnostic commands:

```bash
delivery status --config ./delivery.local.toml
delivery inspect PILOT-123 --config ./delivery.local.toml
delivery recover PILOT-123 --config ./delivery.local.toml
delivery handover PILOT-123 --config ./delivery.local.toml
```

Recovery first explains what completed and what is uncertain. It must inspect remote artefacts before retrying publication. Do not delete a worktree/journal or restart a second worker to bypass a blocked recovery. A stale heartbeat does not authorise takeover.

To change assignee, stop/checkpoint the current worker, confirm a safe state and record handover, then reassign. The next developer reconstructs the task from Jira/Git; their machine need not possess the original conversation. Cancel only after stopping active work, and record the reason.

## Where history lives

| Location | Content |
|---|---|
| Jira | Brief, questions/answers, selected feedback, gates, ownership/status history and execution summaries |
| Application delivery branch | Immutable spec/plan versions, architecture proposals, review/verification/release artefacts and completed execution references |
| Application feature branch/PR | Implementation, tests and product docs shipped with the change |
| GitHub CI | Check results/logs for exact commits |
| Your local state directory | Crash-recovery journal, snapshots, process references and local logs |

The separate delivery branch prevents a review/release report from changing the implementation PR head after approval. Retain it for audit unless the team deliberately consolidates the documentation later.

## Troubleshooting

| Symptom | Check and action |
|---|---|
| Ticket not picked up | Inspect project/type/assignee/status, opt-in label, prerequisites and existing execution |
| It is in the right column | Inspect exact status; several statuses can share a column |
| Questions keep it paused | Check round token, question IDs, answer completeness and correct Submit answers action |
| Approval rejected | Check current token/version, authorised actor, corresponding transition and GitHub head/review |
| CI never completes | Required check may be missing, misnamed, pending or from the wrong producer; inspect real CI |
| Claude cannot run a tool | Inspect the tested permission profile; do not enable bypass permissions |
| Subscription usage/login error | Pause and resolve the local limit/login; no paid fallback |
| Second worker refused | Stop the original safely; do not remove a live lock |
| Branch divergence/publication uncertainty | Preserve records and use recovery; do not force-push or blindly repeat writes |
| Jira/GitHub offline | Keep local checkpoints; reconcile when connectivity returns |

The finished README must name the real support owner, log commands and supported versions. Include a clean-install verification record. Maintain this guide alongside code so new developers can complete setup without reconstructing decisions from chat history.
