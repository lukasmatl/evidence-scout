---
name: code-scout-impact-review
description: Review staged/base Git changes or retained task/session checkpoint evidence with Code Scout to identify affected callers, contracts, services, and relevant tests from cited impact evidence. Do not use for ordinary text lookup or whole-codebase health audits.
---

# Code Scout Impact Review

Use this skill for staged-change, branch-change, or retained task/session checkpoint review; questions about affected callers, contracts, or services; and selection of tests for a selected comparison. Do not use it for ordinary identifier/text lookup or whole-codebase health or technical-debt audit.

## Select the comparison exactly

Infer one mode from the user's words:

- Use `--staged` for staged, index, pre-commit, or “what I am about to commit” changes. It compares `HEAD` with the Git index and excludes unstaged content.
- Use `--base REF` for branch, PR, or committed-change review when the base ref is stated or established by context. It compares the merge base of `REF` and `HEAD` with `HEAD`, excluding staged and unstaged changes.
- Use retained checkpoint evidence when the user supplies a `CHANGE_ID`, `RUN_ID`, or `SESSION_ID`, or asks to review an uncommitted task/session window. Select the explicit task-window or cumulative-session artifact from that recorded run; it compares retained checkpoints, not live Git or the current working tree.

Confirm the intended repository and use read-only Git inspection to verify its top level and requested ref. The impact report must identify the resolved repository, requested ref, merge base, revisions, and blobs. If mode, repository, or base cannot be inferred, ask one concise question.

Never stage files, switch branches, fetch refs, execute gates, or silently substitute working-tree changes. Never replace an invalid/missing base with a guessed ref. Preserve distinct index and working-tree content. If retained capture, IDs, or the selected impact artifact is missing/corrupt, report the exact evidence gap and stop; do not reconstruct history from the current tree, stage/commit, or create a new run.

## Collect and inspect evidence

For staged/base review, Reuse a current readiness result for the same workspace,
or run:

```sh
code-scout status --workspace . --format json
```

Carry freshness, scan-scope, symbol, relationship, and category-coverage gaps into the review. If authorized, a missing index is preparable with `init` then `scan`; preserve config, respect read-only/extractor limits, wait for scan completion or a writer lock, and report setup failures. Then run exactly one selected comparison:

```sh
code-scout impact --workspace . --staged --format json
code-scout impact --workspace . --base main --max-hops 2 --max-results 100 --format json
```

For retained checkpoint review, inspect only recorded artifacts:

```sh
code-scout change show CHANGE_ID --workspace /work/product --format json
code-scout change session show SESSION_ID --workspace /work/product --format json
code-scout change evidence RUN_ID --workspace /work/product --format json
```

Use `change show` to identify the task's `comparisons.task_window` and available runs; use `change session show` for member roles and a session-scoped run. `change evidence` validates the chosen receipt: confirm its `scope`, then cite `impact.task_window` or `impact.cumulative_session`, before/after checkpoint IDs, every member's repository ID/root, scan provenance, changed seeds, and coverage gaps. Never merge task-window/cumulative-session scope or infer task ownership from overlap. These commands are read-only; zero exit says only that the record was readable.

Inspect changed and cited source ranges. Git blobs define staged/base comparisons; retained checkpoint blobs define checkpoint comparisons. The current working tree is separate evidence, never historical comparison content.

For repeated broad searches for changed identifiers, configuration keys, or
script references, prefer Microsoft tgrep through
[code-scout-text-search](../code-scout-text-search/SKILL.md). It covers index
readiness, bounded queries, and fallback to `rg`; keep simple lookups as direct
`rg` searches and follow higher-priority search instructions. Treat text hits
as candidates, not confirmed callers, and keep tgrep freshness separate from
the Code Scout graph and selected Git comparison.

For a remaining named gap, use `--engine static` for ranked discovery. Static matches are leads, not proof. Use `fast` only for unresolved entry-point/flow localization, or `verify` for a bounded unresolved claim. Do not call a model merely to restate impact or opened source. Preserve warnings and data-handling limits.

Read [REFERENCE.md](REFERENCE.md) for output interpretation, the response
contract, command examples, and bounded scenario evaluations.

## Report conservatively

State that test candidates are recommendations unless a separate receipt records execution, naming its gate command/outcome. Impact alone makes no aggregate or per-test execution claim. Empty impact, unresolved/stale mappings, inference, missing coverage, or bounded traversal cannot establish safety or exhaustive caller coverage.
