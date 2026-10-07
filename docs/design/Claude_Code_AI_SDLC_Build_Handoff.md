# Claude Code build handoff for the local Jira driven AI SDLC

Version 1.0 | 1 October 2026 | Owner Ryan | Status Ready for implementation discovery

> **Superseded in part (7 Oct 2026):** human decisions are now the Jira move alone, with no token
> comment; comments are plain feedback. See "Decisions are moves" in
> [Implementation_Decisions.md](Implementation_Decisions.md) and
> [docs/human-templates.md](../human-templates.md). The token-comment rules below are history.

## 1 Purpose and instructions to Claude Code

Build a reusable delivery framework that lets a developer use Jira to drive specification, planning, implementation, independent verification and human release gates through locally installed Claude Code. The desired result is a working coordinator and reusable delivery plugin that can connect to an existing Jira Cloud project and GitHub repository. Demonstrate it by taking a real test ticket through the lifecycle and implementing a small feature.

This is a build specification, not evidence of a working system. Earlier generated scaffolds are provisional drafts and must not override this document. Use this handoff as the agreed baseline. Read the companion AI SDLC overview, Jira Workflow Setup Instructions and Developer Onboarding README as well. The README is a proposed operator interface until you implement and verify its commands.

Start by reading this document, inspecting the destination workspace and reporting your understanding. Ask only questions whose answers change the implementation or are necessary to use the actual integrations. Offer the defaults below and group questions into one short clarification round. Continue work that does not depend on missing credentials or project details. Do not repeatedly ask for permission to make ordinary local implementation changes already requested here.

Implement and test the framework, rather than stopping at a directory scaffold, documentation or mocked demonstration. Keep the actual pilot feature separate from the framework. Never claim a live integration works because mocked tests pass. If live access is missing, finish the framework and provide precise remaining setup steps and verification commands.

Do not silently replace the design with a hosted orchestrator, commercial agent platform, API-billed Anthropic integration or central scheduler. Propose a change only when a concrete requirement cannot be met under the agreed design, and explain the trade-off.

## 2 Outcome and scope

The developer must be able to:

1. Install the coordinator and reusable plugin locally.
2. Edit one configuration file for their identity, Jira project, repository and local paths.
3. Verify integration access and workflow compatibility without changing remote resources.
4. Run the coordinator in a terminal.
5. Assign themselves a ticket and move it into Ready for refinement.
6. See the coordinator claim eligible work, start Claude, publish a draft specification and update Jira.
7. Answer clarification questions and request specification changes through Jira comments.
8. Approve specification and plan revisions, then obtain an implemented feature in a GitHub PR.
9. Receive independent review and real check evidence for the candidate commit.
10. Complete human code review and acceptance, review the release proposal, merge and record a local pilot release.
11. Receive release verification and reach Done.
12. Stop and restart the coordinator without losing task context or duplicating publication steps.

Include Jira read/write integration, Git/GitHub integration, local Claude subprocess execution, project configuration, workflow routing, reusable stage procedures, artefact publication, human decision capture, local recovery records, logs, operational commands and meaningful tests.

Exclude production deployment automation, production rollback automation, central multi-worker scheduling, guaranteed cross-machine locking, a custom web interface, automatic approval of agent output and the PA Media operational metadata agents themselves. This framework helps build applications, including that future workforce; it is not that application.

## 3 Agreed decisions and discovery questions

| Area | Baseline decision | Clarify only when needed |
|---|---|---|
| Coordinator | Deterministic Python 3.11+ application, packaged CLI | Existing framework repo and supported runtime constraints |
| Local platform | macOS first, Linux also supported; Windows via WSL for v1 | Native Windows is not assumed |
| Claude | Local authenticated Claude Code CLI using `-p` and the developer's subscription | Installed version, permitted tools and organisational restrictions |
| Jira | Jira Cloud; poll current APIs; map canonical stages to existing statuses | Site, project key, project type, current workflow and permissions |
| Workflow setup | Jira Software Kanban template, company-managed for a new project; inspect existing project first | Whether its workflow can support the agreed gates; no destructive workflow replacement |
| GitHub | Existing repository, named base branch, PR delivery | Repository URL, CI conventions, access and human reviewer |
| Ownership | Assignee account ID equals the configured developer identity | Jira auth identity versus a separate worker service identity |
| Concurrency | One active execution per local coordinator; one active coordinator per developer identity | Multiple machines for the same identity require a stronger claim mechanism |
| Persistence | Jira and Git for shared history; local filesystem journal for crash recovery | No SQLite requirement in v1 |
| Configuration | One human-edited local TOML file | A local template is provided; secrets are referenced, not embedded |
| Release | Human merge and human local pilot release; agent verifies recorded release | Existing release process can replace the local profile through configuration |
| Pilot standards | Synthetic TV catalogue conventions if repository is empty | Preserve a populated repository's stack and checks |

Actual setup values are not design blockers. They must be supplied before live integration testing. Identify immediately if the Jira workflow lacks required paths or the GitHub plan lacks required branch protection. Do not promise enforcement that the target system cannot provide.

## 4 Architecture and responsibility boundaries

Ship the coordinator and reusable delivery plugin together in a `delivery-platform` repository. Keep the application under development in its own repository. The plugin is a component, not a separately required deployed application.

| Component | Responsibilities | Must not decide or perform |
|---|---|---|
| Deterministic coordinator | Select work, validate inputs, route stages, invoke Claude, run gates, publish, reconcile, record | Invent requirements, grant human approval or silently change workflow |
| Delivery plugin | Procedures for each stage, input/output contract, review expectations | Independently transition Jira, push branches, merge or deploy |
| Application repository | Architecture, standards, test policy, product artefacts, implementation and tests | Store credentials or local machine execution logs |
| Jira | Brief, comments, questions, feedback, ownership, status history and decision records | Replace Git versioning of substantial technical artefacts |
| GitHub | Versioned artefacts, PR, CI evidence, protected merge gate | Treat agent review as a human PR approval |
| Human | Scope decisions, clarification, approvals, ownership handover, merge and release | Infer approval from a vague comment or board column |

The coordinator is ordinary software with explicit allowed transitions. Claude supplies structured proposals and artefacts. It never chooses a destination status by free-form reasoning.

## 5 Ticket eligibility and polling

Statuses are machine triggers. Board columns are a visual grouping and may contain several statuses. Do not trigger solely on a column title.

