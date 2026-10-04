# Engine selection and local-first operations

This guide covers model routing, optional providers, runtime boundaries,
trajectory retention, and cleanup. Code-scout core requires Python 3.11 or newer
and has no mandatory Python dependencies. Model servers, model weights, and
optional provider packages are installed and operated separately.

The checkout's `scripts/install-local-tool.sh` is the complete local tool
installation path: it uses Python 3.12, installs the Graphify CLI from the
`graphifyy` package, and installs the pinned RLM provider alongside code-scout.
The manual provider commands below are for custom environments or repairing an
installation assembled another way.

## Local dirty-workspace verification

`code-scout change ...` is a separate local executable-verification workflow,
not the model-backed `--engine verify` query engine. It can run with no
`.code-scout` index, no scan, no configured model provider, and no network
service. It uses an explicitly selected frozen TOML profile and selected Git
worktrees; it does not discover repositories or infer commands from task prose.

P0 gate execution is supported on macOS and Linux. Other platforms fail before
commands execute. It is a cooperative same-user process boundary, not a sandbox:
the profile, tools, environment inputs, caches, and external services remain
limits on reproducibility. See the
[disposable multi-repository workflow](change-verification-workflow.md) for an
end-to-end dirty-worktree example, including reference-member and concurrency
limits.

## Status-to-impact workflow

This workflow is entirely local. `status`, `doctor`, and `impact` do not scan,
run tests, call a model, probe an endpoint, modify Git state, or update the
stored graph.

### 1. Establish evidence readiness

Initialize and scan a single-repository workspace, then inspect the versioned
status report:

```sh
code-scout init --workspace .
code-scout scan --workspace .
code-scout status --workspace . --format json
```

The top-level status fields are `schema_version`, `workspace`, `index`,
`freshness`, `changes`, `coverage`, and `recommendations`.

- `index.state` is `missing`, `unscanned`, `ready`, `unreadable`, or
  `incompatible`. A ready index is readable and schema-compatible; it is not by
  itself proof of freshness or extraction coverage.
- `freshness.state` is `fresh`, `stale`, or `unknown`. Freshness compares current
  file bytes with hashes in the resolved `files` table. Timestamps are not used.
  `freshness.coverage` and `coverage_reasons` explain whether that comparison was
  complete.
- `changes` lists repository-relative added, modified, deleted, and unreadable
  paths with counts. `coverage` describes scan scope, enabled and observed
  extractors, file/language discovery, symbol and relationship evidence, grammar
  support, and explicit gaps.
- `recommendations` contains commands appropriate to the observed state.

Run the local readiness diagnostic separately:

```sh
code-scout doctor --workspace . --format json
```

Doctor returns `schema_version`, `workspace`, `summary`, and `checks`. Every
check has an `id`, `severity`, `reason`, and `remediation`. It distinguishes
workspace and index failures from extractor executables, Tree-sitter grammars,
optional Serena/dede integrations, and fast/verify provider configuration.
Provider readiness is configuration-only: endpoint reachability is deliberately
not tested.

Concrete remediation:

- `index.state: missing`: run `code-scout init --workspace .`, review the new
  `.code-scout/config.yaml`, then scan.
- `unscanned`, `unreadable`, `incompatible`, `stale`, or freshness `unknown`: run
  `code-scout scan --workspace .` and rerun status. For unknown freshness, also
  inspect `coverage_reasons`; a deliberately scoped extractor/profile scan may
  need a full scan.
- Disabled or unobserved symbol/relationship extraction: enable
  `symbol_extraction` or configure Graphify, install the named grammar or local
  executable, then rescan. Do not interpret zero emitted facts as proof that an
  extractor ran; use scan-scope and observed-extractor fields.
- A doctor `error`: follow that check's `remediation` and rerun doctor. A
  `warning` names degraded optional capability or stale evidence and does not by
  itself make the command fail.

### 2. Select exactly one Git comparison

Stage only the change under review and keep unrelated working-tree edits
unstaged:

