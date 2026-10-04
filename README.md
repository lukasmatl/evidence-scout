# code-scout

code-scout is a local-first architecture memory CLI for software workspaces.

code-scout follows an evidence-first architecture: `code-scout scan` harvests
evidence once, while `code-scout watch` keeps harvesting evidence continuously. In both modes,
extractors feed active facts, the resolver builds the current memory graph, and
outputs read that resolved graph. See
[Evidence-First Architecture](docs/evidence-architecture.md) for the contributor
mental model.

## Development

Contributors should follow the strict [package ownership map](docs/package-ownership.md):
first-party imports use an owning subpackage, and root-level compatibility modules
are not permitted.

Install dependencies and create the local uv environment:

```sh
uv sync
```

Run tests:

```sh
uv run pytest
```

Run lint checks:

```sh
uv run ruff check .
uv run ruff format --check .
```

Run the CLI:

```sh
uv run code-scout --help
```

## Dirty-workspace change verification

Change verification records frozen, content-addressed checkpoints for explicitly
selected Git worktrees and runs a declared local gate profile against the whole
candidate. It supports dirty, multi-repository sessions and single-repository
sessions without an index, scan, model, or network service. See the
[disposable multi-repository workflow](docs/change-verification-workflow.md) and
the [verification contracts](docs/change-verification-contracts.md) for lifecycle,
input ownership, historical applicability, and trust limits.

This product workflow is distinct from `code-scout --engine verify`, which is
model-backed evidence analysis. The dedicated
[`code-scout-change-verification`](skills/code-scout-change-verification/SKILL.md)
skill routes deterministic gates and receipt inspection without an index or model.

## Agent skill selection

The repository distributes agent workflows as self-contained directories under
`skills/`. Choose one top-level workflow from the request; the specialized
skills are alternatives to the general CLI workflow, not mandatory nested
calls.

| Skill | Purpose | Trigger examples | Prerequisites | Expected output |
| --- | --- | --- | --- | --- |
| [`code-scout-workspace-readiness`](skills/code-scout-workspace-readiness/SKILL.md) | Check index freshness and evidence coverage, or diagnose local setup. | “Is the index current?” “Why is verify unavailable?” | A workspace path; `status` can report missing state, so an existing index is not required. | A read-only status/doctor report with observed gaps and concrete remediation. |
| [`code-scout-impact-review`](skills/code-scout-impact-review/SKILL.md) | Review the cited impact of staged or committed branch changes. | “Review what I staged.” “What changed from main?” | One local Git repository, an explicit staged/base selection, and Code Scout index evidence for graph-backed conclusions. | Comparison identity, changed seeds, cited affected paths, test candidates, and limitations. |
| [`code-scout-text-search`](skills/code-scout-text-search/SKILL.md) | Use optional tgrep for repeated, broad exact-text searches while preserving direct-search checks. | “Repeatedly find this configuration key across all packages.” | tgrep plus a complete index for indexed use; otherwise the workflow falls back to bounded `rg`. | Text-occurrence candidates and explicit search/index limits, never inferred call relationships. |
| [`code-scout-change-verification`](skills/code-scout-change-verification/SKILL.md) | Execute a frozen local gate profile or inspect its retained receipt. | “Run the required checks for this session.” “Does this green receipt still apply?” | Explicit authorized session/change/run context for lifecycle actions; receipt inspection is read-only. No scan, graph, or model is required. | Candidate- and policy-bound gate receipt, or historical outcome and current-applicability limits. |
| [`code-scout-cli`](skills/code-scout-cli/SKILL.md) | Explore a non-trivial concept or cross-file flow with static retrieval, exact search, source inspection, and model engines when needed. | “Trace request routing across services.” “Verify this bounded behavior.” | `.code-scout/code-scout.db` for the required static pass and verify; configured endpoints only for selected model-backed engines. | Ranked starting points followed by inspected, cited findings or clearly named evidence gaps. |

### Make the skills visible to Ralph's agent

Keep each complete `skills/<name>/` directory together: `SKILL.md` is the
entrypoint, linked references contain detailed guidance, and `agents/openai.yaml`
supplies OpenAI-agent presentation metadata where present. The checked-in copy
is a shipped package; it is not evidence that the package is installed or
discoverable by a particular host. Make the required directories available
through the chosen agent's documented project-level skill discovery mechanism—
for example, copy or symlink the complete packages into `.agents/skills/` for an
agent configured to scan that repository-local root—or point the agent at the
checked-in files when its host supports explicit skill paths. Verify the skill
names appear in that agent's available-skill inventory.