A ticket is eligible only if its configured project, supported issue type, current assignee and canonical ready status match, its required inputs and approvals are valid, and no known active execution exists. The assignee comparison uses the stable Jira account ID. The person moving the ticket does not determine which worker picks it up.

For example, an approver moving Ryan's ticket into Ready for planning does not make it eligible for the approver's coordinator. Ryan's coordinator processes it because Ryan remains the assignee.

For a dedicated pilot project, the project/status/assignee filters are enough. Support an optional opt-in label such as `agent-enabled` for a shared existing project. Document whether the selected profile requires it; never add an unmentioned hidden filter.

Default polling interval is 60 seconds with jitter, one active execution and deterministic ordering by readiness time then ticket key. Start by scanning both ready tickets and this worker's known active/paused runs. On startup, reconcile active runs before discovering new work.

Do not require an observed status-change event to start: if a ticket entered a ready status while the laptop was offline, the next poll can discover it. A comment by itself does not start a stage. A human submits the corresponding ready transition after commenting.

Use a JQL filter for discovery and directly refetch the issue before starting or publishing. Support pagination and eventual consistency. Eligibility checking is not a distributed lock.

## 6 Canonical Jira workflow

Map the following logical statuses to the actual target Jira status IDs. Labels here are the suggested project names. Do not hard-code site-specific numerical IDs.

| Canonical stage | Ready status | Active status | Success destination | Procedures |
|---|---|---|---|---|
| refinement | Ready for refinement | Refining | Specification review | refine-ticket |
| planning | Ready for planning | Planning | Plan review | plan-ticket |
| development | Ready for development | Developing | Ready for verification | implement-ticket |
| verification | Ready for verification | Verifying | Code review | review-ticket then verify-ticket in separate processes |
| release_preparation | Ready for release preparation | Preparing release | Release review | prepare-release |
| release_verification | Ready for release verification | Verifying release | Done | verify-release |

Additional statuses: Backlog, Acceptance review, Changes requested, Needs clarification, Blocked, Ready for release and Cancelled.

### Human routes

| From | Human action | To | Required input |
|---|---|---|---|
| Backlog | Submit for refinement | Ready for refinement | Brief, owner and initial criteria |
| Specification review | Approve specification | Ready for planning | Decision bound to the current specification revision |
| Specification review | Request specification changes | Ready for refinement | Feedback bound to the reviewed revision |
| Plan review | Approve plan | Ready for development | Decision bound to the current plan and approved specification |
| Plan review | Request plan changes | Ready for planning | Specific feedback |
| Code review | Approve code | Acceptance review | Current PR head, CI and independent human GitHub review |
| Code review | Request code changes | Changes requested | Feedback bound to the candidate |
| Acceptance review | Accept delivery | Ready for release preparation | Criteria satisfied and candidate SHA accepted |
| Acceptance review | Request acceptance changes | Changes requested | Behavioural feedback |
| Changes requested | Submit implementation changes | Ready for development | Selected feedback and unchanged approved scope |
| Changes requested | Revise scope | Ready for refinement | Revised scope; downstream approvals invalidated |
| Release review | Approve release | Ready for release | Approved release proposal and accepted candidate |
| Release review | Request release changes | Ready for release preparation | Release feedback |
| Ready for release | Record release | Ready for release verification | Human merge, exact release commit and environment |
| Needs clarification | Submit answers | Ready status of the originating stage | Matching round answered |
| Blocked | Resume stage | Ready status of the recorded stage | Resolved blocker and previous process stopped |
| Any unfinished stage | Cancel safely | Cancelled | Reason; stop running child before terminal reconciliation |

### Coordinator routes

- Eligible ready -> corresponding active, before starting Claude.
- Active -> success destination only after validated outputs, required gates and durable publication.
- Any automated active stage -> Needs clarification with round and resume stage recorded.
- Any automated active stage -> Blocked for an operational failure, missing prerequisite or unsafe continuation.
- Verifying -> Changes requested for substantive code/test defects, with findings and evidence.
- Failed release verification -> Blocked with `resume_stage=release_verification`; human recovery is required.

Do not automatically bounce implementation failures through unlimited edit loops. Allow a small bounded retry for transient infrastructure failure; substantive review findings are published for a human to submit as changes.

Clarification and Blocked must return to the recorded stage. Jira should restrict available transitions where its configured rules allow this. When it cannot, the coordinator must reject an inconsistent ready status and explain the correct action. A workflow diagram or transition description does not enforce a condition.

### Board grouping

| Column | Statuses |
|---|---|
| Backlog | Backlog |
| Ready | All ready statuses except Ready for release |
| Agent working | Refining, Planning, Developing, Verifying, Preparing release, Verifying release |
| Needs clarification | Needs clarification |
| Human review | Specification review, Plan review, Code review, Acceptance review, Release review, Changes requested |
| Blocked | Blocked |
| Ready for release | Ready for release |
| Done | Done, Cancelled |

If eight columns are too wide, Jira can group several of these visually without altering the status contract. Set the issue resolution correctly on Done/Cancelled; blocked/review statuses must not accidentally resolve an issue.

## 7 Required ticket information

Provide a ticket template with the following fields or description sections:

- Problem or outcome: who needs what and why.
- Initial scope and exclusions.
- Numbered acceptance criteria, such as AC1, AC2 and AC3.
- Known constraints and nonfunctional requirements.
- Links to relevant designs, examples, dependencies or existing behaviour.
- One assignee and the intended human approver or configured approver group.
- Existing assumptions or unanswered questions, if known.

Do not demand a fully designed implementation before refinement. Questions are an expected outcome. A blank summary alone is insufficient. Preserve the original brief and its revision when refinement elaborates it. Jira custom fields can map these sections, but v1 must also support ordinary description text plus comments.

## 8 Clarification and change request protocol

### Questions

1. The worker returns `needs_clarification`, with unique question IDs, rationale and the current draft path.
2. The coordinator publishes the draft and a Jira comment headed with a round token such as `PILOT-123-REFINE-R1`. Include numbered questions, the exact draft link, the human action and who must answer.
3. It records the origin stage, draft revision, question IDs, publication markers and round token; then moves to Needs clarification.
4. The human comments using the supplied answer template, quoting the round token and answering question IDs. Several comments may form one answer set.
5. The human clicks Submit answers for the recorded stage.
6. The coordinator reads and snapshots the selected comment IDs and full answer bodies, checks edit timestamps, and starts a new attempt against the same stage and draft lineage.
7. The worker updates the draft. Unanswered material questions create a new clarification round; otherwise the draft returns to review.