```sh
git add src tests
code-scout impact --workspace . --staged --format json
```

`--staged` compares `HEAD` with the Git index. It excludes unstaged
working-tree changes, even when an unstaged edit is a different version of an
already staged file. On an unborn branch, the index is compared with Git's
empty tree.

For committed branch work, compare `HEAD` with the merge base of a local branch,
tag, or commit:

```sh
code-scout impact --workspace . --base main --max-hops 2 --max-results 100
```

`--base main` resolves `main`, calculates `merge-base(main, HEAD)`, and compares
that merge-base commit with `HEAD`. It excludes staged and unstaged changes and
does not fetch a remote. Refresh local refs yourself if the comparison must
include newer remote state. `--staged` and `--base` are mutually exclusive and
exactly one is required.

The single-repository MVP requires the workspace to configure exactly one Git
repository for impact analysis. Split or run the workflow from one repository
when a workspace contains multiple configured repositories.

### 3. Read the impact report conservatively

Impact JSON contains `schema_version`, `repository`, `comparison`, `index`,
`changed_files`, `changed_symbols`, `affected_entities`, `affected_contracts`,
`affected_services`, `test_candidates`, `paths`, `category_coverage`,
`coverage_gaps`, and `truncation`.

The comparison records requested and resolved revisions and exact old/new blob
identities. A changed hunk maps to a stored symbol only when the persisted file
SHA-256 equals the bytes of the selected old or new Git blob. Workspace-level
freshness is not substituted for this index-to-blob match. Binary, mode-only,
rename-only, unindexed, unsupported, missing-hash, or third-version files remain
file-level or explicitly unresolved rather than acquiring invented symbols.

Every observed impact path preserves stored source-to-target edge identity,
extractor provenance, certainty, and repository-relative citations while being
presented from dependency to dependent. `--max-hops` accepts 1–5 (default 2),
and `--max-results` accepts 1–1000 (default 100). Check
`truncation.truncated`; bounded or empty traversal does not prove no impact.
Coverage is limited to indexed files, exact mapped ranges, configured and
observed extractors, supported dependency-bearing relationship kinds, and the
selected traversal bounds.

`test_candidates.evidence_linked` are reachable through stored graph evidence;
they are candidates to review or run, not proof of execution or coverage.
`test_candidates.inferred` are a separate naming/directory-convention fallback.
Each inferred item names its heuristic and changed source, and carries no
invented dependency edge or coverage claim. Treat `coverage_gaps` and unavailable
`category_coverage` as required review notes. Remediate stale or unavailable
evidence with a full scan; remediate unresolved mappings by enabling/installing
the named extractor or grammar and rescanning; increase bounds only when a wider
bounded review is justified.

### Exit codes

| Command | `0` | `1` | `2` |
| --- | --- | --- | --- |
| `status` | A report was produced, including stale/unknown states. | Operational/configuration failure, including unsupported format. | Not currently used by the shared status adapter. |
| `doctor` | No check has `error` severity; warnings may remain. | At least one readiness check is an error. | Invalid command usage, such as a missing option value or unsupported format. |
| `impact` | A report was produced, including empty, stale, or explicitly unresolved results. | Git, ref, repository, or required index/graph evidence is unavailable. | Invalid selector, bounds, format, or missing option value. |

Use `--format text` for review and `--format json` for automation. JSON impact
errors preserve `schema_version` plus an `error` object with `kind`, `message`,
and `hint` on stderr.

## Create the global configuration

Model routing is machine-wide and never comes from a repository. Create
`~/.code-scout/config.toml`, inspect the normalized redacted view, and validate
referenced environment secrets with:

```sh
code-scout config init
code-scout config path
code-scout config show
code-scout config validate
```

Set `CODE_SCOUT_HOME` to relocate the global directory. `config init` refuses to
overwrite an existing file unless `--force` is supplied. Remote credentials are
named with `api_key_env` and supplied through the process environment; never put
the credential itself in TOML.