Ralph invokes the agent selected by `agent = ...` in `.ralph-tui/config.toml`;
it does not itself install or register these packages. Committing a directory
under `skills/`, installing tgrep, or installing the Code Scout CLI therefore
does not guarantee that every Ralph agent can discover the skill or will select
it. Configure and verify discovery for the actual agent host. No repository
setup step should silently install global skills or change user-level agent
settings. Until that authorized host inventory has actually been checked, record
manual discovery as unverified rather than claiming that a shipped package is
installed.

### Prompt routing examples

| Example prompt | Intended route | When this workflow is unnecessary |
| --- | --- | --- |
| “Check whether the Code Scout index is fresh and has relationship coverage.” | `code-scout-workspace-readiness`; run `code-scout status --workspace . --format json`. | Skip it when freshness and extraction coverage do not affect the request. |
| “Diagnose why Code Scout model verification is unavailable here.” | `code-scout-workspace-readiness`; run status first, then `code-scout doctor --workspace . --format json` for the setup gap. | Doctor is unnecessary when a current status result answers the readiness question and no setup diagnosis was requested. |
| “Review what I have staged before I commit.” | `code-scout-impact-review`; run `code-scout impact --workspace . --staged --format json`. | Skip it for unstaged working-tree review or ordinary source lookup; the command intentionally selects only the Git index. |
| “Review this branch against main and tell me what it affects.” | `code-scout-impact-review`; verify the local ref, then run `code-scout impact --workspace . --base main --format json`. | Skip it when there is no Git comparison or when the desired base is not known yet. |
| “Find every exact reference to `CODE_SCOUT_HOME` across this large workspace.” | `code-scout-text-search` when repeated breadth makes an already-ready tgrep index useful; otherwise use bounded `rg`. | Skip the indexed workflow for a cheap one-off search or when tgrep state is missing, incomplete, or uncertain. |
| “Run the required checks for this session.” | `code-scout-change-verification`; execute the frozen `change verify` profile for the supplied active task or explicit session. | Ask only when the supplied context cannot identify the authorized session/change or policy selection; do not scan or invoke a model. |
| “Does this old green receipt still apply?” | `code-scout-change-verification`; use the read-only `change show`/`change evidence` record and report historical outcome separately from current applicability. | Skip gate execution unless the user asks for a fresh verification run. |
| “Trace how request routing crosses modules.” | `code-scout-cli`; begin with a focused `--engine static` query, inspect source, then use `fast` only for an unresolved localization/flow gap. | Skip it when one exact current-source lookup fully answers the question. |
| “Where is `CODE_SCOUT_HOME` defined?” | No skill: use a bounded `rg` lookup. | The heavier readiness, impact, indexed-text, and semantic/model workflows add no useful evidence. |

## Install the CLI

From a repository checkout, install or refresh the global `code-scout` command:

```sh
./scripts/install-local-tool.sh
```

The script requires `uv` on `PATH`. It force-reinstalls and refreshes both the
Graphify CLI and this checkout with Python 3.12, then confirms that
`code-scout` is available on `PATH`. By default, Graphify is installed from the
`graphifyy` package; set `CODE_SCOUT_GRAPHIFY_PACKAGE` to install a different
package or local path:

```sh
CODE_SCOUT_GRAPHIFY_PACKAGE=/path/to/graphify ./scripts/install-local-tool.sh
```

The `code-scout` installation includes the pinned rawerks RLM revision used by
optional scan and `verify` workflows. Any arguments passed to the script are
forwarded to its `uv tool install` invocation for `code-scout`. The `fast`
engine talks directly to its configured OpenAI-compatible CodeScout endpoint
and needs no additional provider package.

## Global Model Configuration

For the complete local/remote endpoint matrix, alias and opt-in fallback policy,
provider provenance, Serena/dede setup, security boundaries, trajectory retention,
and cleanup, see the
[engine selection and local-first operations guide](docs/operations.md).

Model-backed engines read routing only from `~/.code-scout/config.toml`; the
workspace `.code-scout/config.yaml` remains limited to workspace scanning,
navigation, evidence enrichment, and output settings. Create and inspect the
global configuration with:

```sh
code-scout config init
code-scout config path
code-scout config show
code-scout config validate
```

Set `CODE_SCOUT_HOME` to use a different global directory. Each model target
selects an OpenAI-compatible `base_url` and `model`. Remote targets reference a
secret by environment-variable name through `api_key_env`; the secret itself
is never stored in TOML or printed by `config show`. Loopback endpoints may be
configured without authentication. The `verify` engine has independent
`root_model` and `sub_model` targets.

### Recursive verification with rlm-cli