Do not use headless `AskUserQuestion` as the persistent human interface. Questions must reach Jira and survive the Claude process ending.

### Change requests

At a review gate, publish a readable template identifying the current artefact revision. The human comments with that token, the requested changes and numbered feedback items, then uses Request changes. Save the feedback comment IDs and selected version in the next input envelope. The agent revises the existing document, rather than starting from the original brief alone.

Example human feedback:

```text
CHANGE SPEC PILOT-123-SPEC-v2
F1: Search must include the synopsis as well as the title.
F2: Exclude external metadata APIs from this pilot.
```

Example clarification answer:

```text
ANSWERS PILOT-123-REFINE-R1
Q1: Use synthetic programmes only.
Q2: Match titles without case sensitivity.
```

Publish templates that humans can copy. Exact syntax is a v1 deterministic correlation mechanism, not a reason to ignore substantive answers: if the token is missing or ambiguous, explain how to resubmit and remain paused. Do not have an LLM decide that a vague comment means approval.

## 9 Human approval capture and version invalidation

At each gate, the coordinator creates a pending gate record containing a unique token, artefact revision/commit or candidate SHA, required decision and authorised approver set. It posts that token and exact links in Jira.

The human adds the supplied approval comment and performs the approval transition. The coordinator verifies both the author of the decision comment and the author of the corresponding status transition, using Jira history and configured identity/role checks. It records the matching evidence, time and version before starting downstream work. An automated transition must not count as human approval. If roles cannot be resolved, use explicit configured account IDs or block until the project integration supports them.

Example:

```text
APPROVE SPEC PILOT-123-SPEC-v2
```

The gate token resolves to the exact artefact; the human need not paste a long commit hash. Duplicate valid decisions are idempotent. Conflicting decisions, edited approval comments or superseded gates require human resolution. Store the approved comment body digest and update time.

For the code gate, verify a qualifying GitHub human review against the current PR head and required checks, in addition to the Jira action. Jira acceptance is a product decision, not a replacement for repository merge rules.

If the same Jira account is used by the coordinator and a human, Jira permission roles cannot distinguish these two uses. The coordinator can enforce its own prohibition on approving work, but strict actor separation requires separate service credentials and appropriate Jira conditions. Record this limitation explicitly in the selected setup profile.

Version changes have these consequences:

- Specification scope change invalidates dependent plan approval, implementation acceptance and release approval.
- Plan change invalidates dependent implementation readiness and acceptance as appropriate.
- Implementation branch change invalidates code/acceptance/release approvals tied to the prior candidate, even if a human considers it small.
- Evidence or release-document changes on the separate artefact branch do not silently change the approved code SHA, but approval of that document revision may need renewal.
- Base branch advancement does not itself alter a candidate; a merge/rebase or conflict resolution creates a new candidate that must be checked again.

Existing approvals remain visible as history but are marked superseded. Never silently carry them into a new scope or commit.

## 10 Artefact ownership and Git strategy

All architecture and substantial delivery artefacts belong in the application Git repository. Jira comments contain questions, answers, feedback, decisions and concise execution summaries, with links back to specific commits.

Use two retained branches per ticket in v1:

| Branch | Purpose |
|---|---|
| `delivery/PILOT-123` | Specification revisions, plans, questions snapshots, independent reports and release proposals/evidence |
| `feature/PILOT-123` | Implementation PR targeting the configured base branch; approved docs/ADRs needed with the product change can be copied or included deliberately |

This separation is a deliberate solution to an approval problem: writing a review or release report must not change the implementation PR head after a human approved it. Both branches are in the same application repository. Do not delete the delivery branch automatically. Offer a later human-reviewed documentation consolidation process, not an automatic merge of that branch.

Suggested paths on the delivery branch:

| Artefact | Path |
|---|---|
| Specification revision | `docs/delivery/PILOT-123/specification/v001.md` |
| Plan revision | `docs/delivery/PILOT-123/plan/v001.md` |
| Proposed architecture decision | `docs/delivery/PILOT-123/architecture/adr-001.md` |
| Independent review | `docs/delivery/PILOT-123/reviews/<run-id>/review.md` |
| Verification report | `docs/delivery/PILOT-123/reviews/<run-id>/verification.md` |
| Execution summary | `docs/delivery/PILOT-123/executions/<run-id>.json` |
| Release proposal | `docs/delivery/PILOT-123/releases/v001.md` |
| Release verification | `docs/delivery/PILOT-123/releases/<release-id>/verification.md` |

Include ticket key, revision, run ID, input references and relevant candidate SHA in each artefact. Revision files are append-only; corrections produce a new revision. URLs must use immutable commit references rather than moving branch URLs. An artefact's own Git commit is recorded externally after commit creation; do not create a self-referential commit hash inside its own content.

Architecture baseline, coding standards, testing policy and guardrails live on the application base branch under `docs/` with a short `CLAUDE.md` linking them. App-specific policy must not be hidden in the generic coordinator.

Use separate worktrees, not the developer's working checkout. Refuse dirty or mismatched managed worktrees. Only the coordinator commits/pushes. No force-push in v1; if a remote branch diverges or cannot fast-forward, block and provide instructions. Include run/operation markers in commit messages and PR descriptions for reconciliation.

Record the implementation SHA reviewed. Fresh verification runs on that SHA. Do not allow the reviewer to edit it. If the feature branch changes while reviewing, discard success for the old head and schedule a new verification only after an explicit valid ready transition or controlled retry within the same unapproved verification stage.

## 11 Execution records and local recovery storage

No central database is required under the constrained one-worker-per-identity model. Implement a small local filesystem journal plus Jira's shared records and Git artefacts.

The local journal is operational state, not another user-maintained configuration file. Use an application state directory outside Git, with restrictive file permissions. Keep append-only events, input snapshots, publication intentions/results, stderr/stdout logs and worktree references. Write snapshots through a temporary file and atomic replace; flush critical journal writes before remote mutations. Detect incomplete/corrupt records and block rather than inventing state.

Use one OS process lock per Jira-site/developer identity across configured projects on that machine. A project-specific lock would allow two projects to accidentally run two workers for one identity. PID alone is insufficient; use an OS-backed lock and confirm child-process ownership. Do not delete locks or worktrees merely because a heartbeat looks old.

