# delivery-platform

A local, Jira-driven AI delivery lifecycle. You run one **supervisor** on your laptop. It
watches a Jira project for tickets **assigned to you** that enter a *Ready for…* status, and
runs each one as its own isolated Claude Code session using **your Claude subscription**.
Many tickets can run at once; one slow or blocked ticket never holds up another.

Every stage ends at a human decision: specification, plan, code review, acceptance, release.
Humans approve, merge and release. The coordinator never approves, merges or deploys.

| Part | Where |
|---|---|
| Supervisor and CLI (`delivery`) | `src/delivery/` |
| Claude Code plugin (seven stage procedures, templates, contract) | `plugins/delivery/` |
| Your single config file | anywhere outside Git, e.g. `~/delivery.local.toml` |
| Design decisions and setup guides | `docs/` |

> **Status (1 Oct 2026):** implemented and tested against fakes and real Git; all seven
> procedures exercised with real Claude Code. Live Jira and GitHub pilot pending the project
> setup; see [docs/completion-report.md](docs/completion-report.md).

## Quick start

**Once per project** (Jira administrator or delivery lead): set up the Jira workflow from
[docs/jira-workflow-setup.md](docs/jira-workflow-setup.md), protect the application repository's
base branch, and make sure someone other than the developer can review PRs
([Before you start](#before-you-start)).

**Once per laptop**: sign in to the GitHub CLI (`gh auth login`), then run:

```bash
bash <(gh api repos/Ryanfo/ai-sdlc-coordinator/contents/install.sh -H "Accept: application/vnd.github.raw")
```

It installs what is missing, then asks a few questions and writes `~/delivery.local.toml`
([Quick install](#quick-install)). When your team keeps a shared project file, setup takes the
team's settings from it and asks only for yours ([Sharing the team's settings](#sharing-the-teams-settings)).
The first person on a new project then proves the Jira
workflow once; it walks two labelled test tickets through every status, which you delete
afterwards ([step 5](#5-map-the-workflow-and-run-preflight)):

```bash
delivery workflow verify --yes
```

**Every day**:

```bash
coordinator
```

It starts the coordinator in the background (if it is not already running) and shows it in
this terminal. Closing the window, or `Ctrl-b d`, leaves it running; `coordinator` shows it
again. Then create a ticket in Backlog, assign it to yourself and choose **Submit for
refinement** ([step 7](#7-submit-a-ticket)).

| Command | What it does |
|---|---|
| `coordinator` | Start it in the background if needed, and show it here |
| `coordinator status` | Is it running, and what every ticket is doing |
| `coordinator logs` | The coordinator's own log; `coordinator logs PILOT-7` is one ticket's Claude log |
| `coordinator open PILOT-7` | Open that ticket's latest Claude log (`--folder`: its run folder, `--jira`: the ticket) |
| `coordinator help PILOT-7` | Ask Claude what is wrong with that ticket and how to fix it |
| `coordinator stop` / `restart` | Stop it cleanly (sessions are saved and resume) / stop and start, to use new code |
| `coordinator clean` | Remove what finished runs left on disk |

One config file covers one Jira project and one application repository, and one coordinator
runs per Jira identity. To work on another project, run `delivery setup` again.

---

## Before you start

Confirm with your delivery lead:

- The Jira project and workflow are set up per [docs/jira-workflow-setup.md](docs/jira-workflow-setup.md)
  (Jira Software, Kanban; company-managed or team-managed).
- You can comment on and transition tickets, and tickets can be assigned to you.
- You can push branches and open PRs in the application repository; its base branch is protected.
- A **second person** can review your PRs on GitHub (you cannot approve your own).

Tools (macOS or Linux; Windows via WSL2):

| Tool | Version | Check |
|---|---|---|
| Python | 3.11 or newer | `python3 --version` |
| Git | 2.39+ | `git --version` |
| Claude Code | 2.1.x (2.1.278 tested) | `claude --version` |
| GitHub CLI | 2.x, signed in | `gh auth status` |
| uv (recommended) | 0.5+ | `uv --version` |

On Linux the Claude sandbox also needs `bubblewrap` and `socat`.

## Quick install

With the GitHub CLI signed in (`brew install gh`, then `gh auth login`), run this in a terminal:

```bash
bash <(gh api repos/Ryanfo/ai-sdlc-coordinator/contents/install.sh -H "Accept: application/vnd.github.raw")
```

It checks for Git, uv and Claude Code and offers to install any that are missing, puts the code
in `~/.delivery-platform` and the `delivery` and `coordinator` commands on your PATH, then runs
`delivery setup`. Setup asks a few questions and writes `~/delivery.local.toml` for you:

| It asks | It looks up for you |
|---|---|
| Your Jira site (or paste any link from it) and Atlassian email | Your Jira account ID, from the token |
| A Jira API token, once (stored in your Keychain, never in the file) | The issue types that carry the delivery workflow |
| The project key, and the approvers by name (or `anyone`) | Every workflow status ID and the resume stage field |
| The application's GitHub repository, and your clone of it (it offers to clone) | The base branch and the CI checks it requires |
| Who may approve PRs | The check commands, from the application's `package.json` |
| Claude model, session windows (and with them, opening the app in your browser when development finishes), and an optional Figma token | Whether you are signed in to GitHub and Claude (it offers to sign you in); the app's `dev` script for the preview |

With a team project file (`delivery setup --project <file>`, or one kept in this repository's
`projects/` folder, which setup offers), it asks only for your email and token, your clone,
session windows and an optional Figma token; everything else comes from the team's file.

Press Enter to accept each suggested answer. It finishes by running `delivery doctor` and listing
anything left to do; then start with `coordinator` (step 6). Run `delivery setup` again at any
time to change an answer: it keeps your other settings and comments, and saves the previous
file as `delivery.local.toml.bak`. Run the install command again to update.

Steps 1 to 5 below are the same thing done by hand.

## 1. Install

From your clone of the delivery-platform repository:

```bash
uv sync
```

```bash
uv run delivery --help
```

The rest of this guide writes plain `delivery …`. To run it like that from any folder, install
the commands once. `--editable` means they run this repository's code directly, so pulling
updates needs no reinstall:

```bash
uv tool install --editable .
```

This also installs `coordinator`: on its own it starts the supervisor in the background with
your config and shows it ([step 6](#6-start-the-supervisor)), and `coordinator <command>` is
`delivery <command>`, for example `coordinator attach PILOT-123`, `coordinator status` or
`coordinator --dry-run`.

Otherwise run every command from this folder with `uv run` in front, for example
`uv run delivery doctor`.

Without uv:

```bash
python3 -m venv .venv && . .venv/bin/activate && python -m pip install -e .
```

```bash
delivery --help
```

The application you deliver lives in **its own** repository. The supervisor never touches
your working checkout; it keeps its own clone and creates a separate worktree for every run.

## 2. Sign in to Claude Code with your subscription

Run this in a normal terminal (not inside another Claude session):

```bash
claude auth login
```

The supervisor uses only your subscription login. It refuses to run if an API key,
`ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper`, Bedrock/Vertex/Foundry setting or non-default
`ANTHROPIC_BASE_URL` would take over billing, and there is no paid API fallback. If your
subscription limit is reached, affected tickets pause with a clear message and resume
when you choose **Resume**.

## 3. Create your one config file

`delivery setup` asks for the values below and writes the file (see [Quick install](#quick-install)).
To write it by hand instead, start from the template:

```bash
delivery init --config ~/delivery.local.toml
```

Every other command reads `~/delivery.local.toml` unless you pass `--config <file>` or set
`DELIVERY_CONFIG`, so this guide leaves it out.

Edit it. Everything is commented; the important values are:

| Setting | Value |
|---|---|
| `identity.developer_jira_account_id` | Your Jira **account ID** (shown by `delivery credentials check` once Jira auth works) |
| `identity.worker_id` | A name for this laptop |
| `jira.base_url`, `jira.project_key`, `jira.email` | From your Jira administrator; your Atlassian email |
| `jira.supported_issue_types` | Only issue types that carry the delivery workflow (team-managed projects give each type its own) |
| `repository.url`, `base_branch`, `checkout_path` | The application repository |
| `repository.worktree_root`, `runtime.state_dir` | Local folders **outside** any Git checkout |
| `approvals.jira_account_ids`, `approvals.github_logins` | The humans who approve, and the independent GitHub reviewer |
| `checks.*` | The application's real gates and CI job names |
| `[figma]`, `[jira.attachments]` | Optional: Figma design snapshots and ticket attachment limits |
| `claude.model`, `[claude.models]` | Optional: the Claude model for every procedure, and per-procedure overrides (for example Opus for planning, implementation and review) |
| `[claude.interactive]`, `[preview]` | Optional: watch and type to Claude in tmux; run the app from each finished development session in your browser ([section 11](#watching-claude-work-and-typing-to-it)) |
| `[workflow.statuses]`, `jira.fields.resume_stage` | Generated by `delivery workflow inspect` (step 5) |

Secrets never go in this file. There is **no setting for how many tickets run at once**: the
supervisor runs every eligible ticket assigned to you. `claude.plugin_path` can be left out: it
defaults to `plugins/delivery` in this installation.

### Sharing the team's settings

Most of the config is the same for everyone on a project: the Jira site, project and workflow
status IDs, the repository, approvers, checks and models. Keep that in one **project file** and
each developer's own config shrinks to their identity, email and local folders.

The first developer writes it from their working config:

```bash
delivery project export projects/SDLC.toml
```

Keep it where the team can get it, for example committed in this repository under `projects/`
(it holds no secrets and nothing personal). Everyone else then runs `delivery setup` (it offers
the files in `projects/`) or `delivery setup --project <file>`. Their config names the file with
`project = "<path>"`; a value set in their own config wins over the project file, and the project
file may not contain `[identity]`, emails, local folders or other personal settings.

## 4. Jira and GitHub authentication

Jira (site API token profile). Create a token at
https://id.atlassian.com/manage-profile/security/api-tokens and put your Atlassian email in
the config (`jira.email`; it is not a secret). The token itself never goes in the config.

On macOS, store the token once in your login Keychain. The command asks for it (nothing is
shown), checks it with Jira first, and stores it only if Jira accepts it:

```bash
delivery credentials set
```

Paste once and press Enter; do not paste anything after it finishes. Every `delivery` command
then reads the token from the Keychain (`jira.token_keychain_service`, default
`delivery-jira`) and never prints it. `delivery credentials check` confirms Jira still accepts
it; after rotating a token, run `credentials set` again.

On Linux/WSL2 (or to override the Keychain), export the variables named by `jira.email_env`
and `jira.token_env` in the shell that runs the supervisor, without putting the token in your
shell history:

```bash
read -rs JIRA_API_TOKEN && export JIRA_API_TOKEN
```

GitHub: the supervisor uses your `gh` login to open PRs and read reviews and checks.

```bash
gh auth login
```

Figma (optional): to give Claude the designs linked in tickets, store a Figma token the
same way with `delivery credentials set figma` (see [Figma designs](#figma-designs)).

Claude worker sessions never receive the Jira token, the Figma token, the GitHub token, your
SSH agent or your Git credentials.

## 5. Map the workflow and run preflight

```bash
delivery workflow inspect
```

Paste the printed `[workflow.statuses]` and `[jira.fields]` block into your config. Once per
project (or after changing the workflow), prove every route with real tickets:

```bash
delivery workflow verify --yes
```

It creates two **unassigned** test tickets labelled `delivery-workflow-check` (no supervisor
picks them up), walks them through every status and reports missing or unexpected
transitions, the starting status, the resume field, issue properties and changelog
authorship. Delete the test tickets afterwards (`labels = delivery-workflow-check`). Then:

```bash
delivery doctor
```

Doctor is read-only. It checks your Jira identity and approvers, the status mapping and the
transitions actually offered, repository access and branch protection, that Git can push
(dry run), the Claude CLI version, flags and auth, the model each procedure will use, the
plugin, the Figma token if one is stored, and whether another supervisor is running. Fix
every `FAIL`. With `--claude-probe` it also proves each configured Claude model works on your
subscription.

Once, prove the Claude permission boundary on this machine (uses a little subscription usage):

```bash
delivery doctor --claude-probe
```

It verifies objectively that the plugin loads and that a worker session cannot read a
planted secret, push, write outside its directories, edit read-only checkouts, use `gh` or
reach the network.

Then a dry run (discovery only: no Claude, comments, pushes or transitions):

```bash
coordinator --dry-run
```

## 6. Start the supervisor

```bash
coordinator
```

`coordinator` on its own starts the supervisor (`delivery run`) in the background, in a tmux
session of its own, and shows it in this terminal. Closing the window, or `Ctrl-b d`, only
detaches: it keeps running, and `coordinator` (or `coordinator attach`) shows it again. Without
tmux (`brew install tmux`) it runs in this terminal instead; `coordinator --foreground` does
that on purpose.

Keep the laptop and network available. It polls every 60 seconds (with jitter),
so a status change is picked up within about a minute, including tickets that became ready
while you were offline. Run exactly **one** supervisor for your Jira identity: a second one
on this machine is refused; on another machine it is unsupported and doctor warns about it.

`coordinator stop` (or `Ctrl-C` while it is shown) stops every running session cleanly and
checkpoints it; they resume automatically next time you start it. Even closing a window it runs
in stops it cleanly. `coordinator restart` stops and starts it, which is how changes to the
coordinator's code are picked up: a running coordinator keeps the code it started with, and
says so (in its terminal and in `coordinator status`) when the code on disk is newer.

Everything it prints is also kept in its log, `coordinator logs` (`--follow` to watch;
`coordinator open` opens the file). If it ever stops unexpectedly, `coordinator status` says so
and the log has the error.

Each time a stage starts or finishes, the terminal prints a block between `=====` lines with
the action and time, the ticket key and name, the status change, the Claude procedure and
model, the outcome and your next step (when finished), and a link to the ticket in Jira:

```text
==============================================================================
 STARTED  Planning  PILOT-7                                           11:15:48
------------------------------------------------------------------------------
 Ticket    PILOT-7  Add a "HELLO WORLD" welcome block to the task list page
 Status    Ready for planning -> Planning
 Claude    plan-ticket on opus
 Run       PILOT-7-planning-20261002T091140Z-9973d9
 Jira      https://your-site.atlassian.net/browse/PILOT-7
==============================================================================
```

| Part | Meaning |
|---|---|
| `coordinator` | Start the supervisor in the background if needed, and show it here |
| `coordinator --foreground` (or `delivery run`) | Run it in this terminal until `Ctrl-C` |
| `--config <file>` | Use another config file (default `~/delivery.local.toml`, or `$DELIVERY_CONFIG`) |
| `--dry-run` | Discovery only: list what would start; no Claude, comments, pushes or transitions |
| `--once` | Start every eligible ticket, wait for those sessions to finish, then exit |
| `-v` | Detailed logging: `coordinator -v` (or `delivery -v run`) |

## 7. Submit a ticket

Create the ticket in **Backlog** with a real brief: problem, scope and exclusions, numbered
acceptance criteria (`AC1`…), constraints, links and any open questions. Assign it to
yourself and choose **Submit for refinement**. Someone else may move your ticket; it is still
yours while it is assigned to you.

What you will see in Jira:

1. **Refinement started** (with the run and session ID), then either **Questions** or a
   **Specification ready for review** comment linking the exact revision.
2. Plan review, then development: a PR on `feature/<KEY>`.
3. Independent review and verification reports, real check results, then **Code review**.
4. Release proposal, then your merge of the PR, then **Done**.

Every comment that waits for you starts with the status the ticket is ready to move into, for
example **Ready to move into Ready for planning once the specification is approved**, and each
action it offers says which status it moves the ticket into.

Specifications, plans, footprints, reviews and release documents are versioned on the
`delivery/<KEY>` branch of the application repository. The Jira comments link to exact
commits.

### Designs and attachments

Attach designs, screenshots or documents to the Jira ticket (or paste images into the
description). The coordinator downloads them with your Jira login and gives them to Claude as
read-only inputs for refinement, planning, development and verification; Claude's own
sessions still have no network access. Only images (PNG, JPEG, GIF, WebP), PDFs and plain
text files are handed over, each checked against its file type and size limits
(`[jira.attachments]`); anything else is listed as skipped with the reason. The specification
records which attachments it used (name and fingerprint). Adding or replacing an attachment
counts as a change to the brief.

Attachments are never committed to the repository, but the documents Claude writes describe
them, so do not attach confidential designs to tickets for a public repository.

### Figma designs

1. Once, create a Figma personal access token: avatar menu > **Settings > Security >
   Personal access tokens > Generate new token**. Choose an expiry and tick only **File
   content: Read-only** (`file_content:read`) and **Current user: Read** (`current_user:read`).
   Copy it (Figma shows it once) and store it in your Keychain. The command checks it with
   Figma before storing; repeat it when the token expires:

   ```bash
   delivery credentials set figma
   ```

2. In Figma, select the frame (one screen or state), right-click and choose **Copy link to
   selection**. Paste the link into the Jira ticket description, one per line with a short
   note, for example `Home - empty state: https://www.figma.com/design/...?node-id=12-345`.
   A link to a page includes that page's top-level frames; a link to a whole file is skipped
   because it does not say which design is meant. Prototype, branch and FigJam links work
   when they include a `node-id`. Put links in the description (the brief), not in comments.
   If your team uses the Figma for Jira app, its Designs panel is not read: paste the link
   into the description as well.

For each linked frame the coordinator gives Claude a PNG render, a summary (copy text in
reading order, typography, colours, auto-layout spacing, components used) and the condensed
layer data, all read-only. Claude itself never contacts Figma: the coordinator fetches only
the frames linked in the ticket (up to `[figma] max_frames`, default 10), and Claude's sessions
stay offline without the token. The specification's "Designs and attachments" section lists
each frame, link and version it was written from, and design details become acceptance
criteria.

Refinement pins the Figma file version it wrote the specification from. Planning, development
and verification use that same version, so what is built and checked is what was approved. If
someone changes a linked frame in Figma afterwards, the ticket gets a "Design changed in
Figma" comment and the stage keeps using the approved version; to adopt the new design,
choose Revise scope (or request specification changes).

## 8. Answer questions and request changes

Every decision comment uses a token the coordinator posts, so your intent is never guessed.

```text
ANSWERS PILOT-123-REFINE-R1
Q1: Title only.
```

then choose **Submit refinement answers**. A comment alone never restarts work.

```text
CHANGE SPEC PILOT-123-SPEC-v2
F1: Search must also match the description.
```

then choose **Request specification changes**. The next revision is written from the
current draft plus your numbered items. All templates are in
[docs/human-templates.md](docs/human-templates.md).

To tell Claude something (why the last run went wrong, an approach to take or avoid), add a
comment whose first line is `FOR CLAUDE`, or `FOR CLAUDE development` (any stage name) to aim
it at one stage, then choose the usual action. Every later session of that stage gets the note:

```text
FOR CLAUDE development
The e2e failure is the date picker's timezone; use the fixed clock in tests/clock.ts.
```

When the same correction keeps coming up across tickets, write it once as `FOR CLAUDE project`
on any ticket. The coordinator adds it to the project's guidance file
(`docs/delivery/guidance.md` on the `delivery/guidance` branch of the application repository),
confirms on the ticket with a link, and every Claude session on every ticket, on every
developer's machine, reads it from then on. Edit or remove entries on that branch;
`delivery guidance` shows the file and `delivery guidance add "<text>"` adds to it.

**Review comments on the pull request count.** When changes to a candidate are submitted
(Submit implementation changes), the PR's unresolved review conversations and written reviews
from since that candidate was published reach development as `G1`, `G2`… items next to your
`F` items, with the file and line. Resolve a conversation on GitHub to leave it out. With PR
comments, `CHANGE CODE <token>` needs no items of its own.

**What Claude sees from the ticket**: the description (the brief), its attachments and linked
Figma frames, the decision comments for the current step (`ANSWERS`, `CHANGE …`,
`SUBMIT CHANGES …`), the findings or change items it must address, `FOR CLAUDE` notes from
the assignee or an approver, and the project guidance. Tickets **linked** to this one in Jira
(for example the story a bug was found in) come too: their summary, status and description and,
when they went through delivery, their approved specification and plan, PR and released commit.
Other comments, including the coordinator's own, are never sent.

With [interactive sessions](#watching-claude-work-and-typing-to-it), the session that makes
the changes opens in a window and, once done, tells you how it addressed each item and asks
whether there is anything else. If not, close the window; anything else you type there is
picked up straight away and published as the next revision.

## 9. Approve, review, accept and release

- **Specification and plan**: `APPROVE SPEC <token>` / `APPROVE PLAN <token>`, then the approve
  action. An approval applies to that exact revision; a new revision supersedes it.
- **Code review**: an independent human approves the PR on GitHub at the current head with
  CI passing, then `APPROVE CODE <token>` and **Approve code**. Any new commit supersedes
  the approval.
- **Deviations from the specification**: when something changed during development (for
  example you asked for it in the open session), the review lists it as a deviation (`D1`…),
  says whether you asked for it, and asks whether it is acceptable. It never fails
  verification. If it is acceptable, comment `ACCEPT DEVIATIONS <code token>`
  and chooses **Submit follow-up changes**: Claude rewrites the specification to include it,
  publishes that as the approved revision (no new refinement or planning round) and the
  ticket comes back to Code review with the same tokens. If not, name it in `CHANGE CODE
  <token>` (`D1: keep to the specification`), choose **Request code changes** and then
  **Submit implementation changes**: a development session changes the code back and the new
  candidate is verified. Release preparation waits until every deviation is decided.
- **Acceptance** (product decision): when the ticket enters Acceptance review, the coordinator
  posts how to try the code-approved candidate and what to check: the **acceptance guide**
  verification wrote (how to check each acceptance criterion by hand, in plain language), and
  `delivery try <ticket>` for anyone with the delivery tools to run it on their own machine. With `[preview]`
  configured, your coordinator also runs that exact candidate and opens it in your browser
  until the ticket leaves Acceptance review ([Trying the change](#trying-the-change-in-your-browser)).
  Then `ACCEPT DELIVERY <token>` and **Accept delivery**, or `CHANGE ACCEPTANCE <token>` with
  numbered items and **Request acceptance changes**.
- **Release**: approve the proposal, then merge the PR on GitHub. That merge is the release:
  the coordinator reads the merge commit from GitHub, chooses **Record release** itself and
  verifies that the released commit contains exactly the approved candidate (merge, squash or
  rebase) before **Done**. There is no release to record by hand. (A `RECORD RELEASE <token>`
  comment with `commit:` and `environment:` is still accepted: if it is on the ticket when the
  release is recorded, its commit is verified instead of the merge commit.)

## 10. Several tickets at once, overlaps and integration

- All your eligible tickets run concurrently, each with its own worktree, branch, ports,
  temporary directory, logs and Claude session.
- Each plan publishes a **change footprint** (files, components, shared interfaces). The
  supervisor compares footprints of every in-flight ticket in the project, **including other
  developers'**, and posts one warning per overlap on both tickets. Overlaps never pause work.
  A shared interface, schema, migration or declared dependency ("is blocked by" link) is
  flagged as higher risk so you can agree which ticket merges first.
- Verification tests your candidate alone **and** merged with the latest base and other
  interacting candidates, so behavioural conflicts show up even when Git merges cleanly.
- A textual merge conflict (with the base or another ticket's candidate) is **flagged, never a
  failure**: the comments name the files and the integration checks run without the conflicting
  change. Resolve it in the PR when you merge; release verification accepts the approved
  candidate plus that merge and lists the files the resolution changed.
- **Or have Claude resolve it**: every development run on an existing branch first merges the
  latest base. When that conflicts, a short Claude session (`resolve-conflicts`) resolves the
  conflicts before the rest of the work, and the coordinator commits the merge. If it cannot,
  the merge is left out and the conflict stays flagged, as above. So any change request also
  brings the branch up to date. `[flow] resolve_conflicts = false` turns this off.
- **Out-of-date candidates**: while a candidate waits in Code review or Acceptance review, other
  work merges into the base. When that work changes the same files, or the candidate no longer
  merges cleanly, the coordinator says so once on the ticket, naming the tickets that merged,
  and how to verify again on the latest base (Submit follow-up changes). Nothing waits for it.

Overlap detection is advisory: it cannot see unpublished work on other laptops.

## 11. Day-to-day commands

### Reading what Claude did

Every Claude session writes its log as it runs. Read it from the command line:

```bash
delivery logs PILOT-123
```

```bash
delivery logs PILOT-123 --follow
```

| Option | What it shows |
|---|---|
| (none) | The latest run of the ticket: what Claude said, each tool it used (files read and edited, commands run), anything denied, and how the session ended |
| `--follow` / `-f` | The same, live, until the session finishes |
| `--list` | Every run of the ticket with its stage and state |
| `--stage development` / `--run <id>` | A particular stage's latest run, or one run |
| `--results` | Also the output of each tool call |
| `--raw` | The raw log file paths (stream JSON, for `jq`) |

The terminal's start and finish blocks show the same command, the run folder and a link to a
readable copy of the log (`claude-<procedure>.txt`). `coordinator open PILOT-123` opens that copy
(brought up to date first, even while the session works); `--folder` opens the run's folder and
`--jira` the ticket. When a stage ends Blocked, the finish block also prints the session's last
steps and what to do.

`coordinator logs` without a ticket shows the coordinator's own log: everything its terminal
showed, plus warnings and errors with their details (`--follow`, `-n 200` for more lines).

### Asking Claude what is wrong with a ticket

When a ticket is stuck, failed, or doing something you do not understand, ask Claude:

```bash
coordinator help PILOT-123
```

```bash
coordinator help PILOT-123 "I approved the plan an hour ago, why hasn't development started?"
```

The coordinator first gathers everything about the ticket into one briefing: what
`coordinator inspect` explains, the ticket's recent Jira comments and status history, what the
running coordinator is doing, this machine's runs (results, transcripts, check logs, journals)
and the coordinator log lines about it. Then it opens Claude in this terminal with that
briefing. Claude says what is happening, why (with the evidence), and what to do: the exact Jira
comment to paste and the action to choose, or the `coordinator` command to run. Ask it follow-up
questions; `/exit` or Ctrl-D ends it.

It is your own Claude session, not a sandboxed stage: it can read the run folders, the docs and
the coordinator's code, and runs read-only commands (`coordinator inspect`, `status`, `logs`,
`team`, `gh pr view`, `git log`) without asking. Anything that changes something, such as
`coordinator recover --resume`, it only proposes; Claude Code asks you before it runs. It never
posts to Jira or moves tickets, and never gets your Jira token. It uses Opus unless you set
`claude.help_model`. `--briefing-only` just writes the briefing and prints where it is. When a
stage ends Blocked or Failed, its finish block shows the command.

### Watching Claude work and typing to it

Turn on interactive sessions and each Claude session runs as a normal interactive `claude`
(the full terminal interface) inside tmux, instead of `claude -p`. A terminal window opens on
it as it starts, so you can watch every step and type to Claude at any point: your message is
picked up after its current step. Pressing `Esc` interrupts it, as usual. Closing the window
only detaches it; the session keeps running.

```bash
brew install tmux
```

Then add to your config:

```toml
[claude.interactive]
enabled = true
```

Claude Code asks whether to trust every new git worktree, and each run gets a fresh one. The
coordinator answers yes for its own worktrees under `worktree_root` and nowhere else. Print
mode never asks, and restricted mode ignores the repository's `.claude` settings either way.
Claude Code keeps a trust entry per worktree in `~/.claude.json`.

The restrictions are the same as print mode (restricted mode, the generated permission profile
and sandbox, the allowed tools, no credentials in the environment). The coordinator still only
accepts a schema-checked result: Claude writes it to a file, and a hook keeps Claude working
until the file is valid. `delivery doctor --claude-probe` runs its safety probe this way when
interactive sessions are on.

**Sessions stay open after the work.** Once Claude hands its result over, the coordinator
carries on (comments, pushes, moves the ticket) and the session stays open, so you can keep
asking Claude about what it did. In a development session you can also ask for changes: each
time Claude finishes a reply with the code changed, the coordinator pushes the change as the
next candidate (Claude never pushes), says so in Jira, and moves the ticket back to **Ready for
verification** so the new candidate is verified and reviewed again; earlier code and
acceptance approvals no longer count. This happens while the ticket is in Ready for
verification, Code review, Acceptance review or Changes requested and no run is working on it;
otherwise the change waits and the terminal says why.

**Verification waits until you end the development session.** While it is open, the ticket stays
in Ready for verification and no review or verification runs, so a series of small changes
costs one round of review and verification rather than one per change. Type `/exit` in the
session (or run `delivery close <ticket>`) when you have finished: a change Claude finished
making that had not been pushed yet is pushed first, then review and verification start on the
next poll, on that latest candidate. Closing the terminal window does not end the session (it
only detaches it), so it does not start verification. It needs three transitions in Jira,
named **Submit follow-up changes**, from Code review, Acceptance review and Changes requested to
Ready for verification (`delivery workflow verify --yes` checks them once they exist).

Specification, plan and release proposal sessions take changes the same way. Ask the session
that wrote the document: each time Claude finishes a reply with the document changed, the
coordinator publishes it as the next revision for review, with a new token in a new review
comment, and the revision that was under review no longer counts. The ticket stays where it
is, so no Jira transitions are needed. This happens while the ticket is in that review status
(Specification review, Plan review or Release review) with the session's revision under review.

**After acting on changes you asked for in Jira** (`CHANGE …`, `SUBMIT CHANGES …`, `REVISE
SCOPE …` or verification findings), Claude ends the session by listing each item and what it did
("The changes requested in Jira have been actioned: …") and asks whether you would like any
further changes. If not, close the window (that only detaches it; `delivery attach` reopens it),
or in a development session type `/exit`, which starts verification. Anything else you ask for
there is picked up straight away as above. Once the coordinator has
published the result, it brings that session up: a terminal window opens on it if none is
attached, with a note on the status line. Every development session ends by asking too. A change
request still needs both the comment and the Jira action; a comment alone never starts work.

A session closes when you type `/exit` (or `delivery close <ticket>`), after `idle_close_hours`
with nothing happening, when a new run of the same stage starts for the ticket, or when the
ticket is done or cancelled. A development session left open therefore holds verification for
up to `idle_close_hours` (default 12). Its conversation is kept with the run's logs
(`claude-<procedure>-after`), and changes that were never pushed are saved: as the unfinished
work the next development run continues from, or as a patch whose path the terminal prints.
Set `keep_open = false` to close sessions as soon as the result is handed over.

| Command | What it does |
|---|---|
| `delivery attach PILOT-123` | Open the ticket's session in this terminal (`Ctrl-b d` leaves it running) |
| `delivery sessions` | Every Claude session in tmux: working, or open for questions; and running apps |
| `delivery close PILOT-123` | End a session left open for questions (for development, verification then starts) |
| `delivery preview PILOT-123` | Open the app running from the ticket's development session, or start it again |

### Trying the change in your browser

The coordinator can run the app so people try a change before deciding on it. Add to your
config (or the team's project file, so everyone has it):

```toml
[preview]
command = ["npm", "run", "dev"]
```

| Setting | Meaning |
|---|---|
| `command` | The app's dev server, as an argument list. `{port}` is replaced by a port of its own, which is also exported as `PORT` |
| `setup` | Run first in the worktree (default: `checks.setup`, for example `npm ci`; `[]` for nothing) |
| `seed` | Run after setup and before the app, for example to load demo data (`["npm", "run", "seed"]`) |
| `url` | Where it answers (default `http://localhost:{port}/`); the browser opens once it does |
| `open_browser` | `false` to only print the address |
| `acceptance` | `false` to not run the candidate during Acceptance review |

It runs in three places:

- **While you develop.** Once a development session has handed over its candidate (and stays
  open), the app runs from that session's worktree. Ask for changes in the session: with a dev
  server that reloads, they show in the browser as Claude makes them, and each one is pushed as
  a new candidate; verification starts once you `/exit` the session, which also stops the app.
- **During Acceptance review.** When one of your tickets enters Acceptance review, your
  coordinator runs the exact candidate that was code-approved, in a worktree of its own, and
  opens your browser on it. It stops, and the worktree goes, once the ticket leaves Acceptance
  review. The Jira comment it posts says how to try it and what to check (see
  [section 9](#9-approve-review-accept-and-release)).
- **On anyone's machine.** `delivery try PILOT-123` runs the ticket's current candidate in this
  terminal (its output shows here) and opens the browser once it answers; Ctrl-C stops it and
  removes its worktree. It needs no coordinator running, only the delivery tools and the team's
  config, so the person accepting a ticket can try it themselves. `--ref <branch or commit>` runs
  something else.

`delivery preview PILOT-123` opens a running app again, or starts it again if it stopped, and
`delivery attach PILOT-123 --procedure preview` (or `acceptance`) shows its output. Like the
coordinator's checks, the app runs as you, outside Claude's sandbox, without your credentials
([security boundary](docs/security-boundary.md)).

### Other kinds of work

Not every ticket is a feature. The coordinator handles a few other kinds differently, chosen by
the ticket's issue type or a label (`[flow]` in the config):

| Kind | How it differs |
|---|---|
| **Bug** (`bug_types`, default `Bug`) | The specification records the steps to reproduce, actual and expected behaviour. Development writes a failing regression test first, then fixes it. Verification then runs the candidate's test files on the base branch *without* the fix and says in the code review comment whether they fail there (**Bug reproduced**), pass anyway (**not reproduced**: the test may not catch the bug) or are missing. Reported, never a failure. |
| **Spike** (`spike_types`, default `Spike`) | The specification is the question to answer. Instead of a plan, Claude investigates (it may run code) and writes **findings**: the answer, options compared, a recommendation, evidence. They are reviewed in Plan review (`APPROVE PLAN` accepts them); the coordinator then closes the ticket (Done), since there is nothing to build. Needs the **Complete spike** transition (see the Jira setup). |
| **Fast track** (label `fast_track_label`, default `fast-track`) | For small changes: refinement writes the plan with the specification, and one approval covers both. Planning then publishes that plan as approved and development starts straight away, with no plan review. Needs the **Use approved plan** transition; without it the plan goes to Plan review as usual. If Claude finds the change is not small, it writes no plan and planning runs as usual. |

**Proposed tickets.** When a brief is too big for one delivery, refinement still writes the
full specification and proposes slices of it (`S1`, `S2`…, each a short brief) in its review
comment. A spike's findings can propose follow-up work the same way. Nothing is created until
someone comments `CREATE TICKETS <token>` with the IDs: the coordinator creates them in Backlog,
unassigned, linked to the ticket that proposed them, and replies with their keys. The proposing
ticket carries on as it is (narrow it with a change request, or cancel it).

**Taking a release back out.** `delivery revert PILOT-123 --reason "<why>"` opens a pull request
that reverts the ticket's merged PR (as GitHub's Revert button does, for any merge method),
creates a Bug in Backlog linked to the ticket for the rework, and comments on the ticket with
both. People review and merge the revert as usual; the coordinator never merges.

### Long sessions, guardrails and resuming

Each session has generous turn and time limits (500 turns and 2 hours for implementation).
Two guardrails stop a session much earlier when it is clearly stuck: one that repeats the same
step with the same result (`loop_repeats`, default 6 in its last 30 steps) or one with no
activity for `stall_minutes` (default 15). Limits and guardrails are set under `[claude]`.

When a development session stops before finishing (limit, guardrail or timeout), its
unfinished changes are kept. After you choose **Resume development**, the next session starts
from those changes, sees where the last one stopped, and carries on rather than starting again.
If the specification or plan changed in between, it starts from the approved plan instead.

### When Claude cannot be used

If Claude's login has expired or the subscription's usage limit is reached, no ticket is
blocked for it. The run that hit it waits where it is (with its work so far), a comment on the
ticket says so, new tickets wait too, and the terminal shows a **WAITING FOR CLAUDE** block. Every
few minutes the coordinator sends Claude a one-word request; as soon as it works (after
`claude auth login`, or once the limit resets), the waiting runs carry on by themselves.
Nothing is needed in Jira.

### When the coordinator itself hits an error

A run that fails inside the coordinator stops without publishing anything else, the ticket gets
a comment naming the error, and `coordinator recover <KEY> --resume` continues it. The full
error is in `coordinator logs`.


```bash
delivery status
```

```bash
delivery inspect PILOT-123
```

```bash
delivery stop PILOT-123
```

```bash
delivery recover PILOT-123 --resume
```

```bash
delivery handover PILOT-123
```

```bash
delivery dispatch pause --reason "machine busy"
```

| Command | What it does |
|---|---|
| `coordinator` / `start` / `stop` / `restart` | Start in the background and show it / start only / stop cleanly / stop and start |
| `logs` | Readable Claude session log for a ticket; `--follow` to watch live. No ticket: the coordinator's own log |
| `open` | Open a ticket's latest Claude log (`--folder`, `--jira`), or with no ticket the coordinator's log |
| `attach` / `sessions` / `close` | Interactive sessions in tmux: open one, list them, end one left open. `attach` with no ticket shows the coordinator |
| `status` | Whether the coordinator runs (and whether its code changed since), waiting for Claude, then every session: ticket, stage, run/session ID, state, start time, next action |
| `team` | Every in-flight ticket in the project, any assignee, grouped by what it waits on (a decision, answers or a blocker, a merge, the coordinator) with the longest wait first; read-only |
| `try <KEY>` | Run the ticket's candidate on this machine and open it in your browser (Ctrl-C stops it); `--ref` runs another branch or commit |
| `preview <KEY>` | Open the app running for a ticket (its development session, or Acceptance review), or start it again |
| `guidance` / `guidance add "<text>"` | Show the project's guidance for Claude (`FOR CLAUDE project` notes), or add to it |
| `revert <KEY> --reason "<why>"` | Open a pull request that reverts a released ticket, and a linked Bug for the rework (asks first; `--yes` to skip) |
| `clean` | Remove worktrees and folders that finished runs left (unpushed changes are saved as a patch first); `--older-than DAYS` also removes old local logs |
| `help <KEY> ["question"]` | Ask Claude what is wrong with a ticket and how to fix it: gathers a briefing, then opens Claude in this terminal ([Asking Claude](#asking-claude-what-is-wrong-with-a-ticket)) |
| `inspect` | Why a ticket is (not) eligible, its gates and candidate, and the latest run explained: reasons, findings in full, failed check output, merge conflicts, log locations and the Jira actions available now; read-only |
| `stop <KEY>` | Stops one ticket's session and keeps its work; other sessions continue |
| `recover` | Reconciles one ticket against Jira/GitHub before any retry; `--resume` continues held work |
| `handover` | Stops and checkpoints one ticket and moves it to Blocked so it can be reassigned |
| `dispatch pause` / `resume` | Pauses new launches; running sessions continue |
| `credentials set` / `check` / `delete` `[jira\|figma]` | Store, test or remove the Jira (default) or Figma token in the macOS Keychain |
| `workflow verify --yes` | Walk labelled test tickets through every Jira status (after workflow changes) |
| `project export <file>` | Write your config's team settings as a shared project file |

### Seeing the whole team, and reminders

`delivery team` lists every in-flight ticket in the project, whoever it is assigned to, grouped
by what moves it on next: a decision (anyone who may approve), answers or a blocker, the merge,
or the coordinator and Claude. Within each group the longest wait comes first, so a review
nobody has picked up stands out.

Your coordinator also reminds people about your tickets that have waited long for a person.
After `[reminders] after_hours` (default 24) in a review, answers, blocked or merge status, it
comments on the ticket saying how long it has waited and what is needed next; Jira notifies the
ticket's watchers, and listed approvers are mentioned. It repeats every `repeat_hours` (24), at
most `max_reminders` (3) times per wait, sends nothing at weekends with `weekdays_only`, and
with `webhook_env` naming an environment variable that holds a Slack incoming-webhook URL, posts
the same reminder there. `after_hours = 0` turns reminders off.

## Where things live

| Location | Content |
|---|---|
| Jira | Brief, questions and answers, feedback, decisions, status history, run summaries |
| `delivery/<KEY>` branch | Spec, plan (or a spike's findings) and footprint revisions, ADRs, proposed tickets, reviews, verification and the acceptance guide, release documents, execution records |
| `delivery/guidance` branch | `docs/delivery/guidance.md`: the project's guidance every Claude session reads |
| `feature/<KEY>` branch and PR | Implementation and tests |
| GitHub checks | CI results for exact commits, plus integration provenance |
| `runtime.state_dir` | Your local recovery journal, logs, envelopes and locks (private; never in Git). Each run's `inputs/` holds what Claude was given, including attachment and Figma snapshots; `logs/` holds each Claude session transcript |
| `<state_dir>/supervisor/<id>/coordinator.log` | The coordinator's own log (`coordinator logs`), rotated at 5 MB |
| `repository.worktree_root` | One folder of worktrees per run, removed when it finishes; `coordinator clean` removes any left behind |
| macOS Keychain | The Jira token (`delivery-jira`) and Figma token (`delivery-figma`) |
| `~/.delivery-platform` | The coordinator's code, when installed with the quick install |

## Troubleshooting

| Symptom | What to do |
|---|---|
| Ticket not picked up | `delivery inspect <KEY>`: assignee, status, type, label, waiting input or an existing attempt |
| Verification failed or a stage blocked, next step unclear | `delivery inspect <KEY>`: every reason and finding in full, failed check output, Claude logs, and what each Jira action offered now does |
| Claude keeps getting something wrong | Add a comment starting `FOR CLAUDE` (or `FOR CLAUDE development`) with the guidance, then Resume or Submit as usual: the next session gets it as input |
| "It is in the Ready column" | Several statuses share a column; inspect the exact status |
| Stays paused after answering | Check the round token and Q-IDs, then use the Submit answers action |
| Approval rejected | Use the current token; if `[approvals] jira_account_ids` lists approvers, one of them must make both the comment and the transition (empty: anyone can); for code, an independent GitHub review on the current head with CI green when `require_independent_github_review` is on |
| Blocked: maximum turns or time | The session was making progress but hit its limit: raise `[claude.turn_limits]` or `[claude.timeout_minutes]` for that procedure, restart, then Resume (it continues from the kept changes) |
| Blocked: stopped by a guardrail | It repeated the same failing step or stalled. `delivery logs <KEY>` shows where; add guidance as a comment or adjust the plan, then Resume |
| "Waiting for Claude" | Login expired: run `claude auth login`. Usage limit: wait for the reset. Either way the waiting tickets carry on by themselves; no paid fallback |
| Blocked: permission or sandbox | Run `delivery doctor --claude-probe`; never use bypass permissions |
| Second supervisor refused | One already runs for your identity; `coordinator status` says where, `coordinator` shows it |
| A fix to the coordinator is not taking effect | It runs the code it started with: `coordinator restart` |
| "Stopped unexpectedly" or a "coordinator error" comment | `coordinator logs` has the error; `coordinator` starts it again, `coordinator recover <KEY> --resume` continues a stopped run |
| Disk filling up | `coordinator clean` (and `--older-than 30` for old logs) |
| Branch diverged or publication uncertain | Run `delivery recover <KEY>`; it queries Jira/GitHub before retrying and never force-pushes |
| Jira or GitHub offline | Nothing is mutated; it backs off and reconciles when back. If GitHub is unreachable when the supervisor starts, it logs "could not reach the application repository" and retries (15s, doubling to 10 minutes) without starting work; fix credentials if the error names them. Ctrl-C still stops it |
| A Figma link was not used | The run's envelope lists it under `designs_skipped` with the reason: a whole-file link (use Copy link to selection), no access, no token (`delivery credentials set figma`), or over the frame limit |
| "Design changed in Figma" comment | A linked frame changed after the spec was written; work continues on the approved version. Choose Revise scope to adopt the new design |
| Figma token rejected | It expired or lacks a scope; create a new one and run `delivery credentials set figma` |
| No app in Acceptance review | `[preview] command` is not set, tmux is missing (`brew install tmux`), or `acceptance = false`. The Jira comment still says how to try it; `delivery try <KEY>` runs it anywhere |
| PR review comments did not reach development | Only unresolved conversations with a comment since the candidate was published count; resolved ones are left out on purpose |
| A `FOR CLAUDE project` note was not added | Only notes from you or a listed approver count; the terminal says if adding failed (it retries on the next poll) |
| `CREATE TICKETS` did nothing | Use the token of the revision that proposed them (the specification's, or the spike findings' `PLAN` token) and IDs it proposed; the reply comment says what was created |
| A spike stays in Ready for development | Add the **Complete spike** transition (Ready for development to Done), or move it to Done by hand |
| Fast-track plans still go to Plan review | Add the **Use approved plan** transition (Planning to Ready for development) |

More detail: [operations](docs/operations.md), [security boundary](docs/security-boundary.md),
[pilot runbook](docs/pilot-runbook.md), [design decisions](docs/design/Implementation_Decisions.md).

## Development

```bash
uv run ruff check src tests && uv run mypy src && uv run pytest -q
```

Tests use a fake Jira that enforces the workflow, a fake GitHub, real Git repositories and a
scriptable fake `claude` executable. Live tests against real services are opt-in.
