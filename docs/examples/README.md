# Example artefacts (real run)

These files were produced on 1 October 2026 by the **real** Claude Code 2.1.278 CLI running
all seven delivery procedures through the supervisor, against a copy of the pilot task-list app
with real Git and the app's real gates (`npm ci`, lint, typecheck, unit, build, e2e). Jira and
GitHub were simulated because the live project did not exist yet; the humans' decisions were
scripted (answer, approvals, independent review, merge, release record).

Ticket: "Add title search to the task list", with an explicit open question about also matching
the description.

| File | Produced by |
|---|---|
| `PILOT-1/specification/v001.md` | refine-ticket: draft that asks the open question (round R1) |
| `PILOT-1/specification/v002.md` | refine-ticket: revision after the answer; approved as `PILOT-1-SPEC-v2` |
| `PILOT-1/plan/v001.md`, `v001.footprint.json` | plan-ticket: plan with criterion-to-test mapping; footprint used for overlap detection |
| `PILOT-1/implementation.diff` | implement-ticket: the candidate (committed and pushed by the coordinator, PR opened) |
| `PILOT-1/reviews/<run>/review.md`, `verification.md` | review-ticket and verify-ticket in fresh sessions; `checks.json` holds the coordinator's candidate and integration-tree results |
| `PILOT-1/releases/v001.md` | prepare-release: release proposal for the accepted candidate |
| `PILOT-1/releases/<release>/verification.md`, `provenance.json` | verify-release, plus the coordinator's merge provenance (merge commit, merged tree equals the candidate) |

Each file starts with a provenance header written by the coordinator (ticket, revision, run,
input digest). Local paths were replaced with placeholders; per-run execution records are
omitted for brevity.