Shared Jira properties hold a bounded current-run record and gate/clarification state, not raw transcripts. Preserve completed summaries in versioned execution artefacts and readable comments. Keep property JSON comfortably below the API size limit; use a conservative 24 KiB UTF-8 cap. A compact current record can reference immutable prior run artefacts.

Required execution fields:

| Group | Fields |
|---|---|
| Identity | schema version, ticket key, run ID, attempt, stage, developer account ID, worker ID |
| Input | brief digest, selected comment IDs/bodies/digests, source commit, spec and plan refs, policy/plugin digests, config digest with secrets excluded |
| State | discovered, starting, running, validating, publishing, awaiting_human, completed, interrupted, failed or blocked |
| Timing | created/start/update/end times in UTC, heartbeat, timeout |
| Outputs | artefact paths and published commits, candidate SHA, PR number/URL |
| Evidence | coordinator check results, CI links and source, independent report references |
| Human | gate token, pending/approved/rejected, actor, comment and transition references, approved revision |
| Pause | clarification round, question IDs, resume stage, blocker reason |
| Recovery | operation IDs, prepared request intent, observed remote result, local checkpoint, child metadata |

Jira properties do not provide the shared compare-and-swap claim this design would need for guaranteed multi-machine exclusion. Do not present a property write plus read-back as an atomic lock.

The supported constraint is one registered active machine per developer identity and no reassignment while running. Store worker identity and active-run markers so doctor can detect known conflicts and refuse them, but document the remaining race if two machines start simultaneously. A stronger deployment requires a genuine shared atomic claim service or a single worker for that identity. Do not add one secretly.

## 12 Failure recovery and publication reconciliation

Model execution and publication explicitly; there is no atomic transaction across local files, Jira and GitHub. Aim for reconcilable operations and stable markers, not a claim of universal exactly-once delivery.

For every external operation, journal its intent before sending and its confirmed result afterwards. Derive a stable operation ID from run ID, operation type and revision. Reconcile an uncertain operation by querying Jira/GitHub/Git before retrying it. Comment creation, PR creation and transitions must not receive blind write retries.

| Interruption | Recovery behaviour |
|---|---|
| Before Claude starts | Revalidate ownership/status/inputs; restart the same prepared attempt |
| Claude exits/crashes midway | Preserve worktree and logs; mark interrupted; fresh process continues from durable brief/artefacts/diff after user resume |
| Subscription limit or expired login | Stop/pause; explain local login or limit; never switch to a paid API |
| Claude completes but local validation fails | Publish no success transition; preserve diagnostic; return blocked or failed |
| Checks interrupted | Rerun required checks on the unchanged candidate; no need to redo implementation |
| Commit created but push uncertain | Query remote for exact commit/marker before retrying a push |
| PR/comment created but response lost | Find stable operation marker and attach the existing resource |
| Artefacts published but Jira transition uncertain | Refetch status/history; reconcile whether transition already occurred |
| Assignee or source input changes during work | Stop child, preserve result as stale; no automatic success publication |
| Remote branch diverges | Block; no force-push or silent overwrite |
| Human cancels during execution | Terminate child process group; suppress publication; reconcile cancellation |
| New worker receives a stopped ticket | Reconstruct shared inputs/artefacts; local logs may be unavailable; do not assume session continuity |

Read-only API retries may use bounded exponential backoff, jitter and Retry-After. Authentication and permission errors should surface promptly. A network failure before discovering work leaves the worker idle/reconnecting rather than mutating ticket state. A network failure during an active task cannot justify claiming success.

On SIGINT/SIGTERM, terminate the owned child process group, wait a bounded period, persist an interrupted checkpoint and exit cleanly. On restart, verify whether any former child is still alive before taking new work. If Jira is unreachable, preserve the pending reconciliation locally and show it in CLI status.

Session resume is optional optimisation, never the correctness basis. A fresh process must be able to continue using durable inputs, approved artefacts, current diff and journal checkpoints. A stale heartbeat does not authorise takeover. Human reassignment requires stopping the first worker and confirming a safe handover state.

## 13 Claude runner and permission model

Use the installed CLI, not the Anthropic API or an SDK that selects API billing. Feature-detect actual CLI flags and pin/test a supported version range. Run a small authenticated smoke test before live work.

Use subprocess argument arrays, explicit working directory, a sanitised child environment and timeouts. Never interpolate Jira text into a shell command. Include the ticket data in an input file/envelope with clear instruction boundaries.

Load the external plugin explicitly with `--plugin-dir`. Use the selected namespaced procedure in `-p` if supported by the installed CLI, and verify that it actually ran. Current official documentation supports user-invoked skills in print mode; older versions may differ. Do not rely on successful process exit alone to prove plugin loading.

Illustrative argument shape, not a substitute for capability testing:

```text
claude --plugin-dir <absolute-plugin-path>
       -p <stage-prompt-with-input-envelope-reference>
       --output-format json
       --json-schema <validated-stage-schema>
       --permission-mode dontAsk
       --max-turns <configured-limit>
```

Add only supported permission and tool rules required by the stage. For current versions, disable unattended permission prompts where appropriate; otherwise use deny-on-prompt behaviour. Permission denial is a blocked outcome, not a reason to enable bypass permissions. Do not use `--dangerously-skip-permissions`.

Inspect active authentication and effective configuration. Reject `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper`, API/Console profiles or Bedrock/Vertex/Foundry/gateway configuration that contradicts the agreed subscription profile. Do not log credentials. In print mode an API key can override the subscription, so simply being logged in is insufficient. Do not use bare mode if it removes the subscription login or required configuration. If organisational policy requires a different auth profile, stop and clarify rather than silently changing billing.

The Claude child must not inherit Jira/GitHub write tokens. However, stripping environment variables alone does not isolate a developer account's credential files or native keychain. Build a supported runtime profile with a restricted tool set and filesystem/network controls; document its trust boundaries. Test attempts to access credential files, `gh`, push/merge/deploy commands and protected policy files. If the local setup cannot enforce the intended boundary, doctor must report a non-ready profile rather than calling prompts a sandbox.

Read-only review permits reading code and producing its report through structured output. Executable verification runs necessary test/build commands in an isolated disposable worktree, which may generate caches/output but must not alter tracked implementation. Check tracked diffs after the run. Tests themselves execute code, so review mode is not a complete security boundary for an untrusted repository.

