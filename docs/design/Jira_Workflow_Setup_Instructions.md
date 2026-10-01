# Jira workflow setup instructions for the local AI SDLC

Version 1.0 | 1 October 2026 | Audience Jira administrator and delivery lead

Configure this once per Jira project before developers connect their local coordinators. These instructions define the required operating workflow. They do not imply that a workflow has already been created in your Jira site. The coordinator build handoff contains the corresponding implementation contract.

## 1 Which Jira project to create

Choose **Jira Software, Kanban template, Company-managed**.

On the supplied template-picker screenshot, select the Kanban card at the top right. Select Use template, then Company-managed when choosing how the project is managed. Give it a clear name such as AI SDLC Pilot and key such as PILOT. Jira may label this a software space instead of a project; the relevant choice is still company-managed. Company-managed creation needs Administer Jira access [1].

Kanban is our recommendation because tickets flow continuously through stages. Sprints are optional; an existing company-managed Scrum project can use the same status contract. We are building a custom local coordinator, so the agentic engineering template is not needed to define this workflow. Service management is not the selected pilot profile.

For an existing project, inspect and map its statuses first. Copy shared workflows/schemes before changing them, and review existing ticket migration. Do not alter a workflow shared with unrelated teams. A team-managed existing project requires an explicit compatibility assessment; it is not the default administrator setup in this guide.

Check plan capabilities. Jira Free limits permission-scheme and role configuration [2]. You can explore the functional flow under those limits, but do not claim that approver/worker role separation is enforced without the required capabilities. Strong enforcement may require a paid plan and separate worker identities.

## 2 People and permissions

Identify:

- Jira administrator: configures workflows, fields, schemes and permissions.
- Developers: own tickets and run their local coordinators.
- Approvers: review specifications/plans and product acceptance.
- GitHub reviewer: a human other than the PR author for the code gate.
- Release owner: performs the human merge and agreed pilot release.

The roles may overlap where appropriate. GitHub author self-approval cannot satisfy independent review.

Normal coordinator access needs browse/read issues, comments/history, editing configured fields/properties, adding comments and permitted transitions. It does not need Jira administration. Local coordinator eligibility uses the configured developer account ID and ticket assignee, irrespective of who moves the ticket.

If the coordinator authenticates as the developer, Jira sees the same actor for automated and human actions. Coordinator code must prohibit automatic approval, but Jira roles cannot separate those two uses. Strict separation requires a separate worker/service identity and associated permissions; confirm this choice with the implementer.

## 3 Create or copy the workflow

In Jira administration, find Work items or Issues, then Workflows. Menu labels vary by UI version. Create a dedicated workflow named AI SDLC v1, or copy an existing project-specific workflow and replace its routes deliberately. Do not use global transitions that allow any status to move to any other status.

The initial create transition must lead to Backlog. Use distinct named transitions for human actions. A drag between columns may not provide the required feedback/decision input; named actions and supplied comment templates are the reliable human process.

The Jira Cloud workflow API can create a route skeleton, but that alone does not configure fields, permissions, approval validation, workflow-scheme assignment or board columns [3]. Claude Code may provide a validated setup payload/helper after building the framework. The earlier standalone payload is a draft and should be regenerated to match this final contract before use.

## 4 Required statuses and categories

Create the following statuses, or map equivalent existing ones. Keep the actual status IDs for the coordinator configuration. Reuse existing global statuses carefully; do not create duplicate names to avoid a mapping problem.

| Status | Jira category | Meaning |
|---|---|---|
| Backlog | To do | Brief being prepared |
| Ready for refinement | To do | Eligible for specification work |
| Refining | In progress | Agent drafting or revising specification |
| Specification review | In progress | Human specification decision |
| Ready for planning | To do | Approved scope ready for planning |
| Planning | In progress | Agent producing plan |
| Plan review | In progress | Human plan decision |
| Ready for development | To do | Approved plan ready for implementation |
| Developing | In progress | Agent implementing |
| Ready for verification | To do | Candidate ready for independent checks |
| Verifying | In progress | Fresh reviewers and real checks |
| Code review | In progress | Human PR review gate |
| Acceptance review | In progress | Human acceptance against brief |
| Changes requested | In progress | Feedback awaiting resubmission |
| Needs clarification | In progress | Questions awaiting human answers |
| Blocked | In progress | Execution cannot safely continue |
| Ready for release preparation | To do | Accepted candidate ready for release proposal |
| Preparing release | In progress | Agent preparing release documents |
| Release review | In progress | Human release proposal decision |
| Ready for release | To do | Human merge/release authorised |
| Ready for release verification | To do | Human-recorded release ready for smoke checks |
| Verifying release | In progress | Agent checking recorded release |
| Done | Done | Recorded release verified |
| Cancelled | Done | Work intentionally stopped |

Ready statuses are machine queues. Their category can be aligned with an existing reporting policy if needed, but category is not the trigger: the exact mapped status ID is.

## 5 Main lifecycle transitions

Create these directed paths. The six automated stages each have a start and completion action.

