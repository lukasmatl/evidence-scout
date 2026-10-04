---
name: code-scout-text-search
description: Prefer Microsoft tgrep for repeated broad literal or regex searches in large workspaces, including known identifiers, configuration keys, and scripts. Check availability and index readiness; use rg for small lookups or fallback. Do not use for conceptual architecture questions.
---

# Code Scout Text Search Companion

Use this skill for repeated broad exact searches or repository-wide occurrence
lookup for a known identifier, configuration key, or script during an investigation.
Prefer tgrep when available and ready; check it before defaulting to broad `rg`
searches. This skill supplies the routing policy: installation alone does not change agent routing.

Do not invoke this skill for conceptual architecture or behavior questions, or
for a small one-off lookup where direct search is already cheap. Follow all
higher-priority and repository instructions. If they require `rg` first, use
`rg` first. Where those instructions allow, use tgrep for subsequent broad searches
rather than duplicating every query with both tools.

## Keep each evidence source in its lane

- tgrep retrieves literal or regex text occurrences from its indexed search
  scope. Every hit is a candidate for inspection, not a confirmed call,
  dependency, owner, execution path, or complete caller set.
- Code Scout `--engine static` retrieves ranked, scan-backed starting points for
  concepts. Static matches and scores are also leads rather than proof.
- Graph evidence can establish a recorded relationship, while inspection of the
  cited and current source establishes what the code actually does. Use both as
  appropriate before making relationship or behavioral claims.

Track tgrep availability, server/index status, scope, and completeness separately
from Code Scout index freshness and extraction coverage. A fresh Code Scout scan
says nothing about tgrep, and a complete tgrep index says nothing about Code
Scout graph evidence.

## Check before indexed search

Check once per workspace investigation; reuse the result until relevant state changes:

```sh
command -v tgrep
tgrep --version
tgrep status .
```

Confirm that the executable exists and the status refers to the intended root,
then read its file count, watcher state, and `Indexing` state. Do not treat a
running server, nonzero file count, or partial/background indexing as complete.
Do not build an index or start `tgrep serve` unless that state change is within
the user's scope and allowed by the environment. A missing index is preparable:
when indexing is within scope and worthwhile for repeated queries, run `tgrep index .`
and wait for completion, then search. Otherwise state the reason for using `rg`.
This skill does not require tgrep for Code Scout or manage its daemon lifecycle.

## Search narrowly and inspect

Prefer fixed strings for literals and regex only when needed. Bound every query
with exact paths plus suitable globs or file types and a per-file match cap. Use
JSON Lines output for structured consumption:

```sh
tgrep -F "authorize_payment" src tests -g "*.py" -m 50 --json
tgrep "CODE_SCOUT_[A-Z_]+" src scripts -t py -m 20 --json
```

Do not repeat successful tgrep queries with `rg` by habit. Inspect decisive hits in
current source and use graph evidence when the claim is
about calls or dependencies. Before using an indexed no-match as absence
evidence, check index completeness and run an appropriate direct search over the
current bounded source, normally:

```sh
rg -n -F "authorize_payment" src tests -g "*.py"
rg -n "CODE_SCOUT_[A-Z_]+" src scripts -g "*.py"
```

Fall back to `rg` when tgrep is absent, unreachable, incompletely indexed,
stale or uncertain for the relevant paths, incompatible with the query, or not
worth its index overhead. State the limitation rather than implying tgrep ran.

Read [REFERENCE.md](REFERENCE.md) for official syntax sources, status and
no-match interpretation, validation notes, non-goals, and bounded scenarios.