Discover effective hooks, MCP servers, project settings and plugins. Required project instructions may load, but unrelated write-capable integrations must not be quietly inherited. Report denied tools and plugin errors; support a tested explicit settings profile generated from the one local configuration without hand-editing additional policy files.

Keep tool access, allowed writable paths and stage execution rules in trusted coordinator/plugin policy. The app worker cannot weaken them through a Jira comment, app `CLAUDE.md` or generated code.

## 14 Input and output contracts

Use versioned typed models with JSON Schema generation and coordinator-side validation. The input envelope includes run/ticket/stage identity, exact source refs, policy refs, approved artefact refs, selected feedback/answers, allowed paths, configured checks and expected output contract. Do not let worker-supplied paths or checks override trusted policy.

An example result contract:

```json
{
  "schema_version": 1,
  "run_id": "<provided-run-id>",
  "ticket_key": "PILOT-123",
  "stage": "refinement",
  "input_revision": "<provided-digest>",
  "outcome": "completed",
  "summary": "Draft specification prepared for human review",
  "artifacts": [{"path": "docs/delivery/PILOT-123/specification/v001.md", "kind": "specification"}],
  "questions": [],
  "findings": [],
  "evidence": [{"criterion_id": "AC1", "description": "Defined in specification", "path": "docs/delivery/PILOT-123/specification/v001.md"}],
  "worker_checks": []
}
```

Allowed outcomes: completed, needs_clarification, failed, blocked. Stage-specific required fields apply. A completed outcome is a proposal, not an approved or verified result. Worker check claims are informational; coordinator and CI evidence are authoritative for automated gates.

Validate IDs/digest, allowed stage/outcome, required fields, unique question IDs, path containment, file existence, file sizes and output limits. Reject absolute or traversing paths, symlink escape, missing artefacts, malformed JSON, excessive output and mismatched input identity. Parse the documented Claude response envelope and its structured output; do not assume stdout is directly the business result. Capture stderr separately and redact secrets.

Monitor assignee, status and scoped input digest during work and refetch before side effects. Do not use Jira's generic updated timestamp as the sole revision check: the coordinator's own comments change it. Digest only material input fields and selected comment contents; track human scope amendments and gate events separately from execution noise.

## 15 Delivery plugin and product expectations

Ship a plugin manifest plus seven procedures, a common stage contract and reusable artefact templates. Keep each procedure focused and test it with actual headless invocation before integration.

| Procedure | Inputs | Required output | Completion meaning |
|---|---|---|---|
| refine-ticket | Brief, app standards, selected answers/feedback | Versioned specification or numbered questions | Ready for specification review |
| plan-ticket | Approved spec, source code and selected plan feedback | Plan with file/interface changes and criterion-to-test mapping | Ready for plan review |
| implement-ticket | Approved spec/plan and isolated code worktree | Code/tests and evidence mapping | Ready for independent verification |
| review-ticket | Fresh brief/spec/plan, frozen candidate and standards | Findings and criterion-by-criterion report | Independent proposal, not human approval |
| verify-ticket | Frozen candidate, review report and required commands | Observed checks and acceptance report | Coordinator decides pass/fail |
| prepare-release | Accepted candidate and configured release profile | Versioned release proposal, notes, smoke/rollback instructions | Ready for release review |
| verify-release | Human-recorded release commit/environment and approved proposal | Observed smoke evidence | Coordinator may mark Done |

Fresh review and verification are separate CLI processes with no author session reuse. They receive the original brief and approved artefacts, candidate diff and public evidence; they do not receive private author reasoning. Verification can read review findings. Independent models can still share errors, so executable and human gates remain.

Create generic templates for specification, plan, architecture decision, review, verification and release. Specifications include problem, scope/exclusions, numbered criteria, nonfunctional needs, constraints, dependencies, assumptions/questions and revision history. Plans include affected files, interfaces, implementation steps, test mapping, risks and rollback implications.

For a greenfield dummy repository, propose a client-only synthetic TV catalogue using React, TypeScript and Vite; semantic accessible UI; fixture programme ID/title/synopsis/genre/year/duration; pure search/filter logic; favourites via a storage adapter; no external metadata or production credentials. Standards: strict types, meaningful small functions, error handling, stable IDs, no test weakening or blanket lint exclusions. Tests: Vitest/Testing Library and Playwright, including empty results, case handling, combined filters, favourites persistence, corrupt storage and keyboard use.

Suggested five gates for that profile: lint, typecheck, unit, build and e2e. Commit actual lockfile, tool configs and real scripts during foundation work. A CI template with missing scripts is not a working check. For an existing application, discover and adopt its real architecture and check conventions rather than overwriting them with the dummy profile.

## 16 Jira and GitHub integration details

### Jira

Implement current Cloud REST adapters for identity, project/issue metadata, enhanced JQL search, issue reads, paginated comments, changelog, properties, available transitions, transitions and comments. Support Atlassian Document Format in descriptions/comments with round-trip-safe formatting. Use current endpoints, not retired legacy search assumptions. Recheck official docs during implementation.

Resolve and validate configured status IDs and action names/IDs. Query transitions actually available to the authenticated actor. Never guess numerical transition IDs or map solely by duplicated display names. Refuse ambiguous mapping. Parse human transition history and decisions since the current gate token, not all historical comments indiscriminately.

Normal runtime credentials need issue browse/read, comment, edit property and transition access as appropriate. Workflow administration is a separate setup activity, not a runtime permission requirement. Offer read-only `workflow inspect` and a proposed canonical mapping. An optional workflow-create payload/command must be explicitly invoked, validated and scoped; it must not replace or attach an existing scheme without a separate human setup decision. Jira's Free plan limits role and permission-scheme configuration; doctor must distinguish a functional process pilot from a profile with enforced actor separation.

Where Jira must hide incorrect clarification/blocker resume actions, use a configured single-select Delivery resume stage field with the six canonical stage values and transition field conditions. Map its actual field ID under `[jira.fields]` and set it before entering a paused status. Keep the shared execution record authoritative, and detect any disagreement with the display/routing field. This field is separate from the native Jira issue property; native workflow rules must not be assumed to read arbitrary properties. If this setup cannot be configured, report coordinator validation as the fallback and do not claim strict UI enforcement.

Support the agreed site's auth method, including a distinction between ordinary site API tokens and scoped gateway tokens if applicable. Keep auth secrets outside configuration. Do not assume one base URL/auth combination works for all Atlassian credentials.

