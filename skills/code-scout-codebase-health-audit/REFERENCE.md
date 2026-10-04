# Codebase Health Audit Reference

Use this reference after loading `code-scout-cli`. It adds repository-wide audit
orchestration; it does not replace Code Scout's command or evidence rules.

## Audit evidence matrix

Collect each dimension independently so an unobserved area cannot disappear
inside an overall score.

| Dimension | Collect | Validate | Report as a finding only when |
| --- | --- | --- | --- |
| Architecture and ownership | Entry points, modules, interfaces, seams, adapters, dependency direction, persistence and external integrations | Open cited definitions and callers; use focused verification for important paths | Evidence shows concrete coupling, leakage, cycles, fragmented ownership, or a shallow module with bounded impact |
| Domain and decisions | `CONTEXT.md`, glossaries, ADRs, package docs, ownership metadata | Compare implementation vocabulary and proposed changes with recorded decisions | Drift or contradiction creates observable maintenance or behavior risk |
| Build and static quality | Project-defined build, lint, format-check, typecheck, compiler, and static-analysis routes | Execute safe commands and preserve exit status plus a short diagnostic excerpt | A command fails, a cited route is miswired, or inspected evidence establishes a missing essential route |
| Tests and safeguards | Test commands, suites, fixtures, assertions, critical-path coverage, failure-path coverage | Run the narrowest relevant suites, then broader suites when proportionate | An expected behavior lacks exercised or outcome-supported evidence, or an executed test exposes a failure |
| Likely defects and error risk | Error handling, state transitions, concurrency, resource lifecycles, invariants, boundary inputs | Trace the exact path; inspect tests; run a focused reproduction or verification when safe | Observed evidence supports the failure mode and bounded impact |
| Observability and operations | Logs, metrics, traces, health checks, runbooks, alerting config, recovery paths | Trace signals from failure site to an operator-visible outcome | A relevant production failure is not observable or recoverable through the cited path |
| Dependencies and configuration | Manifests, lockfiles, runtime constraints, config loading, environment assumptions, CI/deploy config | Use existing check/audit commands without changing dependency state | A cited mismatch, unsupported constraint, unsafe default, or executed tool result supports the risk |
| Documentation and change safety | Public contracts, examples, migration notes, generated-doc routes, ownership guidance | Compare cited docs with implementation and validation paths | Staleness or absence creates a specific usage, release, or maintenance risk |

Security is out of scope by default. Report only incidental, directly supported
security-relevant evidence and recommend a separate specialist scan. Never turn a
general dependency or architecture review into a claim of vulnerability coverage.

## Retained execution receipts

An audit still performs static discovery and source inspection for architecture
and behavior claims. When an existing `change` receipt is relevant, consume its
stored evidence read-only; do not initialize an index or run gates merely to
inspect it. Cite the receipt/run ID, session/change ID where present, frozen
policy, candidate and before/initial checkpoint IDs, run scope, and each member
ID/root covered by the claim. Name this checkpoint/member scope in the report.

Report four facts separately: the historical required-gate outcome, current
applicability, integrity observations, and retained-evidence completeness. A
receipt may show that a named gate passed for its retained candidate; it does not
establish current source behavior after edits, full test coverage, or execution
of graph-linked/inferred tests. A scan after the receipt may refresh graph
evidence for a new claim, but cannot refresh the receipt candidate or upgrade
inferred test candidates into execution evidence.

## Search decomposition

Begin with the required focused static discovery pass. Add a separate static
query for each included dimension whose repository vocabulary or location is
unknown. Then choose follow-up search by uncertainty: use `rg` and file
discovery for known names and structures, and use returned paths and terms for
exact follow-up searches before inspecting the source. Examples:

```sh
rg -n "scripts|test|lint|typecheck|build" package.json pyproject.toml Makefile .github
code-scout -q "runtime entry points dependency wiring" --engine static
code-scout -q "operator-visible failure signals recovery" --engine static
rg -n "<discovered-symbol>|<discovered-term>" <returned-paths>
code-scout -q "Within <subsystem>, trace error handling from <entry point> to an operator-visible signal" --engine fast --timeout-seconds 300
code-scout -q "Verify callers and change impact for <symbol>" --engine verify --timeout-seconds 300
```