| From | Action | To | Actor |
|---|---|---|---|
| Backlog | Submit for refinement | Ready for refinement | Human |
| Ready for refinement | Start refinement | Refining | Coordinator |
| Refining | Complete refinement | Specification review | Coordinator |
| Specification review | Approve specification | Ready for planning | Human approver |
| Ready for planning | Start planning | Planning | Coordinator |
| Planning | Complete planning | Plan review | Coordinator |
| Plan review | Approve plan | Ready for development | Human approver |
| Ready for development | Start development | Developing | Coordinator |
| Developing | Complete development | Ready for verification | Coordinator |
| Ready for verification | Start verification | Verifying | Coordinator |
| Verifying | Complete verification | Code review | Coordinator |
| Code review | Approve code | Acceptance review | Human reviewer |
| Acceptance review | Accept delivery | Ready for release preparation | Human approver |
| Ready for release preparation | Start release preparation | Preparing release | Coordinator |
| Preparing release | Complete release preparation | Release review | Coordinator |
| Release review | Approve release | Ready for release | Human release owner |
| Ready for release | Record release | Ready for release verification | Human release owner |
| Ready for release verification | Start release verification | Verifying release | Coordinator |
| Verifying release | Complete release verification | Done | Coordinator |

Human approvals require the current versioned decision token supplied in Jira. Approve code also requires independent human GitHub review and passing CI for the current candidate. Record release requires the merge/release commit and environment. The coordinator validates these facts before starting the next stage; ordinary native Jira conditions do not independently check GitHub CI.

## 6 Changes and failed verification

| From | Action | To |
|---|---|---|
| Specification review | Request specification changes | Ready for refinement |
| Plan review | Request plan changes | Ready for planning |
| Verifying | Verification failed | Changes requested |
| Code review | Request code changes | Changes requested |
| Acceptance review | Request acceptance changes | Changes requested |
| Changes requested | Submit implementation changes | Ready for development |
| Changes requested | Revise scope | Ready for refinement |
| Release review | Request release changes | Ready for release preparation |

Request changes needs a comment tied to the current document/candidate token. Submit implementation changes is permitted only for work within the approved scope; scope amendments return to refinement. The coordinator records superseded downstream approvals rather than treating them as current.

## 7 Clarification and blocked routes

For each active status, add Ask questions -> Needs clarification and Block stage -> Blocked. Before transitioning, the coordinator publishes the reason/questions and sets the origin stage in shared records and the routing field described below.

Create six distinct resume actions from each paused status. Use stage conditions so only the correct one is offered:

| Delivery resume stage | Needs clarification action | Blocked action | Destination |
|---|---|---|---|
| refinement | Submit refinement answers | Resume refinement | Ready for refinement |
| planning | Submit planning answers | Resume planning | Ready for planning |
| development | Submit development answers | Resume development | Ready for development |
| verification | Submit verification answers | Resume verification | Ready for verification |
| release_preparation | Submit release preparation answers | Resume release preparation | Ready for release preparation |
| release_verification | Submit release verification answers | Resume release verification | Ready for release verification |

Also route failed release verification from Verifying release to Blocked with release_verification recorded.

Human order is always: read the exact draft and questions, comment answers with the round token, then use Submit answers. For Blocked, resolve the cause and confirm the previous worker is stopped before Resume. If a human selects an inconsistent route, the coordinator must reject it clearly even if the Jira UI allowed it.

## 8 Fields and transition rules

Create a single-select custom field named Delivery resume stage with these exact canonical options:

```text
refinement
planning
development
verification
release_preparation
release_verification
```

Limit its field context to the relevant project/issue types. Record its `customfield_...` ID in the local config. The coordinator sets it before moving to Needs clarification or Blocked; it must agree with the execution record. Clear or update it when that paused cycle ends. Do not rely on an old stale field value.

Use a Value Field condition for each resume transition to match its option, together with the intended user/role condition. Atlassian documents field and actor conditions in advanced workflows [4]. Test the actual option comparison for your field/editor; do not assume a displayed label and an option ID are interchangeable.

Optional display fields: current gate token, current clarification round, latest artefact link and execution state. These improve readability but must not replace the versioned execution/approval record. V1 supports decision/answer comments with templates, so no long list of mandatory custom fields is needed.

For human approval actions, restrict to intended approvers where supported. For submission/resume actions, allow the assignee or agreed delivery role. For worker actions, restrict to worker identities if separate identities are in use. Avoid a native Only Assignee condition on a worker action if the worker authenticates through a separate service account; enforce developer assignment in the coordinator instead.

Where transition screens are used, expose the required comment or decision inputs and combine them with validators. A nonempty comment validator cannot establish that a token/version is correct. Native rules handle available paths and required fields; the coordinator verifies exact correlation, artefact versions and external evidence. If the desired rule needs an extension, either configure one deliberately or document the coordinator-enforced fallback.

A Jira issue property is not a normal custom field. Do not configure native Value Field conditions against arbitrary hidden execution properties.

