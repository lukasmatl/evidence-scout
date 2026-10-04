# Evidence-First Architecture

code-scout is an evidence harvester and resolver, not a set of independent graph
writers. Extractors produce facts with evidence. Storage keeps the active fact
stream. The resolver turns active facts into the current memory graph, which
engines and inspection commands read directly.

The mental model is:

- `code-scout scan` harvests evidence once for the configured workspace.
- `code-scout watch` keeps harvesting evidence continuously for changed files.
- Both paths converge on the same active-fact storage and resolver.

## Package Ownership Boundaries

The evidence-first pipeline is supported by clear package ownership boundaries.
Contributors should place new code in the owning package, rather than adding a
feature module at `code_scout` package root:

| Package | Ownership |
| --- | --- |
| `code_scout.evidence` | Evidence assessment, evidence-pack construction, semantic retrieval, embeddings, evidence tool adapters, and source/symbol navigation. |
| `code_scout.impact` | Git change selection, change-to-symbol mapping, impact-path tracing, and impact reports. |
| `code_scout.workspace` | Workspace-local configuration, state paths, and workspace status. |
| `code_scout.runtime` | User-level runtime configuration and provider-routing settings. |
| `code_scout.commands` | Command specifications, parsing, dispatch, and command-owned diagnostics. |
| `code_scout.engines` | Engine contracts, registry, engine implementations, and external engine/provider adapters. |
| `code_scout.verification` | Frozen-profile policy, checkpoint capture, session and attempt lifecycle, execution receipts, integrity checks, and reports over retained checkpoints. |

`code_scout.commands.cli` is deliberately small: it is the process-facing
adapter for Typer and fallback invocation, and delegates command behavior to
the rest of `code_scout.commands`. `code_scout` itself is an intentionally
empty package boundary: `__init__.py` is its only Python file. Legacy root
paths, including `code_scout.cli` and `code_scout.doctor`, have been removed;
new and existing callers must import their owning subpackage directly.

## Pipeline

```text
workspace files and configured external sources
  -> extractors
  -> active facts with source evidence
  -> fact storage
  -> resolver
  -> current memory graph
  -> engines and inspection commands
```

1. Discovery classifies workspace files and assigns repository-relative source
   paths.
2. Extractors emit `ActiveFact` rows directly or emit graph objects that are
   normalized into active facts.
3. Active facts are stored in `.code-scout/code-scout.db` in the `facts` table.
4. The resolver reads all active facts and resolves them into normalized graph
   objects: repositories, files, symbols, services, contracts, concepts,
   decisions, and edges.
5. The current graph tables are replaced from the resolved graph.
6. Snapshots and change events describe the resolved current graph, not a
   single extractor's private view.

This keeps new extractors complementary. A new extractor should add evidence
for a fact identity, not bypass the fact stream or write current graph tables
directly.

## Module Seams

code-scout keeps the evidence pipeline split into three contributor-facing module
seams: evidence harvest, command execution, and outputs. These seams describe
where code belongs; they do not create alternate data paths around active
facts, the resolver, or the current memory graph.

### Evidence Harvest

The Evidence Harvest module lives under `code_scout.harvest`. Its public interface is
the scan and incremental harvest API exported from `code_scout.harvest`: harvest scan
evidence, harvest incremental evidence, convert scan harvest results to active
facts, report package fact counts, and expose the public result shapes used by
scan and watch.

The extraction layer stays behind internal harvest adapters. Individual
extractors such as package metadata, OpenAPI, AsyncAPI, ADR, docs, infra,
symbol extraction, Graphify, and LSP are adapted into `HarvestAdapterResult`
inside `code_scout.harvest.adapters`. Adapter results may carry direct active facts,
graph fragments, warnings, package facts, LSP validation inputs, and framework
hints. They are not a new public extractor plugin seam; they are the internal
policy for making many extractor shapes look consistent before assembly.

Shared fan-in belongs in `code_scout.harvest.assembly` and graph-object conversion
belongs in `code_scout.harvest.facts`. Those modules combine adapter output, preserve
Graphify overlap policy, normalize contract identifiers, add inferred evidence,
and materialize active facts for scan and incremental paths. Scan and watch
should not rebuild their own conversion paths around these helpers.

### Graph readers

`code_scout.exporters.output_graph` is the shared resolved-graph reader used by
graph inspection and internal evidence retrieval. It does not run extraction,
write active facts, or expose a static artifact-generation command.

### Commands and CLI Adapters

