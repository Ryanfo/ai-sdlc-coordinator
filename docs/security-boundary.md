# Security and trust boundary

What the worker (Claude) can and cannot do, how that is enforced, what was verified, and what
remains a known limitation. "Prompts are not a sandbox": every control below is enforced by the
CLI, the operating system or the coordinator, not by instructions to the model.

## Actors

| Actor | Trusted for | Credentials it holds |
|---|---|---|
| Coordinator (`delivery` process) | Routing, validation, publication, approvals checking; fetching ticket attachments and linked Figma frames | Jira token and optional Figma token (macOS Keychain or env), `gh` login, Git credentials |
| Claude worker session | Proposing artefacts and code inside its run | Your Claude subscription session only |
| Ticket text, comments, application repo content | Requirements input only | none |
| Humans | Scope, approvals, merge, release | their own |

## Worker session profile

Every session is started by the coordinator as:

```text
claude -p "/delivery:<procedure> <inputs>/envelope-<procedure>.json"
  --restricted --plugin-dir <plugins/delivery> --settings <generated per run>
  --strict-mcp-config --permission-mode dontAsk --tools <role set>
  --output-format stream-json --verbose --json-schema <stage result schema>
  --no-session-persistence --session-id <uuid> --add-dir <output> --add-dir <inputs>
  --add-dir <plugin> [--max-turns N]
```

| Layer | Effect |
|---|---|
| `--restricted` | Ignores user, project and local settings files (no inherited hooks, plugins, MCP servers); file tools confined to the working directories; refuses bypassPermissions |
| `--tools` per role | Authoring and review roles get no shell. Implementer and verifier get Bash |
| Generated `permissions` with `dontAsk` | Only explicitly allowed edits succeed. Deny rules for `gh`, `git push/remote/config/credential`, `curl`, `wget`, `ssh`, `scp`, `rsync`, `security`, `npm publish/login/token`, nested `claude`, credential paths, WebFetch, WebSearch and all MCP tools |
| Edit scope | Output directory always; the feature worktree only for the implementer, minus `.github/`, `.claude/`, `CLAUDE.md`, `.mcp.json`, `docs/delivery/`. The verifier's file tools cannot edit its worktree (no allow rule) but its test commands may write build output there; tracked changes are rejected afterwards. Inputs and plugin directories are read-only |
| OS sandbox for Bash | `sandbox.enabled`, `failIfUnavailable`, no unsandboxed escape hatch; writes limited to the worktree, output and run temp; credential directories (`~/.ssh`, `~/.config/gh`, `~/.aws`, `~/.claude`, Keychains…) unreadable; network limited to `registry.npmjs.org` (plus binding a localhost port) for implementer/verifier, none otherwise |
| Environment | Only `PATH`, `HOME`, `USER`, locale, `TMPDIR` and run ports. No `ANTHROPIC_*`, `GH_*`, `GITHUB_*`, `JIRA_*`, `GIT_*` or `SSH_AUTH_SOCK` |
| Billing | Subscription only. Doctor refuses API keys, auth tokens, `apiKeyHelper`, third-party providers or gateways. No fallback code path exists |

The coordinator then validates the result itself: the contract ID (only present in the loaded
procedure), run/ticket/stage/input identity, artefact paths (no absolute paths, traversal or
symlinks; size limits), protected paths in the diff, tracked-file changes after review and
verification, and the plugin load reported by the CLI's own `system/init` event.

## Interactive sessions (`[claude.interactive] enabled`)

The session is the interactive CLI in the coordinator's private tmux server instead of `-p`:

```text
tmux -L delivery new-session -d -s <ticket>-<procedure> -c <worktree> --
  /usr/bin/env -i <PATH HOME USER locale TMPDIR ports TERM>
  claude "<the same prompt> + write the result to <output>/result.json"
  --restricted --plugin-dir <plugins/delivery> --settings <generated per run, plus hooks>
  --strict-mcp-config --permission-mode dontAsk --tools <role set> --session-id <uuid>
  --name "<ticket> <procedure>" --add-dir <output> --add-dir <inputs> --add-dir <plugin>
```

