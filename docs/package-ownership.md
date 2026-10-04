# Package ownership

`code_scout` is an empty package boundary. Its only Python file is
`__init__.py`; every implementation belongs to one of the owning subpackages
below.

| Package | Owns |
| --- | --- |
| `code_scout.core` | Cross-cutting primitives, including the shared error taxonomy. |
| `code_scout.workspace` | Workspace-local paths, configuration, and status. |
| `code_scout.runtime` | User-level runtime configuration and model routing. |
| `code_scout.commands` | CLI adaptation, command specifications, parsing, dispatch, and diagnostics. |
| `code_scout.engines` | Query-engine contracts, registry, providers, and engine implementations. |
| `code_scout.evidence` | Evidence assessment, packs, retrieval, navigation, and evidence tools. |
| `code_scout.impact` | Git change selection, change mapping, traversal, and reports. |
| `code_scout.extractors` | Source and metadata extractors plus their catalog and profiles. |
| `code_scout.harvest` | Raw-evidence collection, adapters, assembly, facts, and revisions. |
| `code_scout.models` | Typed graph, fact, workspace, and quality models. |
| `code_scout.pipeline` | Scan, watch, snapshot, inspection, and incremental orchestration. |
| `code_scout.storage` | Database schema, persistence, snapshots, and stored evidence. |
| `code_scout.utils` | Small shared utility functions that do not own a feature workflow. |
| `code_scout.domain` | Domain normalization and graph resolution. |
| `code_scout.verification` | Frozen-profile policies, checkpoints, sessions, execution, integrity, and receipts. |

## Contributor rule

First-party imports must name the owning subpackage directly. For example,
import workspace configuration from `code_scout.workspace.config`, rather than
from `code_scout.config`.

Do not add Python modules directly under `src/code_scout`, including
root-level compatibility or forwarding modules. Compatibility modules are not
permitted: callers must migrate to the owning subpackage. The root-package
guard in `tests/test_root_package.py` protects this rule.

The root-package guard in `tests/test_root_package.py` is a permanent
regression check: it must pass with `__init__.py` as the sole Python file in
the root package.