## 9 Cancellation and resolution

Allow cancellation from unfinished stages. During active work, the operator must first stop/checkpoint the owned worker; a human can then cancel safely. If cancellation occurs during execution, the coordinator must detect it, terminate the child and suppress subsequent success publication. Jira alone cannot terminate a process on a laptop.

Set an appropriate resolution on Done and Cancelled. Keep resolution empty for all active, ready, review and paused states. V1 has no automatic reopen path from Done; a later change is a new linked ticket or a separately designed reopen process.

## 10 Attach the workflow to the project

Create a dedicated workflow scheme and map Story, Task and Bug to AI SDLC v1, or only the types selected for the pilot. Keep Epics on their planning workflow unless explicitly supported. Do not accidentally attach the automation lifecycle to every issue type.

Associate the scheme with the chosen company-managed project and publish. Jira workflows are associated through workflow schemes [5]. If existing issues need status migration, review the mapping and affected issue counts first. Keep other projects using copied/shared schemes unchanged.

## 11 Configure the Kanban board

Open the board's configuration and map every workflow status. Several statuses may share one column [6]. Use:

| Column | Mapped statuses |
|---|---|
| Backlog | Backlog |
| Ready | Ready for refinement, planning, development, verification, release preparation and release verification |
| Agent working | Refining, Planning, Developing, Verifying, Preparing release, Verifying release |
| Needs clarification | Needs clarification |
| Human review | Specification review, Plan review, Code review, Acceptance review, Release review, Changes requested |
| Blocked | Blocked |
| Ready for release | Ready for release |
| Done | Done, Cancelled |

Suggested board filter: the selected project, ordered by Rank. Add a My tickets quick filter using `assignee = currentUser()`. Display assignee and status on cards. If the existing project uses an opt-in label, add an automation-specific quick filter rather than hiding unrelated work from the team's primary board.

The board grouping does not change trigger logic. No Jira Automation rule or incoming webhook is required for local polling. The laptop must be awake, online and running its coordinator.

## 12 Give these values to each developer

- Jira site URL and project key.
- Supported issue types and all actual status IDs.
- Transition action names and any known actor restrictions.
- Delivery resume stage field ID.
- Developer account ID and approver account IDs or supported role mapping.
- Whether an opt-in label is required.
- Chosen runtime authentication profile; secrets supplied securely, not in this document.
- Repository URL, base branch and the independently configured GitHub gates.

Put these non-secret values into the single local coordinator config. The workflow/status mapping must agree with the implemented coordinator; names alone are insufficient when duplicated.

## 13 Positive and negative setup checks

Run these before enabling unattended work:

- New ticket starts in Backlog and does not trigger Claude.
- Moving an assigned ticket to the correct ready status makes it eligible only for its assignee's worker.
- Human review, Needs clarification and Blocked do not trigger new work.
- Specification and plan change requests return to the right stage with correlated feedback.
- A clarification originating in planning exposes only the planning resume action where configured.
- Wrong-stage submission or missing approval is refused by Jira or clearly blocked by the coordinator.
- Unauthorised approval is rejected; automated output cannot count as human approval.
- Required GitHub checks and review match the current candidate.
- A scope amendment invalidates downstream approval.
- Done and Cancelled set resolution; no paused status does.
- Ordinary developers can use intended transitions without receiving Jira admin access.
- Existing unrelated projects/issues remain unaffected.

## 14 First ticket template

```text
Summary: Add title search to the synthetic TV catalogue
Problem: Users need to find a programme quickly without browsing the full list.
Scope: Search checked-in synthetic metadata only.
Excluded: External APIs, authentication and production deployment.
AC1: Title search matches without case sensitivity.
AC2: Clearing the search restores the catalogue.
AC3: No matches show an explicit empty-results message.
AC4: Search can be used with a keyboard and has a visible label.
Open question: Should search include synopsis text as well as the title?
Assignee: Named developer running the local coordinator.
Approver: Agreed product/delivery approver.
```

This ticket assumes a runnable catalogue foundation already exists. Begin in Backlog, then Submit for refinement. The open question should exercise clarification and the answer cycle. Drafts/plans/reports live in the application repository; questions, feedback and approvals live in Jira with immutable links between them.

## Official references

These document capabilities; the specific lifecycle above is our design. Recheck current UI labels when configuring your site.

1. Create a company-managed project or space: https://support.atlassian.com/jira-cloud-administration/docs/create-and-edit-a-project/
2. Free-plan permission limitations: https://support.atlassian.com/jira-cloud-administration/docs/permissions-and-issue-level-security-in-free-plans/
3. Jira workflow creation API: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-workflows/
4. Conditions and validators: https://support.atlassian.com/jira-cloud-administration/docs/configure-advanced-issue-workflows/
5. Workflow and scheme concepts: https://support.atlassian.com/jira-cloud-administration/docs/work-with-issue-workflows/
6. Board/status mapping: https://support.atlassian.com/jira-software-cloud/docs/configure-a-company-managed-board/
