---
name: resolve-conflicts
description: Resolve the merge conflicts the coordinator left in a feature worktree after merging the latest base branch into it. Invoked by the delivery coordinator.
argument-hint: <absolute path to envelope.json>
arguments: [envelope]
disable-model-invocation: true
---

> Ports: `PORT` and `E2E_PORT` are already exported for this run. Run commands as plain
> `npm run …` / `npx …` without `VAR=value` prefixes (prefixed commands are denied).

# Procedure: resolve-conflicts

contract_id: `delivery.resolve-conflicts/v1`

Read `${CLAUDE_PLUGIN_ROOT}/references/stage-contract.md`, then the envelope at `$envelope`.

## Goal

The coordinator merged the latest base branch (`source.base_commit`) into this ticket's branch
and the merge stopped with conflicts. The working directory is that branch with the merge in
progress. Resolve the conflicts so the merge can be committed, keeping what both sides meant:
this ticket's change (the approved specification and plan in `approved_artefacts`) and the work
that reached the base branch since.

## Steps

1. `feedback_items` names the conflicted files. Open each one; the conflicts are marked
   `<<<<<<<`, `=======` and `>>>>>>>`. `git diff` and `git log --oneline HEAD..MERGE_HEAD` (when
   they work in your sandbox) show what arrived on the base branch.
2. Edit each conflicted file so it combines both sides' intent, and remove every conflict
   marker. Where the base branch renamed or reworked something this ticket uses, adapt this
   ticket's code to it rather than undoing the base branch's change.
3. Change nothing else: this session only completes the merge. Development of the ticket
   continues after it. Do not run `git add`, `git commit` or `git merge --abort`; the coordinator
   commits the merge.
4. Run the tests that cover the code you touched and record them in `worker_checks`.
5. If a conflict needs a product decision you cannot make from the specification, return
   `blocked` with the reason. The coordinator then abandons the merge (nothing else is lost) and
   flags the conflict for whoever merges the pull request.

## Result

`procedure`: `resolve-conflicts`. `artifacts`: the files you resolved, repository-relative, kind
`code` or `test`. `outcome`: `completed` when every conflict is resolved, otherwise `blocked`.