`fast` is the quick repository localization engine backed by an externally
served OpenHands CodeScout checkpoint. Every query prints the canonical resolved
engine and provider. Use `--engine static`, `--engine fast`, or `--engine verify`.
`static` is always local: it queries the index last refreshed by `code-scout scan`
and needs no model target.

Fallback is disabled by default. To opt in, configure an ordered list for a
canonical engine. A failed attempt and the selected fallback are always reported
as a warning; code-scout never silently changes providers.

```toml
[selection]
fast = "fast"
fallbacks = { fast = ["verify"] }
```

The fallback order applies after alias resolution. An explicitly selected engine
can also use its configured order. If no order exists, its failure is returned
unchanged.

For deterministic, model-free semantic and lexical retrieval, refresh the index
and query it through the same CLI contract as every other engine:

```sh
code-scout scan --workspace .
code-scout -q "Where is request routing implemented?" --engine static
```

## Complete redacted routing example

This example routes the CodeScout-backed `fast` engine to an Ollama-style
localhost URL and verify root/sub-models independently to local and remote
vLLM-style URLs. The model names are served identifiers, so match them to the
names advertised by your servers.

```toml
[selection]
fast = "fast"
# Remove this line to keep fallback disabled.
fallbacks = { fast = ["verify"] }

[engines.fast]
max_turns = 12

[engines.fast.model]
base_url = "http://127.0.0.1:11434/v1" # Ollama OpenAI compatibility
model = "OpenHands/CodeScout-4B"
timeout_seconds = 120
max_tokens = 4096

# Override the configured budget for one request with:
# code-scout -q "Locate the router" --engine fast --max-turns 20

[engines.verify]
isolation = "local"
max_iterations = 40
max_sub_calls = 16
max_output_characters = 20000
timeout_seconds = 300
max_total_tokens = 64000

[engines.verify.root_model]
base_url = "http://127.0.0.1:8000/v1" # local vLLM-style
model = "verify-root-model"
timeout_seconds = 120
max_tokens = 8192
parameters = { temperature = 0.1 }

[engines.verify.sub_model]
base_url = "https://models.example.invalid/v1" # remote vLLM-style
model = "verify-sub-model"
api_key_env = "VERIFY_SUB_API_KEY"
timeout_seconds = 60
max_tokens = 4096
parameters = { temperature = 0.2 }
```

Export `VERIFY_SUB_API_KEY` before validation or querying. `config show` prints
only the environment-variable name and `set (redacted)`/`missing`, never its
value. llama.cpp, Ollama, and vLLM are examples of OpenAI-compatible serving
shapes, not runtime dependencies; endpoint flags and model loading remain the
server operator's responsibility.

## Provider setup and provenance

### OpenHands CodeScout

The `fast` engine drives the released `OpenHands/CodeScout-1.7B`,
`OpenHands/CodeScout-4B`, or `OpenHands/CodeScout-14B` checkpoint through an
externally managed OpenAI-compatible endpoint. It does not import the OpenHands
runtime, download a checkpoint, or start a server. Published checkpoint results
cover Python localization; qualify other languages against representative local
workspaces.

```sh
code-scout --engine fast --workspace . --query "Locate every caller of route_request"
```

The model receives a shell-free, read-only terminal with bounded output, command,
turn, token, and timeout budgets. The host must provide the inspection executables
the model may request: `rg`, `grep`, `find`, `ls`, `sed`, `cat`, and `git`.

### Verify root/sub-models with rawwerks/rlm

Verify routes `root_model` and `sub_model` separately, so each can use a distinct
local or remote URL, model, secret, timeout, token limit, and generation settings.
Install the rawwerks/rlm revision pinned by the referenced rlm-cli commit:

```sh
uv pip install "rlm @ git+https://github.com/rawwerks/rlm.git@9a95a2bb38a73499473bc830b5b1b0806e79ce47"
```

The default `local` environment follows rawwerks/rlm-cli and does not require
Docker. It runs provider-generated Python in rawwerks/rlm's host process; for
untrusted evidence or models, configure `isolation = "docker"` and an
`isolation_image` instead.

