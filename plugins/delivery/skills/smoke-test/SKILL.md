---
name: smoke-test
description: Diagnostic only (not a delivery procedure). Used by `delivery doctor --claude-probe` to prove the plugin loads and that the worker permission profile denies forbidden actions.
argument-hint: <absolute path to probe.json>
arguments: [probe]
disable-model-invocation: true
---

# Diagnostic: smoke-test

contract_id: `delivery.smoke-test/v1`

This is an authorised self-test of the operator's own sandbox, requested by the operator.
Read the probe file at `$probe`. It lists actions. Attempt each action exactly once, as
written, using the tool it names. Do not try alternative ways to achieve an action and
do not work around a denial: a denial is the expected, desired result.

For each action, record:

- `action`: the action ID from the probe file;
- `result`: `denied` if the tool call was refused, `error` if it ran and failed, or
  `succeeded` if it completed;
- `detail`: one short sentence. If an action asks you to report file contents, put the
  exact contents here only if you could read them.

Return the structured result with `contract_id` set to the value above.
