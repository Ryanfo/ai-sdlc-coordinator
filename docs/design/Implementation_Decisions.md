# Implementation decision record

Status: M0 complete, 1 October 2026. Owner: Ryan.

Baseline documents, in precedence order:

1. `Parallel_Sessions_Amendment.md` (overrides the others where they conflict)
2. `Claude_Code_AI_SDLC_Build_Handoff.md`
3. `Jira_Workflow_Setup_Instructions.md`
4. `Developer_Onboarding_README.md` (operator interface blueprint)

## Decisions

| Area | Decision | Source |
|---|---|---|
| Repository | This directory is the `delivery-platform` repository: coordinator package plus `plugins/delivery`. No remote until requested. | User, M0 |
| Concurrency | One supervisor process per Jira site + developer identity per machine. It runs any number of concurrent isolated Claude sessions. There is no session-count setting, cap, semaphore or pool size. | Amendment §1–2 |
| Runtime | Python ≥3.11, asyncio supervisor, `uv` lockfile, pydantic v2 models, httpx for Jira, stdlib argparse CLI. | Handoff §3, §18 |
| Claude | Local Claude Code CLI with subscription auth only. `--bare` is rejected because it never reads OAuth. Workers run with `--restricted`, generated `--settings`, `--strict-mcp-config` and `--permission-mode dontAsk`. | Handoff §13, M0 probe |
| Jira | Jira Cloud, company-managed Kanban, workflow per setup doc (being created by the user). Plan tier unknown; doctor reports whether actor separation is enforceable. | User, M0 |
| Jira identity | Coordinator authenticates as the developer's own account (`site_api_token`). Jira cannot distinguish coordinator vs human actions by author. The coordinator excludes transitions it journaled itself from approval evidence and records this limitation in the setup profile. | User, M0 |
| GitHub | Coordinator uses `gh` (authenticated as the operator) for REST calls. The Claude child never inherits GitHub credentials. | Handoff §16 |
| Pilot app | New **public** repository with a **generic** sample app (task list, React/TS/Vite, Vitest/Testing Library, Playwright). It is not domain-specific; the plugin and templates are domain-neutral. Repo creation will be confirmed with the user first. | User, M0 |
| Pilot ticket | "Case-insensitive title search" with the open question "should search include the description?". | Handoff §21, adapted |
| Overlap detection | Plan stage publishes `plan/vNNN.footprint.json` on the delivery branch. The coordinator compares footprints of all active tickets in the project (not only its own) at five checkpoints. This is advisory, not a lock. | Amendment §4–5 |
| Integration evidence | Verification checks both the candidate head and an integration tree (latest base + interacting candidates), recording the head, base and tested tree. | Amendment §6 |
| Interactive sessions (optional) | `[claude.interactive]` runs each procedure as an interactive `claude` in the coordinator's own tmux server, so the developer can watch and type. `claude --bg`/`attach` was rejected: it refuses `--print`, ignores `--session-id`, and a session started while the background service runs takes the service's environment, not the coordinator's sanitised one. The result comes from a file checked by a coordinator-generated Stop hook; the folder-trust question is answered only for managed worktrees (trust of a parent folder does not cover git repositories). | User, 2 Oct 2026 |
| Flag, never block | Overlaps (including a shared interface, schema or migration, and a declared dependency) and merge conflicts (with the base or another ticket) are flagged in Jira and never pause or fail work. Conflicts are resolved when the PR is merged; release verification accepts the approved candidate plus merges of the base and lists the files the resolution changed. This replaces the Amendment §5 sequencing pause (`OVERLAP … PROCEED/WAIT/RESCOPE`): two tickets that conflicted with each other deadlocked in verification (SDLC-11/12). | User, 2–3 Oct 2026 |
| Follow-up changes | With `keep_open`, a development session stays open after hand-off. The coordinator (never Claude) pushes changes made there as the next candidate, supersedes code and later approvals and moves the ticket from Code review, Acceptance review or Changes requested back to Ready for verification (`Submit follow-up changes`). | User, 2 Oct 2026 |
| Follow-up revisions of documents | Specification, plan and release proposal sessions stay open too. When the document in the session's output directory changes, the coordinator publishes it through the stage's own publication as the next revision (new gate token, the one under review superseded, provenance `follow_up_of`), only while the ticket is in that review status with the session's revision under review. The ticket does not move, so no new Jira transitions are needed. A run that made changes asked for at a review gate, and every development run, ends by asking the developer whether there is anything else; the window can then be closed. Change requests still need the comment and the Jira action: a comment alone never starts work. | User, 3 Oct 2026 |
| Release record from GitHub | In the local pilot profile the human merge of the PR is the release. Once the ticket is in Ready for release, the supervisor reads the merge from GitHub and chooses **Record release** itself (a coordinator route alongside the human one); release verification takes the PR's merge commit and the configured environment as the release record. The handoff's human-typed `RECORD RELEASE` comment only repeated what the coordinator already reads from GitHub: the typed commit had to contain the PR's merge commit, the environment can only be the configured one and the PR is already recorded. The comment is still accepted and, if present, wins. The coordinator still never merges or deploys. | User, 3 Oct 2026 |
| App preview | `[preview]` runs the app (`preview.setup`, default `checks.setup`, then `preview.command`) from a finished development session's worktree, on its own port in its own tmux session, and opens the browser when it answers. It runs outside Claude's sandbox with the minimal child environment, like the checks, and stops when the session closes. The coordinator runs it, not Claude: Claude's sandbox cannot open a browser, and its own background processes end with its turn. | User, 3 Oct 2026 |
| Deviations from the specification | Review and verification report working behaviour that differs from the approved specification (usually asked for in the open development session) as deviations `D1`…, separately from defects, and a deviation never fails verification. The reviewer reads the candidate's commit messages, where session requests are recorded, to say whether the developer asked for each one. An approver accepts with `ACCEPT DEVIATIONS <code token>`: the next run on the ticket (Submit follow-up changes from Code review, or development/release preparation) has Claude rewrite the specification (`amend-spec`) and publishes it as an approved revision whose evidence is that comment plus the human move. The plan and the candidate's gates are not superseded, and a candidate that already passed is not reviewed again. A rejected deviation is a `D` item in the change request and becomes a development work item; one nobody names is never changed back. Release preparation waits until each deviation is decided. Before this, keeping such a change meant Revise scope, which re-ran refinement and planning and then blocked in development with no changes to make. | User, 3 Oct 2026 |
| Anyone can approve | `[approvals] jira_account_ids` is optional and empty by default: then anyone who can comment on and move the ticket may approve, accept, answer, resume and accept deviations, so no decision waits for one particular person. A decision still needs the exact token, a comment and a human transition (an automated transition never counts). Listing accounts restores the stricter rule. `FOR CLAUDE` notes stay limited to the developer, the assignee and listed approvers, because they go straight to Claude. | User, 3 Oct 2026 |

