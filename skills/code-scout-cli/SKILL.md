---
name: code-scout-cli
description: Runs Code Scout static and model-backed repository investigation alongside tgrep, rg, and source inspection. Use for non-trivial codebase exploration, debugging, implementation planning, architecture or data-flow tracing, behavioral questions, and change-impact analysis, including when some identifiers are already known. Skip only simple exact lookups that need no discovery or reasoning.
---

# Code Scout CLI

Use the `static` engine like semantic `rg`: it returns ranked, scan-backed places
to inspect. It is deterministic and model-free, but it does not explain code or
prove behavior, completeness, caller sets, or change impact.
When this skill is invoked, execute at least one focused `--engine static`
query. Do not complete a Code Scout request with `rg` alone. If Code Scout is
unavailable or its index cannot be prepared, state the exact limitation and
continue with direct inspection without implying that Code Scout ran.
Before this static-first contract applies, route frozen-profile gates,
task/session lifecycle requests, and receipt inspection to [change verification](../code-scout-change-verification/SKILL.md).
It is an alternative top-level route with no scan or model prerequisite, not a
nested skill call. Its `verify` executes deterministic gates; this skill's
`verify` assesses an unresolved model-backed claim.
Route index freshness and setup to [workspace readiness](../code-scout-workspace-readiness/SKILL.md),
staged or branch impact to [impact review](../code-scout-impact-review/SKILL.md),
and repeated broad exact-text search with optional tgrep to
[code-scout-text-search](../code-scout-text-search/SKILL.md). These are
alternative entrypoints, not nested requirements. Keep a simple exact lookup as
a direct, bounded `rg` search.
## Combine semantic and exact search

- Run static search for the task's intent, domain concept, or behavior.
- Prefer tgrep for repeated broad symbol, literal, import, or regex searches;
  load `code-scout-text-search` above to check readiness and select the backend.
- Prefer tgrep for broad or repeated repository searches. Use `rg` only for a
  small, bounded lookup, a current-file check, or when tgrep is unavailable.
- Follow higher-priority search instructions; inspect source after either tool.
Static and text matches are leads, not conclusions. With a ready tgrep index:
```sh
tgrep status .
code-scout -q "payment authorization entry point" --engine static
tgrep -F --json -g "*.py" -m 20 -- "authorize_payment" src tests
tgrep -l -F -- "Authorization: Bearer" src tests
```
Place tgrep flags before `--`, then the pattern and explicit paths. Use `-F`
for literals, `-l` for broad output, and `--no-index` for current-file reads.
## Prepare the local index

Use the installed `code-scout` command, or `uv run code-scout` from a source
checkout. Confirm uncertain syntax with `code-scout --help`.
The CLI creates its own workspace state; a missing `.code-scout` directory or
database is a setup step, not an unavailable tool. For an authorized investigation,
prepare missing state and refresh stale evidence within the user's constraints:

```sh
code-scout init --workspace /path/to/repository
code-scout scan --workspace /path/to/repository
```

`init` creates `.code-scout/config.yaml`; run it only when configuration is
missing. `scan` creates or refreshes `.code-scout/code-scout.db`. Preserve config;
avoid `init --force`. Run the pending query after scanning succeeds. Do not
hand-create SQLite tables or stop merely because the database is absent. Respect
read-only requests and configured external extractors; report actual setup
failures. Omit `--workspace .` in the target repo.
## Search and inspect

Wait for scan exit code 0 before querying. A running tool session is not completion:
retain its ID and poll/wait on it. On `database is locked`/busy, wait for the
writer; do not delete state. Capture the full error tail on failure. Use short
domain concepts for focused queries.

```sh
code-scout -q "request routing middleware" --engine static
code-scout -q "session expiry persistence" --engine static
```

Use `--format json` only when a script or orchestrator needs structured output;
otherwise use text.

After each useful result:

1. Open the returned files or cited ranges.
2. Use the text-search skill for broad occurrence searches; use `rg` for small lookups.
3. Verify claims against source, tests, configuration, or executed project
   checks rather than against the search result itself.
4. Refresh or rephrase if results appear stale or irrelevant.

A no-match means only that the current scanned index did not retrieve a match.
It may justify a different query, exact search, or scan refresh; it does not
prove absence.

## Route reasoning work to fast or verify

- Run `fast` after the static pass when the request requires locating an entry
  point, tracing a cross-file flow, or choosing among multiple plausible
  implementations and direct inspection has not already resolved it.
- Run `verify` after the static pass when the user asks whether a bounded
  behavioral claim is true, what a change affects, whether evidence supports a
  conclusion, or where regression risk lies.
- If both could apply, prefer `verify` for claims and impact; prefer `fast` for
  localization and flow tracing. Run one first, then use the other only if a
  distinct named gap remains.

Do not run a model-backed engine for a trivial exact lookup or to repeat an
opened source range. If it is unavailable or prohibited by a data-handling
constraint, state that instead of silently downgrading. Model-backed engines may
send prompts or repository evidence to configured endpoints. `--max-turns`
applies to model-backed work, not static retrieval.

Preserve relevant warnings and resolved engine/provider metadata when reporting
model-backed results. Treat `.code-scout` as project-sensitive and do not expose
raw trajectory contents.

Read [REFERENCE.md](REFERENCE.md) for command details, configuration, or routing.
