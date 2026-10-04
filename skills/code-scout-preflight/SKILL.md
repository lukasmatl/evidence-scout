---
name: code-scout-preflight
description: Runs a Code Scout static search plus source inspection to assess whether a bounded implementation task is ready to begin. Use when the user or orchestrator explicitly asks for Code Scout preflight, implementation readiness, or a go/no-go evidence report.
---

# Code Scout Preflight

Preflight is a skill workflow, not a Code Scout CLI command. Keep it separate
from ordinary repository search.

## Establish the boundary

Record the requested change, target workspace, expected outcome, included and
excluded scope, unresolved decisions, and relevant data-handling constraints.
When supplied, also record session, change/attempt, and run identities; explicit
member IDs, roots, and editable/reference roles; the frozen policy/profile and
candidate/checkpoint identities; and protected-input constraints. Treat these as
bounded provenance and integrity context, not as permission to infer missing IDs
or begin a session. Dirty state, uncommitted work, and overlapping sequential
edits are normal observations, not readiness failures. Do not assess
repository-wide readiness for a bounded task.

## Run the Code Scout discovery pass

An explicit invocation of this skill requires at least one task-focused
`--engine static` query. Do not satisfy Code Scout preflight with `rg` alone.

Read `code-scout-cli`, then ensure the local index is usable. Run `init` when
workspace metadata is absent and `scan` when the index is absent or stale. Do
not use `init --force`. If Code Scout cannot run because the command is missing,
initialization or scanning fails, or a data-handling constraint prohibits it,
continue with direct inspection but state the exact limitation in the report.

Form the static query from the implementation goal and its domain concepts, not
from an already-known identifier alone:

```sh
code-scout -q "<task behavior, boundary, and domain terms>" --engine static
```

Use text output for an agent-readable pass and JSON only when the host requires
a machine-readable bundle. Record the query, scan status, warnings, and useful
starting points or no-match. Open at least one relevant returned file or cited
range before using it in the assessment. Static output remains a list of leads,
not readiness evidence by itself.

## Complete the evidence with direct inspection

After the required static pass, use the cheapest source for each remaining
unknown:

- `rg` for known identifiers, strings, imports, routes, and command names;
- another focused static query when vocabulary or location remains unknown;
- direct inspection for behavior, ownership, configuration, and tests;
- `graph inspect` only when index/extractor coverage affects the assessment;
- focused `fast` or `verify` only when direct inspection leaves a reasoning gap.

Collect evidence for the dimensions material to this change:

- target and owner located;
- relevant wiring, callers, routes, contracts, or configuration inspected;
- expected behavior and important failure paths identified;
- tests, fixtures, or examples located;
- project-defined validation commands located in repository metadata;
- unresolved product, architecture, permission, or environment decisions named.

Do not invent commands or install dependencies. A missing search result is
Unobserved unless direct inspection establishes absence.

Preflight does not open, resume, rebaseline, verify, finish, or otherwise
mutate a verification session. It does not claim that supplied gates passed, and
it does not treat a model explanation as an override of a recorded gate outcome.
For implementation questions, keep static discovery and source inspection even
when receipt identity is supplied: receipt evidence describes a historical
candidate, not the current code behavior.

## Assess readiness

Classify each relevant dimension:

- **Supported** — inspected source or executed validation supports it.
- **Partially supported** — useful evidence exists but a named link is missing.
- **Missing** — scoped inspection establishes expected evidence is absent.
- **Unobserved** — it has not been checked or could not be checked.
- **Not applicable** — outside the task boundary.

Return:

1. **Boundary** — change, outcome, scope, unresolved decisions, supplied
   identity/member-role context, and protected-input constraints.
2. **Code Scout discovery** — scan status, static query, warnings, and the leads
   inspected, or the exact reason Code Scout could not run.
3. **Evidence** — inspected file ranges and executed command results.
4. **Readiness** — Ready, Ready with named risks, or Not ready.
5. **Gaps** — the exact unsupported or unobserved links.
6. **Next step** — the smallest collection, decision, or validation action that
   would reduce the blocking uncertainty.

Reserve Not ready for a gap that materially prevents a safe implementation
decision. Never derive readiness from match counts, graph counts, warnings, or a
passing build alone. Do not add or invoke a `code-scout preflight` command.