If exact approval/clarification conditions cannot be enforced with the target Jira rules, document the manual process and coordinator rejection, and test forbidden transitions. Do not imply a route skeleton guarantees human users have only one valid forward button.

### Git and GitHub

Implement branch/worktree operations and a GitHub adapter for repository identity, PR find/create/update, reviews, check runs, commit statuses and merge/release observations. Using `gh` for the coordinator is acceptable if installed and capability-tested; direct REST is also acceptable. Keep a fake adapter boundary for tests.

Check both check runs and commit statuses when required by the repository. Require configured check names from the expected producer, exact target SHA, successful permitted conclusions and no pending/missing checks. Treat skipped/neutral as failure unless a specific check policy deliberately allows them; do not treat an absent check as passed. Preserve URLs as evidence.

Protect the configured base branch: PR required, at least one independent human review, required CI, stale approval dismissal or an equivalent current-head review rule, no worker bypass, no force pushes and no deletion. Verify protections and plan availability; do not change them during a ticket run. The PR author cannot approve their own PR. A one-person test cannot truthfully demonstrate that independent human gate; obtain a second human collaborator for the full pilot.

A human code approval in Jira must match valid GitHub review evidence and the current PR head. Product acceptance is recorded separately. The coordinator never calls merge or deployment APIs. If a human merges a changed/unapproved candidate or bypasses gates, flag the discrepancy and block completion.

Release verification must understand merge strategy. Record PR approved head, merged commit, actual released commit and environment. Squash/rebase merges may produce a different SHA. Use GitHub's merge metadata and required post-merge checks to establish provenance; do not require naive SHA equality or assume every base-branch commit is the approved release. Reject unrelated or unverified content. Define any additional integration testing when the merge tree differs from the reviewed candidate.

## 17 Single local configuration and operator commands

Supply `delivery.example.toml` and a schema-backed loader. The developer copies it once to a local location and edits the values. Generated caches, runtime settings and recovery data are not extra user configuration. No passwords, tokens or subscription credentials are embedded or printed.

Illustrative config shape; Claude should complete and validate the schema:

```toml
config_version = 1

[identity]
developer_jira_account_id = "YOUR_ACCOUNT_ID"
worker_id = "ryan-laptop"

[jira]
base_url = "https://YOUR-SITE.atlassian.net"
project_key = "PILOT"
supported_issue_types = ["Story", "Task", "Bug"]
required_label = "" # Set agent-enabled for opt-in use in a shared project
auth_profile = "site_api_token"
email_env = "JIRA_EMAIL"
token_env = "JIRA_API_TOKEN"

[jira.fields]
resume_stage = "ACTUAL_CUSTOM_FIELD_ID"

[repository]
url = "https://github.com/YOUR-ORG/YOUR-REPO.git"
base_branch = "main"
checkout_path = "/absolute/path/to/app"
worktree_root = "/absolute/path/to/delivery-worktrees"
github_auth_profile = "gh"

[runtime]
state_dir = "/absolute/path/to/local-state"
poll_seconds = 60
max_parallel_runs = 1
timeout_seconds = 1800

[claude]
executable = "claude"
plugin_path = "/absolute/path/to/delivery-platform/plugins/delivery"
auth_profile = "subscription"
max_turns = 40
allow_paid_api_fallback = false
permission_profile = "local_pilot"

[approvals]
jira_account_ids = ["APPROVER_ACCOUNT_ID"]
github_logins = ["HUMAN_REVIEWER"]
require_independent_github_review = true

[release]
profile = "local_pilot"
environment = "local-pilot"
human_merge_required = true
human_deployment_required = true

[checks.commands]
lint = ["npm", "run", "lint"]
typecheck = ["npm", "run", "typecheck"]
unit = ["npm", "run", "test:unit"]
build = ["npm", "run", "build"]
e2e = ["npm", "run", "test:e2e"]

[checks.ci]
required_names = ["lint", "typecheck", "unit", "build", "e2e"]
expected_producer = "github-actions"

[workflow.statuses]
backlog = "ACTUAL_STATUS_ID"
ready_refinement = "ACTUAL_STATUS_ID"
refining = "ACTUAL_STATUS_ID"
specification_review = "ACTUAL_STATUS_ID"
# Include every status in section 6; doctor resolves and checks all mappings.

[workflow.actions]
submit_refinement = "Submit for refinement"
approve_specification = "Approve specification"
request_specification_changes = "Request specification changes"
# Include all relevant actions; resolve actual transition IDs per issue.
```

Resolve relative paths against the config file, never a variable caller working directory. Validate URLs, enum values, identity, timeout bounds, paths and command argument lists. Only read one explicitly selected config; do not silently merge multiple project files. Reject unknown keys and produce useful diagnostics with secret redaction.

Required operator interface:

| Command | Behaviour |
|---|---|
| `delivery init --config <path>` | Write a commented local template only; refuse overwrite by default |
| `delivery doctor --config <path>` | Read-only integration/capability/workflow/identity/CI/protection checks with actionable results |
| `delivery workflow inspect --config <path>` | Show status/action mapping and missing/ambiguous routes |
| `delivery run --config <path>` | Foreground loop, one worker lock, graceful shutdown |
| `delivery run --once --config <path>` | Reconcile and process at most one eligible execution |
| `delivery status --config <path>` | Show active/pending/paused runs and exact next human actions |
| `delivery inspect <ticket> --config <path>` | Explain eligibility, current refs, gate and blocker without mutating |
| `delivery recover <ticket> --config <path>` | Show recovery proposal; perform only explicit safe resume/reconcile action |
| `delivery handover <ticket> --config <path>` | Stop/checkpoint owned worker; show readiness for human reassignment |

Use `delivery run --dry-run` or equivalent for discovery only: no Claude call, writes, comments or transitions. Doctor may offer a separately explicit authenticated Claude smoke test because it consumes subscription usage. Keep machine-readable JSON output alongside readable terminal output.

## 18 Suggested repository structure

Use these responsibilities; exact filenames can follow sensible Python conventions.

