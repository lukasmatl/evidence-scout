---
name: code-scout-recommend-collection
description: Uses a focused Code Scout operation to choose the smallest next repository evidence-collection action for a bounded unresolved claim. Use when an orchestrator asks what to inspect or run next before coding.
---

# Code Scout Recommend Collection

Recommend evidence collection, not a code change. Begin with the unresolved
claim, current evidence, target workspace, and the decision the evidence must
support.

When the workspace is accessible, this skill must execute one relevant Code
Scout operation before recommending the next action. For a receipt gap, a
read-only `change show`, `change session show`, or `change evidence` inspection
of the supplied identity satisfies that contract; do not require static or graph
access in this route. Otherwise, normally use a focused static query, or `graph
inspect` when index coverage is itself the unresolved question. Do not return a
Code Scout collection recommendation based on `rg` alone.

For static/graph collection, read `code-scout-cli`, prepare or refresh the index
when needed, and record the operation, scan status, warnings, and result. For a
receipt inspection, record the command, supplied IDs, scope, and its validation
or applicability result; it is read-only and does not authorize a gate run. If
the relevant Code Scout operation cannot run, state the exact limitation and
make the recommendation explicitly provisional.

## Choose the next action

Prefer the cheapest action that can discriminate between the remaining
possibilities:

1. Use the required Code Scout operation to establish conceptual starting
   points or index coverage.
2. Open an already cited file or nearby range.
3. Use `rg` for a known symbol, literal, import, route, caller name, or command.
4. Inspect tests, fixtures, configuration, manifests, CI, or task-runner files.
5. Run the narrowest safe project-defined validation command.
6. Use focused `fast` or `verify` only when direct inspection cannot resolve a
   bounded reasoning or coverage question.

A narrow validation command is diagnostic evidence only. It cannot repair or
upgrade a missing, incomplete, failed, invalidated, or stale receipt. When the
gap is a new execution receipt, recommend an explicitly authorized fresh
`change verify` or session verification that executes the entire frozen profile
for the selected identity; do not substitute ad hoc passing tests or splice
results from different candidates. Do not run gates merely to inspect a receipt,
and do not let a model answer override a recorded gate result.

A static match is a lead to inspect; a no-match is not proof of absence. Use
text output for a human and JSON only when the host requires structured data.

## Output

For each recommendation state:

- the unresolved claim or evidence dimension;
- why the current evidence is insufficient;
- one smallest next action, with an exact file, pattern, query, or command when
  known;
- what result would support, contradict, or narrow the claim;
- the Code Scout operation and result used to choose the recommendation;
- any permission, mutation, cost, or data-sharing boundary.

Do not recommend a repository-wide scan, broad model prompt, full test suite, or
larger query budget when a narrower check can answer the question. Label the gap
Unobserved when no evidence was collected and Missing only when scoped direct
inspection establishes absence. Do not add or invoke a
`code-scout recommend-collection` command.