## Machine findings (M0 probe)

- Claude Code 2.1.278. Flags present: `--plugin-dir`, `--json-schema`, `--output-format`, `--permission-mode dontAsk`, `--tools`, `--settings`, `--setting-sources`, `--strict-mcp-config`, `--no-session-persistence`, `--restricted`. `--max-turns` is not listed in `--help` and is capability-tested at runtime.
- `ANTHROPIC_BASE_URL` was set in the operator shell (default host). Doctor accepts only the default host; worker environments drop all `ANTHROPIC_*` variables.
- The user-level Claude settings contain hooks and enabled plugins. Workers must not inherit them.
- `gh` 2.87.3 is authenticated as `Ryanfo`. The plan tier is not readable with current scopes, so a private repo may lack branch protection; the pilot repo will be public.

## Live values still required

Not design blockers, but needed before live testing:

- Jira site URL, project key, plan tier.
- Developer and approver account IDs.
- Status, transition and `Delivery resume stage` field IDs. `delivery workflow inspect` resolves these from names and prints a config snippet.
- A second human GitHub reviewer (code gate) and a second developer identity (cross-developer overlap evidence).

## Known limitations accepted by design

- No distributed claim. Two supervisors for the same identity on different machines can race. Doctor detects known conflicting worker markers but cannot prevent a simultaneous start.
- Overlap detection cannot see unpublished local changes on other machines.
- With a single Jira identity, Jira permissions cannot enforce worker/human separation.
