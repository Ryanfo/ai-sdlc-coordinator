# Security and trust boundary

What the worker (Claude) can and cannot do, how that is enforced, what was verified, and what
remains a known limitation. "Prompts are not a sandbox": every control below is enforced by the
CLI, the operating system or the coordinator, not by instructions to the model.

## Actors

| Actor | Trusted for | Credentials it holds |
|---|---|---|
| Coordinator (`delivery` process) | Routing, validation, publication, approvals checking | Jira token (env), `gh` login, Git credentials |
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
- **Single Jira identity.** With the default profile, Jira cannot distinguish coordinator and
  human transitions. The coordinator never performs a human route and checks authors of both
  the decision comment and the transition, but strict separation needs a worker service
  account and Jira conditions (paid plans).
- **No distributed lock.** One supervisor per identity per machine is enforced with an OS lock.
  Two machines running the same identity can race; doctor warns when it sees another worker's
  active marker but cannot prevent a simultaneous start.
- **Overlap detection is advisory.** It compares published footprints only.
- **`claude auth status` can report a login whose OAuth session has expired.** Only the probe
  (or a real run) proves a working session; an expired session pauses tickets with an explicit
  "sign in" action and never switches billing.
