# Module Migration Conventions

`code_scout` is an intentionally empty package boundary. Its only Python file
is `__init__.py`; all implementation code and first-party imports belong to an
owning subpackage. Root-level forwarding modules are not supported.

## Ownership boundaries

| Package | Owns |
| --- | --- |
| `code_scout.core` | Cross-cutting primitives such as the shared error taxonomy. |
| `code_scout.workspace` | Workspace-local configuration, state paths, and status. |
| `code_scout.runtime` | User-level configuration and runtime routing. |
| `code_scout.commands` | CLI adaptation, command specifications, parsing, dispatch, and readiness diagnostics. |
| `code_scout.engines` | Query-engine contracts, registry, implementations, and provider adapters. |
| `code_scout.evidence` | Evidence assessment, packs, retrieval, tools, and navigation. |
| `code_scout.impact` | Git change selection, mapping, traversal, and reports. |
| `code_scout.verification` | Frozen-profile policies, checkpoints, execution receipts, and retained-checkpoint reports. |

Other existing packages retain their focused ownership; they are not
compatibility destinations.

## Import rule

Import concrete modules from their owners. For example, import workspace
configuration from `code_scout.workspace.config`, CLI adaptation from
`code_scout.commands.cli`, and readiness diagnostics from
`code_scout.commands.doctor`.

Legacy root paths have been removed, including `code_scout.cli`,
`code_scout.doctor`, `code_scout.config`, `code_scout.global_config`, the
former evidence modules, and the former impact modules. Do not recreate them
as shims. Package initializers should remain shallow and must not eagerly
re-export feature modules.

## Enforcement

`tests/test_root_package.py` permanently requires `src/code_scout/__init__.py`
to be the only Python file directly inside the root package. Migration tests
also verify that removed paths cannot be imported. Add new implementation code
to the appropriate owning subpackage instead of relaxing either guard.