The Command module lives under `code_scout.commands`. It owns command metadata, help
text, fallback argument parsing into `CommandOptions`, command execution, and
shared command presentation helpers. `execute_command()` is the command seam:
it receives a `CommandSpec` plus typed options and then calls scan, watch,
and graph inspection through their public module interfaces.

`code_scout.commands.cli` is an adapter layer. It owns Typer wiring when Typer is available,
fallback process invocation when it is not, version and help presentation,
error-to-exit-code handling, and typed option construction for Typer callbacks.
Both Typer callbacks and fallback parsing call the same command dispatch path.
New command behavior should be added to `code_scout.commands` first, with CLI changes
limited to adapter-specific option wiring and presentation.

## Graphify and code-scout Extractors

Graphify is the primary code graph provider. It owns high-order code graph
evidence: symbols, modules, imports, calls, code services, dependency edges,
and repository graph facts.

code-scout code extractors are complementary evidence providers:

- Tree-sitter and heuristic extraction fill Graphify coverage gaps.
- LSP validates or enriches code relationships using semantic language-server
  evidence.
- Framework and metadata extractors contribute facts Graphify may not own, such
  as package metadata, inferred HTTP usage, OpenAPI contracts, AsyncAPI
  contracts, ADR decisions, docs concepts, and infra topology.

When Graphify and a code-scout extractor observe the same entity, both should preserve
their evidence on the same durable identity. When they observe different
facets, each fact should stay scoped to the source file and extractor that
proved it.

## Active Facts

An active fact is the durable unit of extracted evidence. It has:

- `id`: deterministic fact identity.
- `extractor`: the evidence provider.
- `kind`: entity or relation shape.
- `source_file`: repository-relative file or configured external source.
- `identity`: stable entity or relation identity.
- `properties`: observed values.
- `evidence`: file path, extractor name, confidence, optional line range, and
  metadata.
- `confidence`: resolver input for this fact.
- `conflict_metadata`: preserved resolver conflict details.
- `extractor_version`: optional producer version.

The `facts` table is the active fact store. It is source-bucketed by
`(extractor, source_file)` so incremental work can replace only the facts that
belong to the changed source and extractor.

## Snapshots and Change Events

A full scan creates a snapshot row after active facts are resolved and the
current graph tables are replaced. Snapshot metadata records the selected
extractor or profile and the number of extractors included.

Watch updates create change events instead of snapshots. A change event records:

- changed or deleted source file set
- extractors run
- facts added and removed
- resolved graph nodes updated

Snapshots answer "what did this scan publish?" Change events answer "what did
this watch update change?"

### Retained source revisions and comparison

Snapshots reference immutable, content-addressed local source revisions rather
than copying source bytes into every snapshot. A capture is bounded by
`source_retention.max_bytes` (1 MiB by default) in workspace configuration.
When a scan can bind the complete `(repository_id, source_revision_id,
content_sha256)` tuple, engines and evidence packs retain that tuple with the
observation. Snapshot comparison (`changes --from A --to B`) compares the two
published payloads, including fact properties, evidence, and source-revision
references; it does not read or make claims about today's filesystem.
Occurrence metadata, such as a snapshot's scan time, remains on its snapshot
row rather than in immutable content. Fact properties and evidence metadata are
domain evidence even when their keys are named `timestamp`, `created_at`,
`updated_at`, or `scanned_at`. Existing immutable payloads are not rewritten:
values stripped by older versions cannot be recovered, so run a fresh scan to
publish complete future snapshots.

Citation source status has two independent dimensions: `historical=verified`
means the retained bytes and cited range passed integrity checks, while live
`freshness=fresh` means exact byte equality. Changed or deleted live bytes are
`stale`; unreadable or unbound locations are `unknown`; legacy path-only
citations remain `revision-unknown`. These labels authenticate an observation,
not its behavioral interpretation. In particular, citation integrity and line
validity do not prove a behavioral claim is true.

Source blobs remain in local storage after a live source deletion so historical
citations and snapshots remain inspectable. Automatic retention pruning,
historical query replay, and full event sourcing are deliberately deferred.

## Deleted Files

Deleted-file behavior is explicit. A watch delete event is converted to a
source-file replacement with an empty fact set for each selected extractor. That
removes stale active facts for the deleted source, reruns the resolver, and
replaces current graph tables without keeping dead current nodes.

A missing changed file that is not explicitly marked deleted is ambiguous. code-scout
returns an incremental warning and leaves facts unchanged, because the file may
be temporarily unreadable or the watcher event may be stale.

## Confidence, Precedence, and Conflicts

Facts carry confidence from their extractor. Exact parsed evidence such as ADR,
OpenAPI, AsyncAPI, package metadata, and tree-sitter facts generally uses high
confidence. Inferred or validation evidence such as docs, LSP, Graphify call
edges, and infra hints may use lower confidence.

