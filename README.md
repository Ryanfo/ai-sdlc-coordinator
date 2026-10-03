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

---

## Before you start

Confirm with your delivery lead:

- The Jira project and workflow are set up per [docs/jira-workflow-setup.md](docs/jira-workflow-setup.md)
  (Jira Software, Kanban, company-managed).
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
| The project key, and the approvers by name | Every workflow status ID and the resume stage field |
| The application's GitHub repository, and your clone of it (it offers to clone) | The base branch and the CI checks it requires |
| Who may approve PRs | The check commands, from the application's `package.json` |
| Claude model, session windows, and an optional Figma token | Whether you are signed in to GitHub and Claude (it offers to sign you in) |

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

This also installs `coordinator`: on its own it starts the supervisor with your config
(`delivery run`), and `coordinator <command>` is `delivery <command>`, for example
`coordinator attach PILOT-123`, `coordinator status` or `coordinator --dry-run`.

Otherwise run every command from this folder with `uv run` in front, for example
`uv run delivery doctor --config ~/delivery.local.toml`.

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
| `[workflow.statuses]`, `jira.fields.resume_stage` | Generated by `delivery workflow inspect` (step 5) |

Secrets never go in this file. There is **no setting for how many tickets run at once**: the
supervisor runs every eligible ticket assigned to you.

## 4. Jira and GitHub authentication

Jira (site API token profile). Create a token at
https://id.atlassian.com/manage-profile/security/api-tokens and put your Atlassian email in
the config (`jira.email`; it is not a secret). The token itself never goes in the config.

On macOS, store the token once in your login Keychain. The command asks for it (nothing is
shown), checks it with Jira first, and stores it only if Jira accepts it:

```bash
delivery credentials set --config ~/delivery.local.toml
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
delivery workflow inspect --config ~/delivery.local.toml
```

Paste the printed `[workflow.statuses]` and `[jira.fields]` block into your config. Once per
project (or after changing the workflow), prove every route with real tickets:

```bash
delivery workflow verify --config ~/delivery.local.toml --yes
```

It creates two **unassigned** test tickets labelled `delivery-workflow-check` (no supervisor
picks them up), walks them through every status and reports missing or unexpected
transitions, the starting status, the resume field, issue properties and changelog
authorship. Delete the test tickets afterwards (`labels = delivery-workflow-check`). Then:

```bash
delivery doctor --config ~/delivery.local.toml
```

Doctor is read-only. It checks your Jira identity and approvers, the status mapping and the
transitions actually offered, repository access and branch protection, that Git can push
(dry run), the Claude CLI version, flags and auth, the model each procedure will use, the
plugin, the Figma token if one is stored, and whether another supervisor is running. Fix
every `FAIL`. With `--claude-probe` it also proves each configured Claude model works on your
subscription.

Once, prove the Claude permission boundary on this machine (uses a little subscription usage):

```bash
delivery doctor --config ~/delivery.local.toml --claude-probe
```

It verifies objectively that the plugin loads and that a worker session cannot read a
planted secret, push, write outside its directories, edit read-only checkouts, use `gh` or
reach the network.

Then a dry run (discovery only: no Claude, comments, pushes or transitions):

```bash
delivery run --dry-run --config ~/delivery.local.toml
```

## 6. Start the supervisor

```bash
delivery run --config ~/delivery.local.toml
```

Keep the terminal, laptop and network available. It polls every 60 seconds (with jitter),
so a status change is picked up within about a minute, including tickets that became ready
while you were offline. Run exactly **one** supervisor for your Jira identity: a second one
on this machine is refused; on another machine it is unsupported and doctor warns about it.

`Ctrl-C` stops every running session cleanly and checkpoints it; they resume automatically
next time you start the supervisor.

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
| `run` | Start the supervisor in the foreground until `Ctrl-C` |
| `--config <file>` | The one config file to use (required on every command) |
| `--dry-run` | Discovery only: list what would start; no Claude, comments, pushes or transitions |
| `--once` | Start every eligible ticket, wait for those sessions to finish, then exit |
| `-v` (before `run`) | Detailed logging: `delivery -v run --config …` |

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
4. Release proposal, then your merge and release record, then **Done**.

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
   delivery credentials set figma --config ~/delivery.local.toml
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

