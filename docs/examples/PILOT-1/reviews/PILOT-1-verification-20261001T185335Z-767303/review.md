<!-- delivery provenance (written by the coordinator) -->
<!-- ticket: PILOT-1 | kind: review | revision: PILOT-1-verification-20261001T185335Z-767303 | run: PILOT-1-verification-20261001T185335Z-767303 -->
<!-- input_revision: 62a5b28d3c7682c229d65d9b5e8b00e65fe92b08b1c652361e1fa1240f35b9d5 | worker: test-laptop -->
<!-- candidate_sha: be65a480e2015f78a93eef191ba0c88aafaff177 -->
<!-- base_sha: 51a20d5f3ecf3375e1776c943ba203f7e4d263b0 -->
<!-- integration_tree: b0211d764a290020debbf680659c16ad6701b380 -->
<!-- integration_with: base only -->

# Independent review

> Provenance (ticket, run, candidate SHA, inputs) is added by the coordinator.

## Verdict summary
The candidate implements title search exactly as specified in v002 of the specification and
the approved plan: a `search` field is added to `TaskQuery`, `filterTasks` ANDs a
case-insensitive, title-only substring match with the existing status check, and `App.tsx`
wires a labelled text input into the existing filter call. All four changed/added files match
the plan's "Affected files" table with no extra files touched. All four acceptance criteria
have direct unit, component and/or e2e test coverage that exercises the described behaviour,
and the tests' expected values match the actual fixture data (verified by hand against
`src/data/tasks.ts`). This is a proposal, not an approval.

## Acceptance criteria
| Criterion | Status (met / not met / unverified) | Evidence |
|---|---|---|
| AC1 (case-insensitive, title-only match) | met | `src/domain/filter.ts:13-19` lower-cases both `query.search` and `task.title` before `includes`; never reads `task.description`. Unit tests `src/domain/filter.test.ts` "matches titles case-insensitively" and "matches title text only, not description" (the latter searches `"steps a new starter"`, which appears only in t-001's description, and asserts `[]`). Component test `src/App.test.tsx` "narrows the list to tasks whose title matches the search text". |
| AC2 (clearing restores full list) | met | `src/domain/filter.ts:14` trims the search term, so empty/whitespace search is treated as "no filter". Unit test "treats an empty search as no filter". Component test "restores the full list when the search text is cleared" (`user.clear`). E2E test "searches and clears to restore the list" (fill "onboarding" → count 1, fill "" → count 12, matching the 12-item fixture in `src/data/tasks.ts`). |
| AC3 (explicit empty-results message) | met | `src/App.tsx` passes the search-filtered list through the same `visible` computation that already feeds `TaskList`, and `TaskList.tsx:10-12` (unchanged) renders `<p role="status">No tasks match the current filter.</p>` whenever the list is empty, regardless of which filter caused it. Component test "shows the empty-results message when search matches no task" confirms this for the search case specifically. |
| AC4 (keyboard operable, visible label) | met | `src/App.tsx:62-68` renders `<label htmlFor="title-search">Search</label>` paired with `<input id="title-search" ...>`, a plain visible (non-sr-only) label matching the existing status control's pattern. Component test "has a visible label and can be operated using the keyboard alone" uses `getByLabelText`, `.focus()` and `user.keyboard()` (no pointer events) and asserts focus + typed value + filtered result. |

## Findings
| ID | Severity | Location | Description |
|---|---|---|---|
| F1 | info | `src/App.tsx:63-68` | `type="search"` on the input is a reasonable native-semantics choice not mandated by the spec; some browsers render a built-in clear ("×") control for this input type. This is cosmetic only and doesn't affect any acceptance criterion or test. |

No blocker, major or minor findings. No evidence of scope creep, weakened tests, missing
coverage, or standards violations found.

## Scope and standards
- Footprint matches the plan exactly: `src/domain/filter.ts`, `src/domain/filter.test.ts`,
  `src/App.tsx`, `src/App.test.tsx`, `e2e/app.spec.ts`. No unrelated files touched (e.g.
  `TaskList.tsx` and `data/tasks.ts` are untouched, as the plan predicted).
- `TaskQuery.search` is additive to an existing interface per the architecture constraint
  (extend `TaskQuery` in `src/domain/filter.ts` rather than ad-hoc component filtering);
  `grep` confirms the only two call sites of `filterTasks` (`src/App.tsx` and
  `filter.test.ts`) were both updated, so the now-required `search` field doesn't silently
  break another caller.
- No array-index React keys introduced; no new runtime dependency added (plain controlled
  `<input>`); domain logic stays a pure, named, exported function (`filterTasks`); component
  stays thin (state + one `filterTasks` call).
- No existing tests were weakened — the pre-existing three `filterTasks` unit tests were
  updated only to add the new required `search: ""` field, their assertions are unchanged.
- Accessibility: label association (`htmlFor`/`id`) and reuse of the existing `role="status"`
  live-region pattern both match the specification's non-functional requirements.
- Data: no new or real data introduced; search operates only on the existing synthetic
  fixture in `src/data/tasks.ts`.

## Interactions with other in-flight work
`related_work` in the envelope is empty, and the plan's "Coordination" section states no
other in-flight ticket's footprint overlaps `src/domain/filter.ts`, `src/App.tsx`, their
tests, or `e2e/app.spec.ts`. There is no other in-flight ticket information available to this
review to check behavioural interaction against, so no related-tickets flag is raised. This
review does not treat non-overlapping file paths alone as proof of independence — it is simply
reporting that no related work was supplied for comparison.