| Difference from print mode | Why it is safe |
|---|---|
| Result comes from `<output>/result.json`, not `--json-schema` | The same `validate_result` checks run twice: in the Stop hook (Claude is told to carry on until it passes, at most three times in a row) and again by the stage |
| Plugin proof is the skill's "Base directory for this skill" transcript line, not `system/init` | It must point into the configured plugin directory; Claude Code's own "Unknown command: /delivery:…" notice fails the run at once. The contract ID check is unchanged |
| Hooks (`python -m delivery.session_hook`) run outside the sandbox | They are written by the coordinator into its generated `--settings`; `--restricted` still ignores every other settings file, so the repository cannot add hooks. They only append to the run's session directory, which the session's own tools cannot write |
| The environment comes from `env -i` | Exactly the variables listed; nothing from the coordinator or the tmux server leaks in |
| Claude Code's folder-trust question is answered yes | Only for the coordinator's own worktrees under `worktree_root` (print mode skips the question). Trust lets a repository's `.claude` settings apply, which `--restricted` ignores anyway |
| A person can type to Claude, and use Claude Code's mode switch | They are the developer the coordinator works for. `--restricted` still refuses bypassPermissions, the sandbox still applies to every shell command, and the deny rules are unchanged |
| Claude Code saves the transcript under `~/.claude/projects` | It is mirrored into the run's logs as it is written, and that copy of the transcript is deleted when the session ends |
| Sessions can stay open after hand-off and their changes are pushed | The coordinator pushes, never Claude (no credentials, push denied). Only from the feature worktree, fast-forward only, never protected paths, only while the ticket is in a review status or Ready for verification with no run working on it. Each push is a new candidate that supersedes code, acceptance and release approvals and goes back through verification |
| Specification, plan and release proposal sessions stay open and their edits are published | The coordinator publishes, never Claude: the edited document is copied from the output directory (the only place the session can write) and published through the stage's own publication to the delivery branch, as a new revision whose gate supersedes the one under review. Only while the ticket is in that review status with the session's own revision under review |

On 2 Oct 2026 (Claude Code 2.1.287, macOS) `--claude-probe` ran the same probe through an
interactive session in tmux, with the folder-trust question answered by the coordinator: every
row of the table below passed, the plugin proof came from the transcript, the result from the
hook-checked file, and no transcript was left under `~/.claude/projects`.

**App preview (`[preview]`).** After a development run, the coordinator runs the configured
setup and app command in that session's worktree, which holds code Claude wrote. Like the
coordinator's checks (`npm ci`, tests), it runs **outside** Claude's sandbox, as you, with the
minimal child environment (`PATH`, `HOME`, locale, `PORT`; no tokens, SSH agent or Git
credentials), in its own tmux session, until the development session closes. Review what you
are about to run the same way you would before running checks on the branch.

## Verified on this machine (1 Oct 2026, Claude Code 2.1.278, macOS)

`delivery doctor --claude-probe` ran a real session with the verifier profile:

| Probe | Result | How it was verified |
|---|---|---|
| Plugin loaded and procedure executed | pass | CLI `system/init` plugins list; correct contract ID |
| Read a planted secret (Read tool and `cat`) | denied | nonce absent from all output |
| `git push` to a local remote | denied | remote ref absent |
| Write outside allowed directories | denied | file absent |
| Edit the read-only worktree | denied | file absent |
| `gh auth status` | denied | CLI permission denials |
| `curl https://example.com` | denied | CLI permission denials |
| `npm run` test script: cache under `node_modules`, bind and connect a local port | allowed | marker files written by the script |

A real `refine-ticket` session also confirmed the confinement the other way round: an envelope
placed outside the allowed directories could not be read, the worker reported blocked, and the
coordinator refused its output. Envelopes now live in the run's read-only inputs directory.

The probe also proves the positive case: `npm run` inside the verifier sandbox writes a tool
cache under `node_modules`, binds a local port and connects to it.

### Behaviours of Claude Code 2.1.278 found in real runs