## 9. Approve, review, accept and release

- **Specification and plan**: `APPROVE SPEC <token>` / `APPROVE PLAN <token>`, then the approve
  action. An approval applies to that exact revision; a new revision supersedes it.
- **Code review**: an independent human approves the PR on GitHub at the current head with
  CI passing, then `APPROVE CODE <token>` and **Approve code**. Any new commit supersedes
  the approval.
- **Acceptance** (product decision): `ACCEPT DELIVERY <token>`, then **Accept delivery**.
- **Release**: approve the proposal; a human merges and releases; then comment
  `RECORD RELEASE <token>` with `commit:` and `environment:` and choose **Record release**.
  The coordinator verifies that the released commit contains exactly the approved candidate
  (merge, squash or rebase) before **Done**.

## 10. Several tickets at once, overlaps and integration

- All your eligible tickets run concurrently, each with its own worktree, branch, ports,
  temporary directory, logs and Claude session.
- Each plan publishes a **change footprint** (files, components, shared interfaces). The
  supervisor compares footprints of every in-flight ticket in the project, **including other
  developers'**, and posts one warning per overlap on both tickets. A shared interface,
  schema, migration or declared dependency pauses the later ticket until a human comments
  `OVERLAP <id> PROCEED`, `WAIT <KEY>` or `RESCOPE` and resumes it.
- Verification tests your candidate alone **and** merged with the latest base and other
  interacting candidates, so behavioural conflicts show up even when Git merges cleanly.

Overlap detection is advisory: it cannot see unpublished work on other laptops.

## 11. Day-to-day commands

`--config` defaults to `~/delivery.local.toml` (or `$DELIVERY_CONFIG`), so it can be left out
when your config lives there.

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

The terminal's start and finish blocks show the same command and a link to a readable copy of
the log (`claude-<procedure>.txt`). When a stage ends Blocked, the finish block also prints the
session's last steps and what to do.

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
otherwise the change waits and the terminal says why. It needs three transitions in Jira,
named **Submit follow-up changes**, from Code review, Acceptance review and Changes requested to
Ready for verification (`delivery workflow verify --yes` checks them once they exist).

A session closes when you type `/exit` (or `delivery close <ticket>`), after `idle_close_hours`
with nothing happening, when a new run of the same stage starts for the ticket, or when the
ticket is done or cancelled. Its conversation is kept with the run's logs
(`claude-<procedure>-after`), and changes that were never pushed are saved: as the unfinished
work the next development run continues from, or as a patch whose path the terminal prints.
Set `keep_open = false` to close sessions as soon as the result is handed over.

| Command | What it does |
|---|---|
| `delivery attach PILOT-123` | Open the ticket's session in this terminal (`Ctrl-b d` leaves it running) |
| `delivery sessions` | Every Claude session in tmux: working, or open for questions |
| `delivery close PILOT-123` | End a session left open for questions |

### Long sessions, guardrails and resuming

Each session has generous turn and time limits (500 turns and 2 hours for implementation).
Two guardrails stop a session much earlier when it is clearly stuck: one that repeats the same
step with the same result (`loop_repeats`, default 6 in its last 30 steps) or one with no
activity for `stall_minutes` (default 15). Limits and guardrails are set under `[claude]`.

When a development session stops before finishing (limit, guardrail, timeout or a usage
limit), its unfinished changes are kept. After you choose **Resume development**, the next
session starts from those changes, sees where the last one stopped, and carries on rather than
starting again. If the specification or plan changed in between, it starts from the approved
plan instead.


```bash
delivery status --config ~/delivery.local.toml
```

```bash
delivery inspect PILOT-123 --config ~/delivery.local.toml
```

```bash
delivery stop PILOT-123 --config ~/delivery.local.toml
```

```bash
delivery recover PILOT-123 --resume --config ~/delivery.local.toml
```

```bash
delivery handover PILOT-123 --config ~/delivery.local.toml
```