The resolver applies precedence while preserving evidence:

- Durable identities merge facts for the same entity or relation.
- Repeated evidence is deduplicated and retained on the resolved graph object.
- Higher-confidence evidence can win scalar conflicts.
- Conflicting scalar values are not discarded silently. Conflict details are
  preserved in evidence metadata as resolver conflicts.

Output code converts preserved resolver conflicts into review warnings. Generated
docs and exports should surface those warnings instead of pretending the graph is
conflict-free.

## Evidence Assessment Contract

Every rendered behavioral claim may also carry an independent assessment. It is
not a replacement for extractor or resolver confidence:

- `origin`: `extracted`, `inferred`, or `unknown`.
- `validation`: `corroborated`, `unresolved`, or `unknown`.
- `conflicted`: a boolean.
- `freshness`: `fresh`, `stale`, or `unknown`.
- `reason_codes`: stable machine-readable codes explaining the compatibility
  mapping (for example, `assessment.conflict.resolver_conflicts`).

The fixed rendered-label order is `extracted`, `inferred`, `validated`,
`unresolved`, `conflicted`, `stale`, `unknown`; more than one label is allowed.
`validated` is rendered only for `validation=corroborated`. Extractor/resolver
confidence, including `confidence=1`, and a valid file/line citation are not
corroboration of a behavioral claim.

Existing records are mapped conservatively and deterministically. Known direct
parsers map to `extracted`; known relationship-inference extractors (including
Graphify and LSP) map to `inferred`; legacy `certainty=inferred` maps to
`inferred` only when extractor provenance is unavailable. Legacy certainty never
maps to `validated`. `unresolved=true` and legacy `certainty=unresolved` map to
`validation=unresolved`, and
`resolver_conflicts` maps to `conflicted=true`. Explicit assessment metadata
takes precedence. Missing or unrecognized metadata remains `unknown`; confidence
and citation presence do not fill those gaps.

## Resolved and packed assessments

Graph evidence serializes an additive `assessment` beside its original metadata.
Fact-level uncertainty is combined with each provider observation before resolution;
scalar precedence still chooses graph values, while losing observations and their
resolver conflicts remain inspectable. Inferred support remains inferred when
combined with corroborated or extracted support. Agreement alone does not create
an explicit corroboration marker.

Evidence packs carry assessments on raw evidence, relationships, source provenance,
and repository-map entries. Relationships retain ordered `supporting_evidence`,
including observations whose source cannot be used as a live citation. Raw evidence
also reads matching provider conflicts from its persisted graph owner when available.
Missing provenance stays unknown. Existing identifiers, confidence, certainty, and
unresolved fields remain compatible; assessment fields do not participate in graph
identity or scalar tie-breaking. Retrieval reads the current index without running
extractors or introducing another graph writer. Within the existing character budget,
duplicate relationship map entries are trimmed before full relationships.

## Watch Routes and Profiles

Watch routing maps changed source paths to extractor targets:

- TypeScript and TSX route to Graphify, tree-sitter, symbol extraction, and LSP.
- ADR Markdown routes to ADR extraction and supersedes generic Markdown docs.
- Markdown and MDX route to docs concept extraction.
- OpenAPI and AsyncAPI candidate paths route to contract extractors.
- Docker Compose and Dockerfile paths route to infra topology extraction.
- `package.json` routes to package metadata extraction.

Workspace config can add ADR, OpenAPI, AsyncAPI, infra, and package metadata
paths. Those configured paths extend the default watch globs.

Profiles restrict which route-selected extractors are allowed to run:

- `code-only`: Graphify, tree-sitter, LSP.
- `docs`: ADR, docs, OpenAPI.
- `agent`: Graphify, OpenAPI, ADR.
- `full`: Graphify, tree-sitter, LSP, ADR, OpenAPI, AsyncAPI, infra, docs.

Profiles are filters over enabled extractors. They do not change the evidence
pipeline.

## Graphify Watch Integration

Graphify can be configured with a scan source or a watch source:

- `extractors.graphify.command`
- `extractors.graphify.json_path`
- `extractors.graphify.watch.command`
- `extractors.graphify.watch.output_path`

During watch updates, code-scout only selects Graphify when a Graphify source is
configured. Graphify watch events can provide a graph payload directly or a
payload under `graph`; code-scout converts those events through the same Graphify JSON
adapter used by scan. Watch events are cached per watch update so multiple
changed code files do not repeatedly invoke the same Graphify watch source.
