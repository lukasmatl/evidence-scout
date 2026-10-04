# Change Impact Review Reference

Read this reference when interpreting an impact result, preparing the final
review, or checking one of the comparison scenarios below.

## Read-only verification and commands

Verify the repository and a user-selected base with argument-based, read-only
Git commands. Pass the literal repository path and ref as separate arguments;
do not assemble an executable shell string from user input:

```sh
git -C /path/to/repository rev-parse --show-toplevel
git -C /path/to/repository rev-parse --verify --end-of-options 'main^{commit}'
```

The implemented Git-comparison commands are:

```sh
code-scout status --workspace . --format json
code-scout impact --workspace . --staged --format json
code-scout impact --workspace . --base main --max-hops 2 --max-results 100 --format json
```

`impact` is local and read-only. It does not scan, run tests, call a model,
fetch, stage files, switch branches, or update the graph. Exactly one of
`--staged` and `--base REF` is required. `--max-hops` accepts 1–5 and
`--max-results` accepts 1–1000.

For retained checkpoint comparison, select an explicit recorded task, run, or
session; these are not arguments to `impact`:

```sh
code-scout change show CHANGE_ID --workspace /work/product --format json
code-scout change session show SESSION_ID --workspace /work/product --format json
code-scout change evidence RUN_ID --workspace /work/product --format json
```

`change show` exposes the attempt's recorded `comparisons.task_window` and
`comparisons.cumulative_session`; `change session show` exposes member identity
and available runs; `change evidence` validates the immutable run receipt and
its retained candidate/impact artifacts. Select either the task-window
(`before` task checkpoint → run candidate) or cumulative-session (session
initial checkpoint → run candidate) report explicitly. A session-scoped run has
only the cumulative session context; do not invent a task window for it.

All three commands are read-only inspection. They do not capture a checkpoint,
run gates, stage, commit, fetch, scan, update the graph, or make a current
workspace version historical. Their successful exit means inspection succeeded,
not that a run passed or still applies. If an ID, receipt, candidate, impact
artifact, checkpoint blob, member report, or selected scope is absent/corrupt,
report that specific gap. Do not guess a ref, select another run, stage or
commit content, execute a gate, or use live working-tree bytes as a substitute.

When a specific gap remains after impact output and source inspection, examples
of the engine roles are:

```sh
code-scout -q "payment authorization callers" --workspace . --engine static
code-scout -q "locate the cross-file payment authorization flow" --workspace . --engine fast
code-scout -q "does this change violate the payment authorization contract" --workspace . --engine verify
```

Do not run all three reflexively. Static is model-free ranked retrieval. Fast
localizes an unresolved entry point or flow; verify assesses a bounded claim.
Both model-backed modes can send prompts and repository evidence to configured
endpoints.

## Interpret the report

The JSON report includes `repository`, `comparison`, exact `changed_files` and
`changed_symbols`, affected entities/contracts/services, test candidates, cited
paths, category coverage, coverage gaps, and truncation.

- A symbol mapping is trustworthy only when the indexed SHA-256 matches the
  exact old or new Git blob used by the comparison. A stale working tree does
  not automatically invalidate an exact old-side or index-side match; a third
  file version must remain unresolved.
- Each observed path retains source-to-target relationship identity, extractor
  provenance, citations, per-edge certainty, and weakest-path certainty while
  the presentation walks from dependency to dependent.
- Evidence-linked tests have a stored path. Inferred tests use only the named
  file/directory convention and must not be presented as a relationship,
  coverage claim, or executed test.
- Empty categories mean no supported path was returned within current evidence
  and bounds. They do not prove that no callers, contracts, services, or tests
  are affected.

## Response contract

Return a focused inspection and validation plan containing:

1. **Comparison identity:** For Git mode, repository root/identity, requested
   base when applicable, resolved merge base, and old/new revisions. For
   checkpoint mode, change/run/session IDs, selected task-window or
   cumulative-session scope, before/after checkpoint IDs, and each member's
   repository ID/root and scan provenance. In both modes, say explicitly that
   current working-tree content is outside historical comparison content.
