# Release proposal

> Provenance (ticket, revision, accepted candidate SHA) is added by the coordinator.

## Release notes
User-facing summary of the change.

## Candidate
- Accepted candidate SHA: ...
- Environment profile: local-pilot

## Pre-merge checklist
- [ ] Independent human GitHub review on the current head
- [ ] Required CI passing on the current head and integration with the latest base
- [ ] Product acceptance recorded in Jira

## Smoke checks
1. ...

## Rollback
1. ...

## Recording the release
A human merges the PR; that merge is the release. The coordinator reads the merge commit from
GitHub, records it and runs release verification on it. Nothing is recorded by hand.