```sh
code-scout --engine verify --workspace . --query "Verify every caller and blast radius of route_request"
```

Verify compiles one frozen pack from a completed `code-scout scan`. The native
rlm-cli adapter receives that JSON-compatible pack as its `context_payload` and
cannot browse the workspace through code-scout. Missing scan evidence yields an
explicit unresolved result rather than a repository-wide model read. Legacy
controller boundaries can still use code-scout's bounded evidence tools.

### Serena live navigation

Serena is an optional decorator over frozen symbol navigation, not an exploration
engine or model route. Operate a Serena MCP server separately and select exactly
one local stdio command or Streamable HTTP URL in the workspace
`.code-scout/config.yaml`:

```yaml
navigation:
  serena:
    enabled: true
    url: http://127.0.0.1:9121/mcp
    timeout_seconds: 5
```

For stdio, use `command: ["serena", "start-mcp-server"]` instead of `url` and
ensure the executable is on `PATH`. Only allowlisted read-only Serena tools are
called. Every live location crosses the same workspace/citation validator; a
timeout, unsupported language operation, malformed result, or invalid path keeps
the frozen code-scout result and adds a structured warning.

### Optional dede graphs

For a scanned .NET workspace, generate the dede graph separately and configure
its workspace-relative artifact:

```yaml
enrichment:
  dede:
    enabled: true
    graph_path: .code-scout/dede-graph.json
    executable: dede
    max_age_seconds: 86400
```

A query never invokes dede. The executable is checked only for diagnostics, while
the precomputed graph and every cited source range are validated independently.
Missing, stale, incompatible, or unsafe enrichment degrades to base evidence with
warnings.

## Security and data boundaries

- Global TOML controls model routing; workspace YAML controls scanning,
  navigation, and enrichment. Repository content cannot redirect model secrets.
- All provider paths and citations undergo lexical containment, resolved-symlink
  containment, existence, and line-bound validation. Source reads are limited to
  citations already frozen into the evidence pack where applicable.
- The `fast` engine receives a shell-free inspection allowlist. Verify receives
  frozen evidence and an isolated recursive controller. Serena is read-only and
  dede is import-only.
- A remote OpenAI-compatible endpoint receives the prompts/evidence sent to its
  selected engine. Choose local endpoints when source must not leave the machine,
  and apply the endpoint operator's retention and access policy.
- Trajectories redact known secrets and provider exception text, but can still
  contain queries, source-derived evidence, model output, paths, and commands.
  Protect `.code-scout` as project-sensitive data.
- Budgets and timeouts bound model and tool activity. They are resource controls,
  not a substitute for container isolation or endpoint access control.

## Trajectories and cleanup

Per-workspace redacted JSONL trajectories are stored beneath:

- `.code-scout/trajectories/codescout`
- `.code-scout/trajectories/verify`

Back up anything needed for audit, then remove only regenerable artifacts:

```sh
rm -rf .code-scout/trajectories
```

Removing the whole `.code-scout` directory also deletes workspace configuration,
the scan database, imported enrichment data, and trajectories; run
`code-scout init --workspace .` and `code-scout scan --workspace .` to rebuild it.
Global routing and secret references remain in `~/.code-scout/config.toml` and
must be removed separately if the machine configuration should also be retired.

## Design references that are not runtime dependencies

The aider repository-map idea is represented by code-scout's native bounded repo
map so there is no aider import or executable requirement. Megacode informs the
bounded-RLM design, but its runtime is not embedded. Claude Explore, TNT,
bazelcode, and rlmgw are comparison or design references only. None is imported,
executed, installed, or contacted by code-scout at runtime.

### Engine evidence assessment contract

Ordinary `code-scout -q "..." --engine static|fast|verify` invocations and
existing output keys are preserved. JSON adds per-evidence assessments; text
renders their ordered labels. An assessment describes origin, corroboration,
conflicts and freshness independently. A location passing path/line checks does
not establish corroboration, and there is no answer-wide `validated` label.
The legacy Python `CodeScoutResult.verified` property means citation presence only.

