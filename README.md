# delivery-platform

A local, Jira-driven AI delivery lifecycle. One **supervisor** runs on your laptop, watches a
Jira project for tickets assigned to you that enter a *Ready for…* status, and runs each as its
own isolated Claude Code session on **your Claude subscription**. Many tickets run at once.

Every stage ends at a human decision: specification, plan, code review, acceptance, release.
Humans approve, merge and release; the coordinator never does.

## Before you start

- **Jira**: a project with the delivery workflow ([setup guide](docs/jira-workflow-setup.md),
  done once by a Jira administrator), and permission to comment on and transition tickets.
- **GitHub**: you can push branches and open PRs in the application repository, its base branch
  is protected, and a second person can review your PRs.
- **Tools** (macOS or Linux; Windows via WSL2): Python 3.11+, Git 2.39+ and a Claude
  subscription. The installer offers to add Git, uv, Claude Code and the GitHub CLI if they are
  missing, and signs you in to GitHub.

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Ryanfo/ai-sdlc-coordinator/main/install.sh)
```

This installs the `coordinator` and `delivery` commands, then asks a few questions (Jira site,
a Jira API token, project key, application repository) and writes `~/delivery.local.toml`. It
looks up the rest itself and finishes by running `delivery doctor`, which lists anything left
to do. Press Enter to accept each suggested answer. Run the same command again to update, or
`delivery setup` to change an answer.

If your team shares a project file, setup takes the team's settings from it and asks only for
yours. The first person on a new project also proves the Jira workflow once:

```bash
delivery workflow verify --yes
```

## Use it

```bash
coordinator
```

This starts the coordinator in the background (if it is not already running) and shows it in
this terminal. Closing the window, or `Ctrl-b d`, leaves it running.

Then, in Jira: create a ticket in Backlog with a real brief (problem, scope, numbered
acceptance criteria), assign it to yourself and choose **Submit for refinement**. Claude
writes a specification and waits. From here every step is you moving the ticket:

1. Approve the **specification**, then the **plan**.
2. Claude builds it on a `feature/<KEY>` branch and opens a PR; it is independently reviewed
   and verified.
3. A second person approves the PR on GitHub, then you choose **Approve code**.
4. **Accept** the delivery, approve the release proposal and **merge the PR**. The coordinator
   sees the merge and moves the ticket to Done.

To ask for changes or answer questions, move the ticket to the matching action and say what you
want in a comment. Each comment on the ticket starts with the action to choose.

## Commands

| Command | What it does |
|---|---|
| `coordinator` | Start it if needed, and show it here |
| `coordinator status` | Is it running, and what every ticket is doing |
| `coordinator logs [KEY]` | The coordinator's log, or one ticket's Claude log |
| `coordinator help KEY` | Ask Claude what is wrong with a ticket and how to fix it |
| `coordinator stop` / `restart` | Stop cleanly (sessions are saved and resume) / stop and start |
| `delivery inspect KEY` | Why a ticket is or is not moving, and what to do next |
| `delivery doctor` | Check your setup |

The full list is in the [user guide](docs/user-guide.md#command-reference).

## More

- [User guide](docs/user-guide.md): configuration, designs and Figma, interactive sessions,
  previewing changes, bugs and spikes, overlaps, unattended running, troubleshooting
- [Operations](docs/operations.md), [security boundary](docs/security-boundary.md),
  [pilot runbook](docs/pilot-runbook.md), [design decisions](docs/design/Implementation_Decisions.md)

## Development

```bash
uv run ruff check src tests && uv run mypy src && uv run pytest -q
```