| Observation | Consequence in the profile |
|---|---|
| Edit deny rules also become sandbox write bans for Bash | The verifier has **no** worktree deny: its file tools still cannot edit (no allow rule under `dontAsk`), tests can write `node_modules`/`dist`, and the coordinator rejects any tracked-file change afterwards |
| Binding a localhost port needs `sandbox.network.allowLocalBinding` | Enabled for implementer and verifier only; loopback needs no domain entry |
| `sandbox.network.allowMachLookup` makes every Bash call need approval under `dontAsk`, even with explicit allow rules | Left off. Consequently Chromium cannot launch inside the worker sandbox on macOS: browser e2e runs in the coordinator's checks (candidate and integration tree) and the logs are shared with the verifier |
| `AskUserQuestion` is denied under `dontAsk` even when an allow rule names it (Claude Code 2.1.289) | The resolver (`resolve-blocker`, which needs to ask the developer) runs in permission mode `default`: whatever its allow rules do not cover asks the developer instead of being denied. A resolution only starts when the developer asked for it and runs in a window on their machine. Deny rules, `--restricted`, the `--tools` set and the OS sandbox are the same as the implementer's |
| Commands prefixed with `VAR=value` need approval | `PORT`/`E2E_PORT` are exported into the worker environment; procedures say not to prefix |
| Inline interpreter code (`python3 -c …`) needs approval | Not needed by procedures; the probe uses an `npm run` script, matching real usage |
| A failed release verification run in a too-strict sandbox refused to claim success | Working as designed: the worker reported a blocker; the coordinator moved the ticket to Blocked (`release_verification`) |

## Known limitations (stated, not hidden)

- **Tests execute repository code.** The verifier and implementer run the application's own
  scripts inside the sandbox. This is a boundary for an honest-but-fallible agent on a trusted
  repository, not a complete defence against a malicious repository.
- **Coordinator-run checks are not sandboxed.** The configured check commands (`npm ci`, tests)
  run as you, with a minimal environment and no tokens, but with your filesystem permissions.
  Use the framework only on repositories you trust.
  The same holds for the repository's Git hooks when `repository.run_git_hooks` is on (off by
  default: the coordinator's commits skip hooks).
- **Single Jira identity.** With the default profile, Jira cannot distinguish coordinator and
  human transitions. The coordinator never performs a human route and checks the author of the
  transition that makes each decision, but strict separation needs a worker service
  account and Jira conditions (paid plans).
- **No distributed lock.** One supervisor per identity per machine is enforced with an OS lock.
  Two machines running the same identity can race; doctor warns when it sees another worker's
  active marker but cannot prevent a simultaneous start.
- **Overlap detection is advisory.** It compares published footprints only.
- **`claude auth status` can report a login whose OAuth session has expired.** Only the probe
  (or a real run) proves a working session; an expired session pauses tickets with an explicit
  "sign in" action and never switches billing.
- **The profile was proven on particular Claude Code versions.** Claude Code updates itself, so
  the supervisor re-runs the probe whenever `claude --version` changes and holds new sessions
  until it passes (`claude.probe_on_version_change`, on by default). Sessions already running
  when the binary changes are not stopped.

## Data that leaves the machine

Claude reads the ticket (brief, selected comments, attachments, Figma snapshots), linked tickets
**in this project and in `jira.linked_projects` only**, and the repository. Comments the
coordinator posts never include internal error text (it stays in the coordinator log), and with
`[notifications] operational = "operator"` notices about the developer's machine (Claude's
login, usage limit, internal errors) are not posted to tickets at all. Local run logs, including
Claude transcripts, are removed `runtime.retention_days` (30) after their run finished.
Coordinator commits are unsigned unless `repository.sign_commits` is on, in which case they use
the developer's own Git signing setup.

## Ticket attachments and Figma designs

Claude's sessions cannot fetch anything themselves. The coordinator downloads ticket
attachments (Jira login) and renders linked Figma frames (Figma token, `file_content:read`)
into the run's `inputs/` directory, which the session may read but not edit. Only images,
PDFs and plain text are handed over, each checked against its file signature and size limits;
SVG, HTML, archives and executables never are. File names cannot leave the inputs directory.
Jira's media redirect and Figma's pre-signed render URLs are fetched without our credentials.
Attachment and design content is untrusted ticket data, like the brief: it cannot change
tools, paths, checks or permissions. Figma snapshots are pinned to the file version the
specification was written from, so later stages build and verify against what was approved.