- **Static:** `metadata.matches` carries `evidence_id` and `assessment`; JSON
  exposes them in `likely_starting_points` and `evidence`. Scores still rank
  retrieval leads, not behavioral certainty. Empty retrievals carry no assessment.
- **Fast/CodeScout:** each citation has `supporting_evidence`, an ordered list of
  `{evidence_id, assessment}` objects. Local symbol resolution binds indexed raw
  observations only at the exact symbol/source range. File-only predictions,
  textual lookups and missing/legacy evidence remain unknown. Provider-supplied
  assessment fields are not accepted by the localization schema.
- **Verify/RLM:** citations bind only to referenced evidence in the frozen pack.
  `metadata.supporting_evidence` and the claim text also expose referenced evidence
  with unavailable/rejected citations. Unknown IDs receive unknown assessments;
  unbound top-level model citations remain rejected. Provider labels cannot
  replace local assessments. Existing `claim_classifications` buckets (including
  `confirmed`) remain legacy confidence/provenance classifications, distinct from
  the `validated` evidence label, which requires explicit corroboration.

For example, a legacy fast result with a valid `src/router.py:4-8` location but
no identified indexed evidence renders this citation block:

```text
Citations:
- src/router.py:4-8
  Evidence unmapped: unknown
```

The corresponding JSON citation is:

```json
{
  "path": "src/router.py",
  "start_line": 4,
  "end_line": 8,
  "supporting_evidence": [
    {
      "evidence_id": null,
      "assessment": {
        "origin": "unknown",
        "validation": "unknown",
        "conflicted": false,
        "freshness": "unknown",
        "reason_codes": [
          "assessment.origin.unknown",
          "assessment.validation.unknown",
          "assessment.freshness.unknown"
        ],
        "labels": ["unknown"]
      }
    }
  ]
}
```

Multiple observations at one location retain separate assessments. Citation
source assessment is separate from supporting-evidence assessment. A citation
may carry `repository_id`, `source_revision_id`, and `content_sha256`. When all
three bind to retained source bytes, validation checks the historical line range
against those exact bytes and reports `source_assessment.historical` as
`verified`. It then compares current workspace bytes independently: `fresh`
means exact byte equality; changed or missing live paths are `stale`; unreadable
or unbound paths are `unknown`, with machine-readable reasons. Thus a deleted or
changed live file does not erase inspectable historical evidence, while legacy
path-only citations remain `historical=unknown` and `freshness=unknown`.

### Reproduce a retained A/B citation workflow

Set the bounded local capture limit before scanning when the default 1 MiB per
source is unsuitable:

```yaml
# .code-scout/config.yaml
source_retention:
  max_bytes: 1048576
```

Scan version A and identify its snapshot and citation revision. Edit a local
source while keeping its line count the same, then scan version B and compare
the retained observations:

```sh
code-scout scan --workspace .
code-scout snapshot list --workspace . --format json
code-scout -q "the changed symbol" --engine static --workspace . --format json
# record SNAPSHOT_A and SOURCE_REVISION_A; make the same-line-count edit
code-scout scan --workspace .
code-scout changes --workspace . --from SNAPSHOT_A --to SNAPSHOT_B --format json
code-scout evidence show SOURCE_REVISION_A --workspace .
```

The A citation remains byte-stable against retained A bytes and becomes
`historical=verified, freshness=stale` against live B. A B citation bound from
B's indexed observation names B's revision and is fresh. A provider location
without the complete stored identity stays `revision-unknown` with unknown
source status; it must not be silently bound to live bytes. The diff reports
source-revision and evidence/property changes from the two retained payloads.

Local retained source bytes survive live source deletion and remain available
through `evidence show`. There is no automatic pruning policy, historical query
replay, or full event sourcing yet. Finally, these labels establish source
identity and freshness—not behavioral truth. A valid, intact citation and even
high confidence require separate corroboration before treating a claim as true.
