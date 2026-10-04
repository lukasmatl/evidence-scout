# Code Scout Text Search Companion Reference

Read this reference when exact tgrep syntax, index-state interpretation, or a
bounded routing example is needed.

## Syntax and validation record

Microsoft documents tgrep as a trigram-indexed regex search tool with an optional
client/server workflow. The official
[microsoft/tgrep README](https://github.com/microsoft/tgrep/blob/main/README.md)
documents `tgrep index`, `tgrep serve`, ordinary pattern search, `-F`, `-g`,
`-t`, `-m`, `--json`, `--no-index`, and `tgrep status .`. `--json` emits one
object per line. Exit status `0` means matches, `1` means no matches, and `2`
means an error.

Validation on 2026-09-08 found optional tgrep 1.0.4 at
`/opt/homebrew/bin/tgrep`. `tgrep --help`, `tgrep search --help`, and
`tgrep status --help` confirmed the documented fixed-string, regex, path, glob,
file-type, per-file maximum, JSON, direct-search, and status syntax. A bounded
JSON query was validated without creating an index:

```sh
tgrep -F "Code Scout" skills/code-scout-text-search -g "*.md" -m 5 --json --no-index
```

It returned JSON Lines `begin`, `match`, `end`, and `summary` records.

`tgrep status .` reported no index for this workspace. Therefore an indexed
query and ready-index/server result were unavailable optional checks. No index
or server was created merely to validate this skill. Recheck help and status in
the actual environment because another installed version or workspace may
differ.

## Availability and index-state interpretation

Check the executable and version before relying on locally installed syntax.
Run `tgrep status ROOT` for the exact intended workspace. A useful indexed-search
record includes the resolved root, file count, watcher state, and whether
indexing is complete. An active watcher describes tgrep updates only; it is not
Code Scout freshness evidence.

Treat all of the following as unavailable or incomplete for indexed absence
claims: missing executable, connection/status error, missing index, indexing in
progress, wrong root, excluded or unsupported relevant files, uncertain watcher
coverage, or changes not known to be incorporated. Use `rg` over the exact
current paths for the decisive absence check. `tgrep --no-index` is a direct
scan option supported by tgrep, but it provides no index advantage and does not
override an instruction to prefer `rg`.

Keep searches bounded by named roots/files and suitable `-g` or `-t` filters.
`-m` limits matches per file, not the total result set, so narrow the path scope
before parsing JSON Lines. Inspect returned file and line locations directly.

## Evidence composition

Use tgrep or `rg` to answer “where does this text occur?” Use Code Scout static
to find scan-backed conceptual starting points. Use stored graph edges and their
citations to propose a relationship, then inspect current source to confirm the
claim and to account for stale or incomplete evidence.

A textual occurrence can be a declaration, import, comment, string, fixture,
documentation example, generated file, or dead code. Never label it a caller or
dependency based only on text. Likewise, no text hit proves that a relationship
does not exist through aliases, dynamic dispatch, generation, or an unsearched
scope.

## Live, indexed, and retained bytes

`rg` reads the named live paths at query time. Indexed tgrep reads its own
indexed text scope until a direct `--no-index` search is selected. Code Scout
static and graph evidence describe the content retained by the relevant scan.
Track the freshness of each independently; neither a tgrep update nor a Code
Scout scan establishes that the other reflects current files.

Verification checkpoint blobs are a separate retained-byte source. They describe
the exact historical candidate identified by a receipt's checkpoint/member
scope, even if live files later change or disappear. Do not ask `rg`, tgrep, or
a refreshed graph index to reconstruct those bytes. A scan after a receipt can
support a new live/indexed claim only; it cannot change the receipt candidate,
refresh receipt applicability, or convert a textual/graph inferred test into a
test-execution record. Use validated `change show`/`change evidence` artifacts
for receipt scope and gate execution claims.

## Non-goals and authorization boundary

This skill adds no Code Scout runtime search backend, graph facts or writes,
status/doctor integration, tgrep dependency, or daemon lifecycle management.
Installing tgrep, starting its server, and building or rebuilding its index are
separate mutations. Perform them only when they follow the user's existing scope
and the environment's authorization and resource constraints.

## Bounded scenario evaluations

These cases record expected routing and evidence interpretation. Only the
validation record above describes checks performed for this story.

### Ready indexing

Prompt: “Search all service packages repeatedly for `authorize_payment`.”

When tgrep is installed and `tgrep status` identifies the intended root with
complete indexing for the relevant paths, use a bounded fixed-string JSON query.
Inspect the candidates. Use Code Scout graph evidence plus source inspection if
the user asks which occurrences are callers or dependencies.

### Missing executable

Prompt: “Use the optional fast text index to find `CODE_SCOUT_HOME`.”

If `command -v tgrep` fails, state that the optional executable is unavailable
and run a bounded `rg` search. Do not install tgrep or imply that Code Scout is
unavailable.

### Incomplete indexing

Prompt: “The tgrep server is still indexing; prove `legacy_route` is absent.”

Do not use the indexed no-match as absence evidence. Search the decisive current
scope directly with `rg`, report any remaining exclusions or unreadable paths,
and keep tgrep state separate from Code Scout freshness.

### Textual false-positive caller

Prompt: “tgrep found `authorize_payment` in a comment and a fixture. Are both
callers?”

No. Classify them only as textual candidates, inspect the source, and require a
cited graph relationship or direct call-site evidence before naming a caller.

### rg-first instruction precedence

Prompt: “This repository says all text search starts with rg; find every use of
the deployment script name across a very large indexed workspace.”

Run the required bounded `rg` search first. Where governing instructions allow,
prefer tgrep for subsequent broad searches once its index is ready; do not
duplicate each query with both tools. Installation alone does not supersede
the repository instruction or authorize index/server setup.

### Historical checkpoint question

Prompt: “Find the old helper content that this receipt verified.”

Do not search the live tree or use a fresh tgrep/Code Scout index as historical
evidence. Inspect the retained receipt/checkpoint artifacts, cite their member
and checkpoint IDs, and state current applicability separately. A new scan is
not a deterministic-check prerequisite and cannot update that historical
candidate.
