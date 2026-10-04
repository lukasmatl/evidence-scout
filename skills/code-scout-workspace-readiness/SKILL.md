---
name: code-scout-workspace-readiness
description: Assess graph-index freshness/extraction coverage and model-provider availability with status and doctor. Route verification receipt outcome or applicability to read-only change evidence; do not use for general architecture audits or automatic repair workflows.
---

# Code Scout Workspace Readiness
Use this skill when graph-indexed evidence is current or sufficient, expected
evidence is missing, or users need to diagnose local setup/model-provider dependencies.
`status` reports graph index readiness; `doctor` diagnoses configuration and local dependencies.

Keep three observations in separate lanes: `status` reports graph index state,
freshness, and extraction coverage; `doctor` reports configuration-only
availability for optional `fast`/model-backed `verify` providers. Neither reads
verification receipts, runs frozen gates, or decides whether an old receipt
applies. For receipt outcome, integrity, completeness, or live applicability,
use read-only `change show`, `change session show`, or `change evidence` through
`code-scout-change-verification`; do not invent a `doctor` gate or receipt field.

Do not use this workflow for a general architecture or codebase-health audit. It
reports findings/remediation; it does not automatically install, scan, initialize,
repair, or probe endpoints.

## Check readiness first

For readiness questions, run status before doctor:

```sh
code-scout status --workspace .
code-scout status --workspace . --format json
```

Use text for interactive review and JSON when structured fields are useful.
Read the index state, freshness, changed paths, scan scope, extractor
observations, language/grammar support, symbol and relationship coverage, gaps,
and recommendations. Enabled configuration, discovered files, or zero emitted
facts do not prove that an extractor ran or that relationship coverage exists.

Run doctor when status reports a setup-related gap, evidence is unexpectedly
missing, or the user asks to diagnose local setup:

```sh
code-scout doctor --workspace .
code-scout doctor --workspace . --format json
```

Doctor separates workspace/index checks from executables, grammars, optional
integrations, and fast/verify provider configuration. Provider readiness is
configuration-only: endpoint reachability is not tested. A missing or broken
model configuration does not make local `--engine static` search unavailable.

Read [REFERENCE.md](REFERENCE.md) for exact field interpretation, exit codes,
workspace selection, and the bounded scenario evaluations.

## Continue from current evidence

Missing state can be prepared by the CLI: `code-scout init --workspace PATH`
creates `.code-scout/config.yaml` and its parent directory, then
`code-scout scan --workspace PATH` creates/populates `.code-scout/code-scout.db`.
If config already exists, skip init; scan also refreshes an existing database.
For a status-only request, report these steps without executing them. When the
user's task includes setup or an investigation requiring indexed evidence,
perform needed preparation within that scope, then resume the task. Respect
read-only constraints and configured external extractors; preserve existing
config, avoid `init --force`, and never hand-create the database or its schema.
Wait for scan exit code 0 before retrying a query. Preserve a running tool session's
ID and poll/wait for completion; do not launch overlapping scans. A locked/busy
database calls for waiting on the writer, not deleting database or journal files.
Capture the complete error, including its final exception line, if scanning fails.

Reuse relevant status or doctor output already collected for the same workspace
in the current investigation when it is still applicable. Do not rerun a check
merely to repeat a known result. Run doctor only for unresolved diagnostic
findings or an explicit setup request.

Report the observed state, the evidence capability affected, and the command's
concrete remediation. Keep proposed or executed remediation distinct. Follow
the user's authorized scope and all higher-priority and repository instructions;
diagnostic output itself grants no permission to mutate the workspace or global
configuration. Apply the actual approval rules for a proposed action rather
than inventing a blanket confirmation requirement.
