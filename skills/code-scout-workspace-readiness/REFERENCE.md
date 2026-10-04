# Workspace Readiness Reference

Read this reference when exact command behavior, output interpretation, or a
bounded routing example is needed.

## Commands and workspace selection

Supported invocations are:

```sh
code-scout status --workspace . --format text
code-scout status --workspace . --format json
code-scout doctor --workspace . --format text
code-scout doctor --workspace . --format json
```

`--format` defaults to `text`. `--workspace PATH` takes precedence; otherwise
the command uses `CODE_SCOUT_WORKSPACE`, then the current directory. Both
commands are local and read-only. They do not initialize or scan the workspace,
install dependencies, repair configuration, run tests or models, or probe an
endpoint.

## Separate state lanes

`status` and `doctor` expose implemented graph and provider diagnostics only;
they do not inspect `.code-scout/verification/` or synthesize gate health.

- **Graph index state** comes from `status`: `index`, `freshness`, `changes`,
  and `coverage` describe the scanned workspace evidence, not a retained
  checkpoint or a gate execution.
- **Model-provider availability** comes from `doctor` checks such as
  `provider.fast` and `provider.verify`. It is configuration-only and never an
  endpoint probe, graph-freshness result, or prerequisite for deterministic
  `change verify`.
- **Verification receipt/applicability** comes from read-only `change show`,
  `change session show`, or `change evidence`. It reports a historical
  candidate under a frozen policy and, separately, that receipt's current
  applicability. It is not a `status` field or a `doctor` check.

Use the existing emitted index recommendations only to remedy graph evidence.
An index scan after a receipt may make later graph evidence current, but cannot
alter that receipt's candidate checkpoint, refresh applicability, or turn graph
test candidates into execution evidence.

## Status interpretation

JSON status contains `schema_version`, `workspace`, `index`, `freshness`,
`changes`, `coverage`, and `recommendations`.

- `index.state: ready` means the database is readable and schema-compatible;
  it does not establish freshness or coverage.
- `freshness.state: fresh` means the current in-scope readable file bytes match
  hashes in the resolved `files` table and the comparison coverage is complete.
- `freshness.state: stale` means at least one in-scope path was added, modified,
  or deleted. Use the repository-relative lists in `changes`.
- `freshness.state: unknown` means the comparison cannot establish freshness,
  such as after a partial scan, with missing indexed hashes, unreadable files,
  or an unavailable index. Name the `coverage_reasons`; do not call it fresh or
  stale by inference.
- `index.state: missing` means workspace configuration is absent. Report the
  emitted `init` recommendation. `unscanned`, `unreadable`, and `incompatible`
  similarly retain unknown freshness and emit a scan recommendation.
- `coverage.scan_scope.state: partial` means the recorded scan selected one
  extractor or profile rather than all enabled extractors. Report the selector
  and `selected_extractors`; enabled configuration or zero facts is not proof
  that another extractor ran.
- `coverage.gaps` supplies evidence-specific remediation. An observed file count
  does not imply symbol, relationship, caller, or test coverage.

Status exit codes are `0` when a report is produced, including missing, stale,
and unknown states, and `1` for an operational or configuration failure such as
an unsupported format. The shared status adapter does not currently use exit
code `2`.

## Doctor interpretation

JSON doctor contains `schema_version`, `workspace`, `summary`, and `checks`.
Each check has `id`, `severity`, `reason`, and `remediation`. Checks cover
workspace and index readiness, configured extractor executables, Tree-sitter
grammars, optional Serena and DEDE integrations, global routing, and fast/verify
providers.

An `ok` provider check means configuration and requested local dependencies are
available; it does not mean the endpoint is reachable. An absent global model
configuration produces a warning plus unknown fast/verify checks and explicitly
leaves static search usable. A configured provider's missing secret, package,
or executable is an error for that provider, not evidence that static search
failed.

Doctor exits `0` when no check has `error` severity, even if warnings or unknowns
remain; `1` when any check is an error; and `2` for invalid command usage such
as an unsupported format or missing option value.

## Bounded scenario evaluations

These cases record routing expectations, not claims that the commands were run.

### Fresh full scan

Prompt: “Is this Code Scout index current and does it contain relationship
evidence?”

Run status first. If `index.state` is `ready`, freshness is `fresh`, scan scope
is `full`, and relationship extraction is `observed`, report those bounded
facts. Do not run doctor unless another finding or the user asks for setup
diagnosis.

### Stale workspace

Prompt: “Can I trust the indexed locations after these edits?”

Run status first. Report `stale`, enumerate the relevant changed paths, and
recommend the emitted scan command. Do not claim the scan was performed. Run
doctor only if status also reveals an unresolved local capability problem.

### Missing index

Prompt: “Why is there no Code Scout evidence in this workspace?”

Run status first. For `index.state: missing`, report unknown freshness and the
emitted initialization recommendation. Use doctor to distinguish invalid or
missing workspace configuration from other setup problems. Do not initialize or
scan unless that mutation is within the user's authorized scope.

### Missing optional provider

Prompt: “Static search works, but why is the verify engine unavailable?”

Reuse a current status result if available, then run doctor. Report the exact
provider check and remediation. Preserve that provider checks are
configuration-only and that a model configuration or optional-dependency error
does not establish failure of the static engine. Do not claim an endpoint probe
or installation occurred.

### Old verification receipt

Prompt: “Does this green receipt still apply after today's edits?”

Do not run `status` or `doctor` as a substitute for receipt inspection. Route
to read-only `change show`, `change session show`, or `change evidence` and
report the receipt ID, historical outcome, checkpoint/member scope, integrity
and completeness, plus its reported current applicability. A fresh graph scan
cannot answer this question or update the receipt.

### Should not trigger

Prompt: “Audit the whole repository architecture and prioritize technical debt.”

Do not use this skill. Route to the codebase-health audit workflow; status and
doctor do not perform an architecture audit or automatic repair.