| Path | Responsibility |
|---|---|
| `pyproject.toml` and lockfile | Package, CLI, dependencies and development tooling |
| `src/delivery/cli.py` | Operator commands |
| `src/delivery/config.py` | One-file validation and path/secret references |
| `src/delivery/models.py` | Inputs, results, run and gate schemas |
| `src/delivery/workflow.py` | Pure routing and prerequisites |
| `src/delivery/coordinator.py` | Execution orchestration |
| `src/delivery/ownership.py` | Eligibility, machine lock and handover |
| `src/delivery/jira.py` | Jira adapter and ADF handling |
| `src/delivery/github.py` | PR, reviews, checks and merge observation |
| `src/delivery/git.py` | Worktrees, branches and immutable refs |
| `src/delivery/claude.py` | CLI capabilities, invocation and output parsing |
| `src/delivery/permissions.py` | Trusted stage execution profiles |
| `src/delivery/gates.py` | Checks, human decision validation and invalidation |
| `src/delivery/feedback.py` | Questions, comments and correlation |
| `src/delivery/journal.py` | Durable local execution/outbox records |
| `src/delivery/recovery.py` | Reconciliation of uncertain side effects |
| `src/delivery/publication.py` | Artefacts, comments and transition publication |
| `plugins/delivery/.claude-plugin/plugin.json` | Plugin manifest |
| `plugins/delivery/skills/<procedure>/SKILL.md` | Seven reusable delivery procedures |
| `plugins/delivery/references/` | Common contract and stage guidance |
| `plugins/delivery/templates/` | Specifications, plans, reports, release and human comment templates |
| `config/delivery.example.toml` | Single local config template |
| `docs/` | Setup, workflow, security boundary, recovery and live pilot runbook |
| `tests/` | Unit, fake integration, failure injection and opt-in live tests |
| `.github/workflows/ci.yml` | Framework checks, separate from app CI |

Keep credentials and application worktrees out of this repository. Keep modules proportionate; do not add a plugin marketplace server, job queue or unnecessary infrastructure.

## 19 Build sequence and milestone acceptance

### M0 Discovery and final clarification

Inspect workspace, existing app and integration constraints. Record chosen Jira profile, actor/auth split, status mappings, app standards, CI names, permitted tool profile and live reviewer. Identify unresolved blockers without pretending the build is complete. Create a concise implementation decision record.

Acceptance: the implementation can explain every stage and human action, has a clear one-file config contract, and separates live setup values from architecture questions.

### M1 Deterministic core and configuration

Build package/CLI, typed configuration, models, pure routing, input fingerprints, local lock and journal. Use fake Jira/GitHub/Claude boundaries initially. Cover forbidden routes and ownership. Implement read-only inspect/doctor output and dry-run.

Acceptance: a fake ticket enters only the correct stage, other-assignee tickets are ignored, unknown mappings are rejected and restarting cannot silently lose local publication intent.

### M2 Real Jira and Git adapters

Implement auth/capability discovery, polling, paginated comments/history, properties, available transitions and ADF. Implement isolated branches/worktrees, immutable links and publication markers. Test read-only connection first against the supplied real project/repo.

Acceptance: actual identity and issue selection are correct; a controlled fixture can publish a linked draft and a matching Jira comment; duplicate reconciliation attaches to existing resources.

### M3 Plugin and headless Claude runner

Create seven procedures/templates and shared schema. Test real local subscription invocation, plugin loading, permission denials, response parsing, timeout and process cleanup. Start with a small refinement envelope before implementation permissions.

Acceptance: Claude generates a valid draft/questions; questions reach Jira; missing plugin/schema/tools cannot lead to a success transition. No API billing fallback is configured.

### M4 Clarification and specification approval vertical slice

Complete Backlog -> ready refinement -> refinement -> clarification -> updated draft -> specification review -> approved revision. Add revision-specific change requests and input invalidation.

Acceptance: a human answers in Jira and receives a revised specification without editing Git files; approval binds to the correct version; stale/wrong-round decisions are rejected clearly.

### M5 Planning and implementation

Add plan approval, feature worktree/branch and PR publication against exact approved artefacts. For an empty repo, include a separate human-owned foundation setup task before the pilot feature; scaffold real scripts and app CI without letting ordinary feature tickets edit protections. For a populated repo, adapt to its conventions.

Acceptance: a feature PR implements agreed scope; an input or ownership change blocks success; neither worker nor coordinator merges.

### M6 Independent verification and human gates

Run fresh review and verification, coordinator-owned checks, GitHub CI reads and code/acceptance decision validation. Publish findings on the artefact branch. Complete a change-request loop and stale-approval invalidation.

Acceptance: an intentionally faulty candidate cannot progress; a fixed candidate has valid evidence for the exact head, an independent human GitHub review and separate acceptance.

### M7 Release proposal and verification

Add release preparation/review, human merge observation, local release recording, merge-strategy provenance and smoke verification. Failed smoke pauses for human action.

Acceptance: only a recorded accepted release can reach Done; coordinator never merges/deploys/rolls back; unrelated SHA cannot pass.

### M8 Recovery and operational hardening

Inject failures before/after every external operation and during Claude/check execution. Exercise offline Jira, rate limits, SIGINT, usage limit, uncertain PR/comment creation, changed assignee and remote divergence. Polish status/next-action messages, handover and runbook.

Acceptance: restart reconciles without blind duplicate writes, unsafe takeover or lost output, and the known cross-machine limitation is stated accurately.

### M9 Live pilot and completion report

Run the scenario in section 21. Preserve real ticket/PR/commit/check references and final artefacts. Report which checks actually ran, what remains manual and any configuration limitations. No blanket statement that all tests pass when live stages were skipped.

## 20 Required tests

Tests should verify important behaviours and integration boundaries, not repeat the implementation. Use a fake Claude executable for predictable failure tests and real Claude only for opt-in smoke/pilot tests. Fake APIs must model uncertain remote success and pagination, not merely return happy-path dictionaries.