The optional `verify` engine sends only a frozen, scan-backed evidence pack and
its evidence identifiers to a native adapter matching rawwerks/rlm-cli's pinned
`rlm.RLM` API. It never places the whole repository in the root prompt. The
default local environment needs no Docker:

```toml
[engines.verify]
isolation = "local"
max_iterations = 40
max_sub_calls = 16
max_output_characters = 20000
timeout_seconds = 300
max_total_tokens = 64000
```

Install the exact optional provider revision used by the referenced rlm-cli:

```sh
uv pip install "rlm @ git+https://github.com/rawwerks/rlm.git@9a95a2bb38a73499473bc830b5b1b0806e79ce47"
```

Local mode runs provider-generated Python in rawwerks/rlm's host-process
`LocalREPL`; use `isolation = "docker"` plus `isolation_image` when the evidence
or model is not trusted. Root and sub-model endpoints, authentication, timeouts,
token limits, and generation parameters remain independently configurable.
Redacted JSONL trajectories are written beneath `.code-scout/trajectories/verify`.

### OpenHands CodeScout

Select the endpoint-backed localization engine with:

```sh
code-scout --engine fast --workspace . --query "Locate every caller of route_request"
```

The `fast` engine supports the released OpenHands CodeScout 1.7B, 4B, and 14B
checkpoint identifiers through an externally managed OpenAI-compatible endpoint.
It does not download weights or start a server. `code-scout config show` reports
the selected checkpoint size and whether endpoint configuration is ready. Its
readiness status is configuration-only and does not make a network request.

Published CodeScout quality results come from Python code-localization
evaluations. They do not establish or imply validated localization quality for
multilingual repositories; assess non-Python languages against your own
representative workspaces before relying on the engine.

## MVP Demo Workflow

code-scout is run through `uv` during local development. From a fresh checkout, create
the local environment and verify the CLI is available:

```sh
uv sync
uv run code-scout --help
```

Initialize code-scout metadata in the workspace you want to inspect:

```sh
uv run code-scout init --workspace .
```

This writes `.code-scout/config.yaml`. Edit that file before scanning if the workspace
has multiple repositories or optional extractor settings.

Run the default scan:

```sh
uv run code-scout scan --workspace .
```

Check that the index is usable and still matches the workspace before reviewing
a change:

```sh
uv run code-scout status --workspace .
uv run code-scout doctor --workspace .
```

`status` reports index state, content-hash freshness, changed paths, scan scope,
extractor and language coverage, and concrete scan or setup recommendations.
`doctor` separates workspace/index, extractor, grammar, optional integration,
and model-routing checks; it does not probe configured endpoints. After staging
exactly the change to review, request a local, cited impact report:

```sh
git add src tests
uv run code-scout impact --workspace . --staged
```

To review committed work on the current branch against its merge base with
`main`, use:

```sh
uv run code-scout impact --workspace . --base main
```

`--staged` excludes unstaged working-tree edits. `--base main` compares the
merge base of `main` and `HEAD` with `HEAD`; it does not include uncommitted
index or working-tree edits. Impact mappings are trusted only when the indexed
SHA-256 matches the exact selected old or new Git blob. Empty results are not a
safety or test-coverage claim: review coverage gaps, traversal truncation, and
whether test candidates are evidence-linked or only inferred by naming
convention. The current MVP supports one configured Git repository per impact
request.

