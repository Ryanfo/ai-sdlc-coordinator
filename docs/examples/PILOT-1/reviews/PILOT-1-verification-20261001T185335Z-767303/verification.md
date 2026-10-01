<!-- delivery provenance (written by the coordinator) -->
<!-- ticket: PILOT-1 | kind: verification | revision: PILOT-1-verification-20261001T185335Z-767303 | run: PILOT-1-verification-20261001T185335Z-767303 -->
<!-- input_revision: 62a5b28d3c7682c229d65d9b5e8b00e65fe92b08b1c652361e1fa1240f35b9d5 | worker: test-laptop -->
<!-- candidate_sha: be65a480e2015f78a93eef191ba0c88aafaff177 -->
<!-- base_sha: 51a20d5f3ecf3375e1776c943ba203f7e4d263b0 -->
<!-- integration_tree: b0211d764a290020debbf680659c16ad6701b380 -->
<!-- integration_with: base only -->

# Verification report

> Provenance (ticket, run, candidate SHA, tested trees) is added by the coordinator.

## Observed evidence per criterion
| Criterion | Observation | Command / test | Status |
|---|---|---|---|
| AC1 (case-insensitive, title-only match) | `filterTasks` in `src/domain/filter.ts:13-19` lower-cases and trims `query.search`, compares against `task.title.toLowerCase()` only, never reads `task.description`. Ran the unit suite myself: "matches titles case-insensitively" (search `"ONBOARDING"` → `["t-001"]`) and "matches title text only, not description" (search `"steps a new starter"`, which is in t-001's description only → `[]`) both pass. Component test "narrows the list to tasks whose title matches the search text" also passes. Verified against the actual fixture in `src/data/tasks.ts`: "onboarding" appears only in t-001's title; "steps a new starter" appears only in t-001's description. | `npx vitest run` (local) | met |
| AC2 (clearing restores full list) | `filterTasks` trims the search term, so `"   "` or `""` is treated as no filter (`src/domain/filter.ts:14,18`). Ran unit test "treats an empty search as no filter" and component test "restores the full list when the search text is cleared" (`user.clear`) myself — both pass. E2E test "searches and clears to restore the list" is in `coordinator_checks.json`/`check-logs/candidate-e2e.log` (fill "onboarding" → 1 item, fill "" → 12 items, matching the 12-item fixture) — not rerun locally since Playwright/Chromium cannot launch in this sandbox; coordinator's result is the e2e evidence. | `npx vitest run` (local); `candidate-e2e.log`, `integration-e2e.log` (coordinator) | met |
| AC3 (explicit empty-results message) | `App.tsx` feeds the search-filtered list through the same `visible` value already passed to `TaskList`; `TaskList.tsx:10-12` (unchanged) renders `<p role="status">No tasks match the current filter.</p>` whenever that list is empty, independent of which filter caused it — confirmed by reading the unchanged file. Ran component test "shows the empty-results message when search matches no task" myself (search `"no such task title"` → `role="status"` element with that text) — passes. | `npx vitest run` (local) | met |
| AC4 (keyboard operable, visible label) | `App.tsx:62-68` renders a plain, visible `<label htmlFor="title-search">Search</label>` paired with `<input id="title-search">`, following the existing status control's pattern (not sr-only). Ran component test "has a visible label and can be operated using the keyboard alone" myself: it resolves the input via `getByLabelText`, calls `.focus()`, asserts focus, types via `user.keyboard()` (no pointer events), and asserts the filtered result — passes. E2E coverage (`page.getByLabel("Search").fill(...)`) is in `candidate-e2e.log`/`integration-e2e.log` per the coordinator's run. | `npx vitest run` (local); `candidate-e2e.log`, `integration-e2e.log` (coordinator) | met |

## Commands run
| Command | Result | Notes |
|---|---|---|
| `npm ci` | passed | 247 packages installed, 0 vulnerabilities |
| `npx vitest run` | passed | 3 test files, 18 tests, all pass (includes the new filter and App tests listed above plus pre-existing tests, none weakened) |
| `npx eslint . --max-warnings 0` | passed | no output, no warnings/errors |
| `npx tsc --noEmit -p tsconfig.json` | passed | no output, no type errors |
| `git diff <base>..<candidate> --stat` | observed | confirms exactly the 5 files the plan names: `src/domain/filter.ts`, `src/domain/filter.test.ts`, `src/App.tsx`, `src/App.test.tsx`, `e2e/app.spec.ts`; no other files touched |

## Findings
No new findings beyond the review's info-level note (`src/App.tsx:63-68` uses `type="search"`, a cosmetic native-semantics choice with no effect on any acceptance criterion).

## Notes
Playwright e2e tests cannot launch Chromium in this worker sandbox on macOS, so the two e2e
specs (`filters tasks and persists a starred task across reloads`, `searches and clears to
restore the list`) were not rerun here. The coordinator's own results in
`coordinator_checks.json` and `check-logs/candidate-e2e.log` /
`check-logs/integration-e2e.log` show both passing on both the candidate and the integration
tree, and are cited above as the e2e evidence for AC2 and AC4. All configured checks (lint,
typecheck, unit, build, e2e) passed on both the candidate and integration trees per
`coordinator_checks.json`. Working tree is clean (`git status`); no tracked files were
modified during this verification.
