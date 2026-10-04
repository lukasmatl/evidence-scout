# Code Scout CLI Reference

Read this reference when command syntax, static-result interpretation, index
freshness, configuration, or model-engine routing matters.

## Command catalog

| Goal | Command |
| --- | --- |
| Initialize workspace state | `code-scout init --workspace PATH` |
| Refresh the local index | `code-scout scan --workspace PATH` |
| Run one extractor | `code-scout scan --workspace PATH --extractor NAME` |
| Inspect graph coverage | `code-scout graph inspect --workspace PATH` |
| Inspect graph coverage as JSON | `code-scout graph inspect --workspace PATH --format json` |
| Run semantic/lexical retrieval | `code-scout -q "CONCEPT TERMS" --workspace PATH --engine static` |
| Return structured retrieval results | add `--format json` |
| Ask a model-backed engine | use `--engine fast` or `--engine verify` |
| Override a model query budget | add `--max-turns COUNT` (1-100) |
| Bound query duration | add `--timeout-seconds SECONDS` |
| Preview incremental work | `code-scout watch --workspace PATH --dry-run` |
| Watch changed files | `code-scout watch --workspace PATH --changed-only` |
| Review watch events | `code-scout changes --workspace PATH` |
| Validate global routing | `code-scout config validate` |

`CODE_SCOUT_WORKSPACE` can provide the default workspace. `CODE_SCOUT_HOME` can
relocate the global configuration directory.

## Workflow routing

Select one primary entrypoint from the user's request:

| Request | Primary skill | Why |
| --- | --- | --- |
| Index freshness, extraction coverage, missing evidence, or local setup | [Workspace readiness](../code-scout-workspace-readiness/SKILL.md) | Owns the read-only `status`-first and conditional `doctor` workflow. |
| Staged or branch impact, affected callers/contracts/services, or test selection | [Impact review](../code-scout-impact-review/SKILL.md) | Owns exact Git comparison selection and cited `impact` interpretation. |
| Repeated broad literal/regex lookup with optional tgrep | [Text search](../code-scout-text-search/SKILL.md) | Owns tgrep state checks, bounded occurrence search, and `rg` fallback. |
| Run a declared frozen profile, manage a verification task/session, or inspect an existing receipt | [Change verification](../code-scout-change-verification/SKILL.md) | Owns deterministic `change` execution and read-only receipt inspection; it needs no scan, graph, or model. |
| Non-trivial conceptual discovery, flow tracing, or bounded claim verification | `code-scout-cli` | Owns static-first retrieval and conditional `fast`/`verify` escalation. |
| One cheap exact symbol, literal, filename, or import lookup | No skill; use bounded `rg` | Avoids index and workflow overhead that adds no evidence. |

The specialized skills and `code-scout-cli` are alternative top-level routes.
They may reuse already-collected results, but none must invoke another skill.
Select change verification before this workflow when the user means executable
gates or a receipt: `change verify` is deterministic execution, while
`--engine verify` is model-backed claim analysis. A receipt-only request uses
the dedicated skill's read-only inspection commands and does not rerun gates.
Staged/base impact remains the impact-review route. An invoked
`code-scout-cli` workflow still performs a focused static query.

## What static search does

The static engine queries `.code-scout/code-scout.db` without an LLM. It ranks a
hybrid of semantic embedding matches and weighted lexical matches from scanned
files, symbols, services, contracts, concepts, decisions, and graph context.
Results are limited ranked candidates, not an exhaustive grep or a generated
answer.

An invoked Code Scout workflow must execute or consume a Code Scout operation;
it must not silently become an `rg`-only workflow. For `code-scout-cli`, evidence
docs, preflight, and health audit, run at least one focused static query. Evidence
evaluation may consume an existing Code Scout result. Collection recommendation
may use static retrieval or graph inspection when coverage is the question.

Use static search for conceptual discovery such as:

```sh
code-scout -q "outbound quote submission" --engine static
code-scout -q "authentication token refresh" --engine static
```

Use the [text-search skill](../code-scout-text-search/SKILL.md) to prefer tgrep
for repeated broad exact-token or pattern searches, while retaining the narrow
Code Scout pass required by the invoked workflow. With a ready index:

```sh
tgrep status .
tgrep --json -F -g "*.py" -m 20 -- "submit_quote" src tests
tgrep -l -F -- "Authorization: Bearer" src tests
tgrep -C 2 -- "CODE_SCOUT_[A-Z_]+" src scripts
```

Use `rg` for small lookups, decisive current-file checks, or fallback when tgrep
is unavailable or unsuitable. Follow higher-priority search instructions. Put
tgrep flags before `--`, then pass the pattern and explicit search paths; use
`--no-index` deliberately when freshness matters more than indexed speed.

Static search is especially useful before exact search: it discovers the
project's vocabulary and likely paths. Exact search then establishes concrete
definitions and references.

## Reading results

Text output is optimized for interactive review. JSON includes ranked starting
points, evidence, citations, warnings, `needs_review`, and engine/provider
metadata for automation.

Interpret result fields conservatively:

- A high score means the indexed unit ranked well for this query, not that a
  claim is true.
- A citation identifies the source of an indexed match, not proof of all
  surrounding behavior.
- A file-only match is a location hint and may not identify a relevant line.
- A no-match describes this query against this scan; it does not prove absence.
- Counts and graph coverage describe the index, not repository quality.

Open the source before asserting behavior or editing. Use tests, configuration,
call sites, and executed validation for stronger claims.

## Index freshness

Run `scan` when:

- the database is missing;
- relevant files changed after the last scan;
- configuration or extractors changed;
- the user requests current scan-backed results; or
- results conflict with files on disk.

Do not rescan reflexively for every query when the index is known to be current.
Use `graph inspect` when extractor coverage or graph health is itself relevant,
not as a prerequisite for ordinary search.

## Model-backed engines

`fast` is intended for focused repository localization or tracing through its
configured CodeScout provider. `verify` answers bounded questions from a frozen
scan-backed evidence pack. Their quality depends on configuration, language
coverage, and question scope.

After static retrieval, use `fast` for unresolved localization and cross-file
flow tracing. Use `verify` for bounded behavioral claims, evidence assessment,
change impact, and regression risk. A non-trivial request in one of those
categories should route to the corresponding model-backed engine unless opened
source already answers it or the configured endpoint/data-handling boundary
prevents the call. Include a subsystem, path, entry point, symbol, or language
when known. Avoid repository-wide prompts such as “explain everything.”

`--max-turns COUNT` overrides the configured request budget for model-backed
engines. `--timeout-seconds` overrides the configured timeout. Do not add either
flag to static queries unless an execution environment specifically requires a
timeout bound.

Fallback is opt-in. Results report the resolved engine/provider and warnings
when fallback occurs; never assume a failed engine silently escalated.

## Configuration and privacy

- `~/.code-scout/config.toml`: global model routing, endpoints, budgets, and
  environment-variable references for credentials.
- `.code-scout/config.yaml`: workspace repositories, extractors, navigation,
  enrichment, and output settings.
- `.code-scout/code-scout.db`: local scan-backed architecture index.
- `.code-scout/trajectories/`: provider activity that can contain sensitive
  project-derived material.

Code Scout does not start model servers or download weights. Model-backed
endpoints may receive prompts or evidence. Do not expose credentials or raw
trajectory content.
