<!-- delivery provenance (written by the coordinator) -->
<!-- ticket: PILOT-1 | kind: release_verification | revision: 66d77086ee93-local-pilot | run: PILOT-1-release_verification-20261001T185823Z-eaea83 -->
<!-- input_revision: ecc2f57412ceffeffe8231faef46b2fd1bd478aa1d78c137769b566f3b70a98d | worker: test-laptop -->
<!-- released_commit: 66d77086ee9390780200c667446275dc14fc2f89 -->

# Release verification

> Provenance (ticket, release record, released commit, provenance chain) is added by the coordinator.

## Release record
- Released commit: `66d77086ee9390780200c667446275dc14fc2f89` (comment 1037, RECORD RELEASE PILOT-1-RELEASE-v1)
- Environment: `local-pilot`
- Merged PR: #1
- Provenance (`release_record.json`): candidate `be65a480e2015f78a93eef191ba0c88aafaff177` == PR head == merge commit tree; `released_equals_merge: true`; merged by a human; strategy `merge commit`. Current working tree is checked out at `66d77086ee9390780200c667446275dc14fc2f89` (`Merge #1`), matching the recorded release.
- Coordinator check logs (`coordinator_checks.json`): only a `setup` check is present (`npm ci`, exit 0, 247 packages installed). No e2e run results are recorded for this verification run, so no e2e log evidence could be cited (see F1).

## Smoke evidence
| Step | Observation | Status |
|---|---|---|
| 1. Check out candidate, `npm ci` | Working tree already at released commit `66d7708`; `npm ci` completed, 247 packages, 0 vulnerabilities | Pass |
| 2–3. Run dev server, see list + labelled "Search" box next to status filter | Not run live (no browser in this sandbox); confirmed instead via component test `App.test.tsx` "lists every task with a labelled status filter" and "narrows the list to tasks whose title matches the search text", which render `<App>` and assert a `Search`-labelled control exists alongside `Status` | Pass (via component test) |
| 4. Case-insensitive title search narrows list (AC1) | `filter.test.ts` "matches titles case-insensitively" (search `ONBOARDING` → only `t-001`); `App.test.tsx` "narrows the list to tasks whose title matches the search text" (search `onboarding` → 1 item, "Draft onboarding checklist") | Pass |
| 5. Clearing search restores full list, respecting status filter (AC2) | `App.test.tsx` "restores the full list when the search text is cleared" (type then `user.clear`, full `TASKS.length` list returns); `filter.test.ts` "combines search with the status filter" confirms status + search compose correctly | Pass |
| 6. No-match search shows explicit empty message (AC3) | `App.test.tsx` "shows the empty-results message when search matches no task" asserts `role="status"` text "No tasks match the current filter." | Pass |
| 7. Keyboard-only operation with visible label (AC4) | `App.test.tsx` "has a visible label and can be operated using the keyboard alone": `getByLabelText("Search")`, `.focus()`, `user.keyboard(...)`, asserts focus and resulting value/filtered list | Pass |
| 8. Status filter / starred-task behaviour unaffected | `App.test.tsx` "filters by status and shows an explicit empty state" and "stars a task with the keyboard and persists it" both pass unchanged | Pass |
| 9. Full configured checks locally | `npm run lint` — clean, no errors (max-warnings 0); `npm run typecheck` — clean; `npm run test:unit` — 3 files, 18 tests, all passed; `npm run build` — `tsc --noEmit` then `vite build` succeeded, `dist/index.html` + bundle emitted | Pass |

Live interactive smoke steps 2–8 against a running `npm run dev`/browser could not be performed directly: this worker sandbox cannot launch a browser, and attempts to start a local preview server and probe it over HTTP (`npm run preview`, `curl 127.0.0.1`) were denied by the session's command permissions. In their place, the same behaviour (AC1–AC4, status filter, starred-task persistence, visible labels, keyboard operability) is exercised end-to-end through React Testing Library component tests that render the real `App` component and `filterTasks`/`sortTasks` domain unit tests — not mocked UI. Browser-level (Playwright) e2e confirmation is deferred to the coordinator per the procedure; see F1.

## Findings
| ID | Severity | Description |
|---|---|---|
| F1 | minor | `coordinator_checks.json` for this release-verification run contains only a `setup` (`npm ci`) check; no Playwright e2e run (candidate or integration tree) is recorded and no e2e logs are present under `check-logs/` to cite as browser-level evidence for AC1–AC4. All other configured checks (lint, typecheck, unit, build) were re-run locally and passed, and the same scenarios are covered by component tests, but true browser e2e confirmation of the released commit is outstanding. |

## Evidence
- AC1 (case-insensitive title search): `filter.test.ts::filterTasks "matches titles case-insensitively"`, `App.test.tsx "narrows the list to tasks whose title matches the search text"` — met.
- AC2 (clearing search restores full list): `App.test.tsx "restores the full list when the search text is cleared"`, `filter.test.ts "combines search with the status filter"` — met.
- AC3 (explicit empty-results message): `App.test.tsx "shows the empty-results message when search matches no task"` — met.
- AC4 (keyboard-operable, visibly labelled search): `App.test.tsx "has a visible label and can be operated using the keyboard alone"` — met.