2. **Changed seeds:** changed files and exact mapped symbols/ranges, including
   file-only or unresolved mappings and their reasons.
3. **Affected evidence:** affected callers/entities, contracts, and services;
   for each material conclusion give the complete cited path, source ranges,
   relationship provenance, and weakest evidence strength/certainty.
4. **Test candidates:** evidence-linked and inferred candidates kept separate,
   each with its selection reason. Label all as recommendations. Only a separate
   gate receipt can report execution, and it must name the gate command/outcome;
   impact does not establish aggregate or per-test execution.
5. **Inspection and validation plan:** prioritize changed seeds, weakest or
   highest-risk paths, contract boundaries, and recommended tests. Distinguish
   facts inspected in current source from scan-backed evidence and proposals.
6. **Limits:** freshness and scan/extractor/category coverage gaps, unresolved or
   inferred evidence, selected bounds, and whether results were truncated.

Never turn an empty impact result, unresolved mapping, inference, or bounded
traversal into “safe,” “no impact,” or exhaustive caller coverage.

## Bounded scenario evaluations

These cases record expected routing and interpretation; they are not claims that
the example commands were executed.

### Staged-only changes

Prompt: “Review what I have staged before I commit.”

Select `--staged`. Report `HEAD -> index` identity and exclude unstaged edits.
Inspect cited sources and recommend tests from evidence-linked and inferred
collections without claiming execution.

### Branch comparison

Prompt: “What does this branch affect compared with main?”

Verify the repository and local `main` ref, then use `--base main`. Report the
requested ref, its resolved commit, merge base, and `HEAD`. Do not fetch, switch
branches, or include index/worktree changes.

### Partially staged file

Prompt: “Review my staged patch; I also kept extra edits unstaged in one file.”

Use `--staged`. Treat the index blob as the new comparison side and current
working-tree bytes as separate evidence. Do not silently review the third
working-tree version or call an unresolved mapping exact.

### Stale mappings

Prompt: “The report found an old-side symbol but says the workspace is stale.”

Retain an exact blob-matched old-side mapping with its cited provenance while
reporting freshness limitations. Keep mismatched new/third-version files
unresolved and recommend the emitted rescan action; do not claim it was run.

### No changes

Prompt: “Review the staged impact,” when the index matches `HEAD`.

Report the successful empty `HEAD -> index` comparison. Do not require graph
access and do not conclude that the repository or working tree is safe or clean.

### Retained task checkpoint

Prompt: “Review the uncommitted impact retained for change `CHANGE_ID`, run
`RUN_ID`.”

Read `change show CHANGE_ID`, then `change evidence RUN_ID`. Confirm the run
belongs to that task and select the retained task-window report. Cite its
before/after checkpoint IDs and every per-repository member report. Separately
cite the cumulative-session report when useful, never as task ownership. Do not
inspect current bytes as historical content or run the profile again.

### Retained session checkpoint

Prompt: “Review the combined uncommitted session `SESSION_ID` from `RUN_ID`.”

Read `change session show SESSION_ID`, then `change evidence RUN_ID`. Confirm a
session-scoped run and select its cumulative-session report. Cite the session
initial and candidate checkpoint IDs, each repository's identity/provenance, and
cross-repository coverage gaps. A stale/missing mapping or linked/inferred test
remains advisory; the impact report itself never proves a test ran.

### Missing retained evidence

Prompt: “Review task `CHANGE_ID`, but it has no recorded run or capture.”

Report the missing task/run/checkpoint/impact reference exactly as observed.
Do not stage, commit, execute gates, guess a ref, create a replacement session,
or treat the current working tree as the missing historical before/after state.

### Should not trigger

Prompt: “Find every textual occurrence of `authorize_payment`.”

Do not use this skill. Use an exact text-search workflow; the request does not
ask for a Git comparison or cited change-impact review.