```bash
delivery dispatch pause --reason "machine busy" --config ~/delivery.local.toml
```

| Command | What it does |
|---|---|
| `logs` | Readable Claude session log for a ticket; `--follow` to watch live |
| `attach` / `sessions` / `close` | Interactive sessions in tmux: open one, list them, end one left open |
| `status` | Every session: ticket, stage, run/session ID, state, start time, next action |
| `inspect` | Why a ticket is (not) eligible, its gates, pause, candidate, overlaps, local runs; read-only |
| `stop` | Stops one ticket's session and keeps its work; other sessions continue |
| `recover` | Reconciles one ticket against Jira/GitHub before any retry; `--resume` continues held work |
| `handover` | Stops and checkpoints one ticket and moves it to Blocked so it can be reassigned |
| `dispatch pause` / `resume` | Pauses new launches; running sessions continue |
| `credentials set` / `check` / `delete` `[jira\|figma]` | Store, test or remove the Jira (default) or Figma token in the macOS Keychain |
| `workflow verify --yes` | Walk labelled test tickets through every Jira status (after workflow changes) |

## Where things live

| Location | Content |
|---|---|
| Jira | Brief, questions and answers, feedback, decisions, status history, run summaries |
| `delivery/<KEY>` branch | Spec, plan and footprint revisions, ADRs, reviews, verification, release documents, execution records |
| `feature/<KEY>` branch and PR | Implementation and tests |
| GitHub checks | CI results for exact commits, plus integration provenance |
| `runtime.state_dir` | Your local recovery journal, logs, envelopes and locks (private; never in Git). Each run's `inputs/` holds what Claude was given, including attachment and Figma snapshots; `logs/` holds each Claude session transcript |
| macOS Keychain | The Jira token (`delivery-jira`) and Figma token (`delivery-figma`) |
| `~/.delivery-platform` | The coordinator's code, when installed with the quick install |

## Troubleshooting

| Symptom | What to do |
|---|---|
| Ticket not picked up | `delivery inspect <KEY>`: assignee, status, type, label, waiting input or an existing attempt |
| "It is in the Ready column" | Several statuses share a column; inspect the exact status |
| Stays paused after answering | Check the round token and Q-IDs, then use the Submit answers action |
| Approval rejected | Use the current token; the approver must make both the comment and the transition; for code, an independent GitHub review on the current head with CI green |
| Blocked: maximum turns or time | The session was making progress but hit its limit: raise `[claude.turn_limits]` or `[claude.timeout_minutes]` for that procedure, restart, then Resume (it continues from the kept changes) |
| Blocked: stopped by a guardrail | It repeated the same failing step or stalled. `delivery logs <KEY>` shows where; add guidance as a comment or adjust the plan, then Resume |
| Blocked: usage limit or login | Run `claude auth login` or wait for the limit to reset, then Resume. No paid fallback |
| Blocked: permission or sandbox | Run `delivery doctor --claude-probe`; never use bypass permissions |
| Second supervisor refused | One already runs for your identity; use `delivery status` |
| Branch diverged or publication uncertain | Run `delivery recover <KEY>`; it queries Jira/GitHub before retrying and never force-pushes |
| Jira or GitHub offline | Nothing is mutated; it backs off and reconciles when back |
| A Figma link was not used | The run's envelope lists it under `designs_skipped` with the reason: a whole-file link (use Copy link to selection), no access, no token (`delivery credentials set figma`), or over the frame limit |
| "Design changed in Figma" comment | A linked frame changed after the spec was written; work continues on the approved version. Choose Revise scope to adopt the new design |
| Figma token rejected | It expired or lacks a scope; create a new one and run `delivery credentials set figma` |

More detail: [operations](docs/operations.md), [security boundary](docs/security-boundary.md),
[pilot runbook](docs/pilot-runbook.md), [design decisions](docs/design/Implementation_Decisions.md).

## Development

```bash
uv run ruff check src tests && uv run mypy src && uv run pytest -q
```

Tests use a fake Jira that enforces the workflow, a fake GitHub, real Git repositories and a
scriptable fake `claude` executable. Live tests against real services are opt-in.
