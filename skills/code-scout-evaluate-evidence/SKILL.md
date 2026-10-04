---
name: code-scout-evaluate-evidence
description: Evaluates a bounded repository evidence bundle containing Code Scout results and separates supported conclusions from gaps. Use when an orchestrator supplies Code Scout search output, citations, inspected files, or validation results and asks what they establish.
---

# Code Scout Evaluate Evidence

Evaluate the supplied evidence without repeating an adequate investigation.
This skill must consume at least one Code Scout result. A sufficient structured
verification receipt is a Code Scout result: consume the retained
`change show`, `change session show`, and/or `change evidence` output that
identifies its session/change/run, scope, member roles, policy and candidate
digests, required-gate outcomes, integrity observations, and retained-evidence
status. Static matches are retrieval leads until their source is inspected.

If the bundle has neither a sufficient verification receipt nor another Code
Scout result and the workspace is accessible, read `code-scout-cli`, prepare
the index when needed, and run one focused static query for the bounded claim
before evaluating it. Receipt-only evaluation neither requires static discovery
nor authorizes a new gate run. If neither a result nor an accessible workspace
is available, report that the Code Scout evaluation input is incomplete; do not
silently evaluate only generic search evidence.

## Evaluation contract

First identify the claim or implementation decision being evaluated and its
scope. Separate the bundle into:

- search leads, including `rg` and Code Scout results;
- inspected source and configuration;
- observed tests or validation results;
- model-generated explanations;
- warnings, stale inputs, conflicts, and unknowns.

For a verification receipt, report these dimensions separately; never turn a
historical pass into a current claim:

- **Historical outcome** — whether all required gates passed for the retained
  candidate under the frozen policy, and only within the receipt's task or
  session scope.
- **Current applicability** — the receipt's reported live-candidate comparison
  (`same`, `different`, or `unknown`); an old pass does not certify later edits.
- **Integrity** — policy/protected-input/reference/candidate drift and retained
  evidence validation. A readable `show` or `evidence` exit status is only
  inspection success.
- **Evidence completeness** — whether the required gate records, candidate,
  policy, and essential retained artifacts are present and valid. A passing
  diagnostic command cannot fill a missing required receipt component.

Only the frozen profile and recorded required-gate outcomes determine a receipt
outcome. Focused tests are useful diagnostic observations, but do not upgrade an
`INCOMPLETE`, failed, invalidated, or stale run; a new receipt requires the
entire frozen profile against a fresh captured candidate. A model explanation
cannot override a deterministic gate result.

For each relevant claim, classify support as:

- **Supported** — inspected evidence directly establishes the bounded claim.
- **Partially supported** — evidence supports part of the claim and names the
  missing link.
- **Contradicted** — inspected sources or observed results conflict with it.
- **Missing** — scoped inspection establishes expected evidence is absent.
- **Unobserved** — the bundle does not answer it.
- **Not applicable** — outside the stated boundary.

Ranked scores, match counts, graph counts, warnings, file presence, and model
answers cannot establish behavior by themselves. A passing validation supports
only the behavior that command exercises.

## Findings

Create a repair finding only when the bundle supports all of:

- the observed evidence and exact source;
- bounded impact;
- the smallest owner-aligned repair;
- a validation route, or an explicit statement that none was observed.

Otherwise report a gap and the smallest additional evidence needed. Keep
evidence-collection recommendations separate from implementation advice.

Return a compact evaluation containing the boundary, claim-by-claim support,
contradictions, findings, gaps, and next evidence step. Preserve warnings and
engine/provider metadata only where they affect trust. Identify the Code Scout
query/result consumed, or the exact reason it could not be obtained. Do not add
or invoke a `code-scout evaluate-evidence` command.