See the [status-to-impact operations workflow](docs/operations.md#status-to-impact-workflow)
for output fields, exit codes, limits, and remediation.

The scan writes code-scout's local SQLite graph data to `.code-scout/code-scout.db`. Default scans
use built-in, dependency-light extractors, including `symbol_extraction` for
multi-language symbol and relationship extraction. Optional external process
extractors are skipped unless they are requested explicitly.

`code-scout init` enables `symbol_extraction` in `.code-scout/config.yaml` by default:

```yaml
extractors:
  symbol_extraction:
    enabled: true
```

Set `enabled: false` to disable the multi-language symbol extractor for a
workspace.

Ask a configured exploration engine about the workspace:

```sh
uv run code-scout --workspace . --engine fast \
  --query "Locate the authentication middleware"
```

Use `--engine static`, `--engine fast`, or `--engine verify`. The `static`
engine returns deterministic, local-index backed, evidence-only results from the
index produced by `scan`.

```sh
uv run code-scout -q "implement X" --workspace . --engine static
```

Static text output is intended for humans choosing likely starting points before
implementation. Static JSON output carries the same evidence-only result in a
machine-readable shape for orchestrator skills and agents that need local,
deterministic evidence before implementation:

```sh
uv run code-scout -q "implement X" --workspace . --engine static --format json
```

### Retained-evidence workflow

Each successful scan publishes a retained snapshot and, for eligible local text
files, stores immutable source bytes in the local `.code-scout/code-scout.db`.
The default retention limit is 1 MiB per source; configure a smaller or larger
bounded capture in `.code-scout/config.yaml`:

```yaml
source_retention:
  max_bytes: 1048576
```

For a reproducible A/B review, scan A, record its snapshot ID, change a source
without changing its line count, then scan B:

```sh
uv run code-scout scan --workspace .
uv run code-scout snapshot list --workspace . --format json
# edit a source file, keeping its line count unchanged
uv run code-scout scan --workspace .
uv run code-scout changes --workspace . --from SNAPSHOT_A --to SNAPSHOT_B
uv run code-scout snapshot show SNAPSHOT_A --workspace .
uv run code-scout evidence show SOURCE_REVISION_A --workspace .
```

A citation with its complete `repository_id`, `source_revision_id`, and
`content_sha256` reports `historical=verified`; live bytes that differ from its
retained bytes report `freshness=stale`. `fresh` means exact live-byte equality.
Missing identity is `revision-unknown` / `unknown`, rather than a guess. These
source labels establish identity and freshness only: citation integrity, a high
confidence score, or a valid line range does not establish behavioral truth.
Retained bytes can still be read after the live file is deleted. Automatic
pruning, historical-query replay, and full event sourcing are intentionally
deferred.

The `static` and `verify` engines require a completed scan; the
repository-reading engines do not produce static export or documentation
artifacts.

For broad tree-sitter grammar support, install the optional grammar extra:

```sh
uv sync --extra tree-sitter-all
```

This adds the core `tree-sitter` package plus supported grammar bindings for
languages such as Python, JavaScript, TypeScript, Go, Rust, Java, C/C++, Ruby,
C#, Kotlin, Scala, PHP, Swift, Lua, Zig, Dart, Elixir, Julia, Bash, and SQL.
If a grammar is not installed, code-scout degrades gracefully for that file: the scan
continues and other extractors can still contribute graph data.

### Optional Extractors

The LSP extractor is opt-in because it requires a language server. For
TypeScript workspaces, install or otherwise make `typescript-language-server`
available on `PATH`, or configure `extractors.lsp.command` or
`extractors.lsp.connect` in `.code-scout/config.yaml`. Then run:

```sh
uv run code-scout scan --workspace . --extractor lsp
```

Graphify is code-scout's primary code graph provider. It owns symbols, modules,
imports, calls, code services, and repository graph facts. code-scout's built-in code
extractors are fallback or complementary sources: they fill Graphify coverage
gaps, validate uncertain code evidence, or emit distinct facts such as inferred
HTTP usage.

The Graphify extractor needs an external graph source. When the `graphify` CLI
is available on `PATH`, a default scan runs `graphify update .` and reads the
generated graph from `.code-scout/graphify-out/graph.json`. You can also configure
`extractors.graphify.command` to run a command that prints code-scout-shaped graph
JSON, or `extractors.graphify.json_path` to read a generated JSON file from the
workspace. You can run Graphify alone:

```sh
uv run code-scout scan --workspace . --extractor graphify
```

### Optional dede verification enrichment

For a scanned .NET workspace, verification can import an existing
[dede](https://github.com/mitkox/dede) graph. Generate and refresh the graph
separately, then configure its workspace-relative path:

```yaml
enrichment:
  dede:
    enabled: true
    graph_path: .code-scout/dede-graph.json
    executable: dede
    max_age_seconds: 86400
```

code-scout never starts or scans with dede during a verification query. It
checks the configured executable only for availability diagnostics, validates
every imported source location against the selected workspace, and retains
the base scan-backed evidence when the executable or graph is missing, the
schema is incompatible, or graph metadata is stale.

Generated data and artifacts live under `.code-scout` by default:

- `.code-scout/config.yaml`: workspace configuration created by `code-scout init`
- `.code-scout/graphify-out/graph.json`: default Graphify code graph input created by
  `code-scout scan` when the `graphify` CLI is available
- `.code-scout/code-scout.db`: local SQLite graph data created by `code-scout scan`

## Migrating Existing Workspaces

The workspace-state rename is a hard cut. code-scout does not discover, read,
write, or recreate state under the retired product's state directory. Back up
or remove that directory, run `code-scout init --workspace .`, review the new
`.code-scout/config.yaml`, and run `code-scout scan --workspace .` to build a
fresh `.code-scout/code-scout.db`. Do not copy the retired database into the new
state directory; rescanning also prevents legacy-branded metadata from carrying
forward into newly persisted artifacts.