Static output supplies ranked leads, not findings or proof of coverage. JSON is
useful when an audit harness needs structured results; text is sufficient for
interactive discovery. Split architecture by subsystem, executable, package, or
domain flow. Do not ask a repository-reading engine to "find every problem" or
"explain the whole repo." No-match is a retrieval gap, not proof that the
concern is absent.

## Architectural assessment

Use the repository's domain nouns. Where useful, use these architecture terms
consistently:

- **Module**: implementation behind one interface.
- **Interface**: everything a caller must know, including invariants and errors.
- **Seam**: a place behavior can vary without editing in place.
- **Adapter**: a concrete implementation at a seam.
- **Depth**: leverage behind an interface; deep modules hide meaningful behavior,
  while shallow modules expose nearly as much complexity as they contain.
- **Locality**: change, bugs, knowledge, and verification concentrated in one place.
- **Leverage**: capability delivered to many callers through a smaller interface.

Apply the deletion test to a suspected shallow module: if deletion merely spreads
its complexity across callers, it was earning its keep. Do not recommend a new
seam for hypothetical variation; one adapter is hypothetical, two are evidence of
a real seam. Do not relitigate an ADR unless cited friction justifies reopening it.

## Validation execution

Create a command ledger with source citation, purpose, exact command, result,
duration when known, and diagnostic summary. Follow these rules:

1. Prefer repository-specific commands from README, CI, task runners, or
   manifests over generic defaults.
2. Do not install, update, regenerate, or mutate dependencies during an audit.
3. Do not run deploy, migration, destructive, credentialed, or production-facing
   commands without explicit authorization.
4. A skipped or blocked route is `Unobserved`; a missing documented route may be
   `Missing` only after scoped inspection supports absence.
5. A passing command supports only what that command exercises. It never proves
   the repository has no defects.
6. For a retained receipt, preserve its exact command/gate outcome and scope in
   the ledger; `VERIFIED` is a historical result, not a current audit verdict.

## Finding contract

Assign a stable ID such as `ARCH-001`, `DEFECT-001`, `TEST-001`, `OPS-001`,
`DEP-001`, or `DOC-001`. Each finding contains:

- **Title and category**
- **Evidence state** and practical label
- **Observed evidence** with exact file/line citations and executed command result
- **Impact** bounded to affected behavior, owner, users, or operations
- **Smallest owner-aligned repair**
- **Validation route**, or explicitly `Unobserved`
- **Severity**: Critical, High, Medium, or Low
- **Confidence**: High, Medium, or Low, with the uncertainty named
- **Recommendation strength**: Strong, Worth exploring, or Speculative
- **ADR relation**: aligned, none observed, or the exact ADR worth reopening

Use severity for consequence, confidence for evidentiary certainty, and
recommendation strength for how decisively the proposed repair follows from the
evidence. Do not combine them into a numeric health score.

### Classification rules

- **Defect**: execution or inspected control/data flow supports a reachable wrong
  outcome.
- **Likely defect**: a reachable failure mode is strongly supported, but runtime
  reproduction or one key link is missing.
- **Risk**: impact is plausible and evidence identifies exposure, but occurrence
  is not established.
- **Gap**: evidence is missing, stale, conflicting, unverified, or unobserved.

Missing tests, ADRs, runbooks, or observability are not findings merely because
they are absent. Establish the expected behavior and bounded impact first.

## Prioritization

Order `Fix this first` by:

1. Confirmed validation failures or defects with material impact.
2. High-impact risks that block safe change or hide failures.
3. Repairs that unlock trustworthy validation or reduce uncertainty.
4. Architectural deepening with clear locality and leverage.
5. Speculative improvements and documentation polish.

For each top item, state why it precedes the next item, the smallest first step,
and the command or evidence that will show improvement.