| Test area | Cases that must be demonstrated |
|---|---|
| Ownership | Correct assignee selected; other assignee ignored; assignee changes mid-run; second same-machine worker refused; known other-machine marker blocks |
| Discovery | Ready while laptop offline; several statuses in one column; optional label; pagination; stable ordering; no pickup from review/blocked |
| Workflow | Every permitted route; illegal ready status after clarification; missing prerequisite; cancellation; no global arbitrary transition |
| Human decisions | Current token; stale token; unauthorised actor; edited/conflicting comments; automatic actor cannot approve; GitHub author cannot self-approve |
| Feedback | Multiple answer comments; missing answer; wrong round; spec v2 change request; selected feedback IDs; scope change invalidates dependencies |
| Claude | CLI unavailable; unsupported flags; missing plugin; malformed envelope; permission denial; timeout; nonzero exit; usage/auth limits; fresh reviewer isolation |
| Output safety | Traversal, symlink escape, missing file, identity mismatch, oversized output, fabricated worker check claim |
| Checks | Failing/missing local command; test process interrupted; stale CI; duplicate check names; wrong producer; check run plus commit status; pending/skipped/neutral conclusions |
| Git | Dirty worktree; wrong repo; diverged remote; failed push; unchanged frozen review SHA; artefact-only commit does not change PR head |
| Journal | Partial last record; atomic snapshot; interrupted checkpoint; secret redaction; property size cap |
| Reconciliation | Crash before/after push, comment, PR and transition; remote success with lost response; no blind duplicate writes |
| Release | Squash/merge/rebase provenance; unapproved candidate; unrelated release; failing smoke; no merge/deploy call |
| Handover | Stop first worker; second identity reconstructs shared context; active reassignment cannot auto-takeover |

Add framework lint, type checking and tests to its own CI. Require operator documentation commands to work from a clean install. Do not run a live mutation test against arbitrary production issues; use the explicitly chosen pilot issue/repository scope.

## 21 Live pilot scenario and definition of done

Preconditions: configured Jira workflow and ordinary developer/approver permissions; accessible repository; real app foundation and checks; local Claude login; second human reviewer; one active worker for the assigned identity. Run doctor and a dry-run first.

For an empty synthetic catalogue app, use a small feature such as case-insensitive title search. The foundation app is prerequisite work, not an undocumented special bypass of all feature gates.

Create a ticket with a deliberately open question about whether search includes synopsis text. Include criteria for matching, clearing search, empty results and keyboard operation. Then:

1. Assign the ticket to the operator and Submit for refinement. Confirm another developer's ticket is ignored.
2. Receive questions and a draft linked from Jira. Answer in comments and Submit answers.
3. Review specification v1, request one change in Jira and receive v2. Approve v2 using the published token.
4. Review/approve the plan. Observe development and the implementation PR.
5. Verify fresh independent review and actual checks. Exercise a fault/change-request loop using a dedicated test fixture or controlled candidate, without inserting a defect into a real accepted feature just to show a demo.
6. Obtain independent human GitHub review and Jira code approval, then product acceptance.
7. Approve the release proposal. Human merges and records the local pilot release/environment.
8. Verify smoke evidence and Done, with exact references back to the ticket and codebase.
9. Interrupt one safe pre-publication run and restart. Demonstrate recovery from a separately injected uncertain publication fixture. Never manufacture a dangerous production failure just to test recovery.

Definition of done: installable framework, usable one-file config, all seven procedures, real Jira/GitHub/Claude integration, meaningful required tests, honest permission boundary, completed live ticket/feature evidence where access is supplied, and setup/recovery/handover documentation. Report partial completion explicitly if credentials, permissions or human actions prevent the live lifecycle from finishing.

## 22 What Claude Code should return

- Working source code and reusable plugin in the delivery repository, plus application standards only where needed.
- A clear root README for a new developer. Implement and verify the proposed onboarding commands before marking the README ready; include prerequisites, install, one-file config, auth, doctor/dry-run, first ticket, approvals, troubleshooting, safe stop/recovery/handover and where records live. No secret examples or untested placeholder install commands in the finished README.
- Separate Jira workflow setup instructions for an administrator, including Jira Software Kanban company-managed selection, statuses, transitions, role/field conditions, scheme attachment, board mapping and positive/negative acceptance checks. Match the shipped coordinator rather than leaving these as a generic diagram.
- A commented one-file configuration template and resolved setup checklist for the actual Jira/GitHub project.
- Canonical workflow/status/action mapping, readable board guidance and any optional import/API artefact clearly marked as setup-only.
- Installation and foreground CLI commands, doctor and dry-run output, and subscription/permission smoke evidence.
- Human comment/approval templates and linked example specification, plan, review and release documents.
- Test results, failure-recovery evidence and real pilot issue/PR/commit/check links.
- A concise completion report distinguishing implemented, tested locally, tested against live services and still blocked.

## 23 Official references to verify during implementation

These references support API/CLI capabilities. The ownership, workflow, journal, branch strategy and approval protocol above are project design decisions, not claims of an industry standard. Interfaces can change; recheck the installed versions and current official documentation.

1. Claude Code programmatic execution, structured output and skill invocation: https://code.claude.com/docs/en/headless
2. Claude Code CLI flags: https://code.claude.com/docs/en/cli-reference
3. Claude Code authentication and credential precedence: https://code.claude.com/docs/en/authentication
4. Claude Code permissions: https://code.claude.com/docs/en/permissions
5. Claude Code sandbox boundaries: https://code.claude.com/docs/en/sandboxing
6. Claude plugin manifest and directory structure: https://code.claude.com/docs/en/plugins-reference
7. Jira Cloud enhanced issue search: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/
8. Jira issue properties: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-properties/
9. Jira workflow create/validation APIs: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-workflows/
10. GitHub protected branches: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches
11. GitHub required human reviews: https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews
12. GitHub check runs: https://docs.github.com/en/rest/checks/runs
13. GitHub commit statuses: https://docs.github.com/en/rest/commits/statuses
14. GitHub PR reviews: https://docs.github.com/en/rest/pulls/reviews

## 24 Paste this opening instruction into Claude Code

```text
Read Claude_Code_AI_SDLC_Build_Handoff.md in full. It is the agreed design for a local,
Jira-driven delivery coordinator and reusable Claude delivery plugin. Start with M0:
inspect this workspace, identify the implementation destination and any existing app,
then give me one concise clarification round for values or constraints that genuinely
block implementation. Use the documented defaults for routine choices. Continue useful
local implementation while missing live integration details are being supplied.

Build the working framework through M1-M9, not just a scaffold. Use my locally authenticated
Claude Code subscription through the CLI; do not add paid API fallback. Implement ownership,
versioned human gates, questions in Jira, independent review and durable recovery. Keep one
human-edited local configuration file. Test important failure paths and use doctor/dry-run
before live actions. Keep live mutations scoped to the pilot project, ticket and repository
I identify. Humans own approval, merge and release. Report real evidence and remaining
blockers accurately, and do not claim the full live lifecycle is complete until it is.
```
