# Change-verification workflow

Code Scout can retain and verify a dirty, explicitly selected workspace without requiring a commit, an index, a scan, or a model. This is a local-cooperative workflow: it records exact bytes and detects drift relative to its receipts, but it is not a sandbox or a boundary against another same-user process.

The disposable fixture at [`tests/fixtures/change-verification`](../tests/fixtures/change-verification) demonstrates the complete P0 lifecycle. It has two editable repositories (`api`, `client`) and an immutable reference repository (`schemas`). It uses only `git`, `sh`, and `python3`; no network service, Code Scout graph index, extractor, or model configuration is involved.

## Run the disposable example

Run this from a checkout of Code Scout on macOS or Linux. `python3` and Git must be available on `PATH`; the fixture gates deliberately use no package manager. The runner rejects unsupported platforms before it starts a gate. Copying into a fresh temporary directory keeps all generated Git and receipt state disposable:

```sh
fixture=tests/fixtures/change-verification
workspace=$(mktemp -d "${TMPDIR:-/tmp}/code-scout-verification.XXXXXX")
rmdir "$workspace"
sh "$fixture/bootstrap.sh" "$workspace"
uv run code-scout change session begin --workspace "$workspace" \
  --repo api=api --repo client=client --reference-repo schemas=schemas \
  --policy verification.toml --profile demo --format json
```

Save the returned `session_id` as `SESSION`. Bootstrap commits each fixture's starting contents once, then deliberately leaves dirty edits in both editable repositories and a dirty `api/src/legacy.txt`. Do not commit, stage, stash, or reset after `session begin`: every later comparison is a retained checkpoint, not a convenient Git boundary.

The frozen profile gives every selected file one owner. Repository files belong to their declared repository; `integration/check_contracts.py` is a workspace input and protected workspace input; the selected policy source is independently measured. `schemas` participates in the aggregate candidate and final cross-repository gate, but as a `reference` member it must remain at exactly its admitted contents. Do not edit it in this example.

Start task A and make the deliberately incomplete cross-repository change:

```sh
uv run code-scout change begin A --session "$SESSION" --workspace "$workspace" --format json
# Save the returned change_id as A.
rm "$workspace/api/src/legacy.txt"
printf 'api=1\ndirty-before-session=yes\ncontract=trial\n' > "$workspace/api/src/state.txt"
uv run code-scout change verify "$A" --workspace "$workspace" --format json
```

`api-check` and `client-check` pass, while `cross-repository-contract` fails: the client has not declared the contract and the API value is `trial`. The failed run is retained and can be inspected with `change evidence RUN_ID`; it is not discarded when the same active attempt is repaired.

Repair task A, rerun the whole frozen profile, and retain the successful run ID as `RUN_A`:

```sh
printf 'api=1\ndirty-before-session=yes\ncontract=ready\n' > "$workspace/api/src/state.txt"
printf 'client=1\ndirty-before-session=yes\ncontract=ready\n' > "$workspace/client/src/state.txt"
uv run code-scout change verify "$A" --workspace "$workspace" --format json
```

Before finishing, demonstrate candidate binding. Changing any selected content after `RUN_A` makes `change finish` reject that run; restore the exact candidate before finishing it:

```sh
printf 'api=1\ndirty-before-session=yes\ncontract=ready\nextra=unverified\n' > "$workspace/api/src/state.txt"
uv run code-scout change finish "$A" --run "$RUN_A" --workspace "$workspace"
# Rejected: the live candidate no longer matches RUN_A.
printf 'api=1\ndirty-before-session=yes\ncontract=ready\n' > "$workspace/api/src/state.txt"
uv run code-scout change finish "$A" --run "$RUN_A" --workspace "$workspace" --format json
```

`legacy.txt` is absent live, but the task-window comparison still retains its before bytes and source digest. Use `change show "$A" --format json` to inspect that deletion; a current Git diff cannot recover the original dirty baseline if it was never committed.

Task B intentionally overlaps A's API file and also edits the client. Its baseline is the actual cumulative workspace after A, not an inferred ownership boundary:

```sh
uv run code-scout change begin B --session "$SESSION" --workspace "$workspace" --format json
# Save the returned change_id as B.
printf 'api=1\ndirty-before-session=yes\ncontract=ready\nfeature=b\n' > "$workspace/api/src/state.txt"
printf 'client=1\ndirty-before-session=yes\ncontract=ready\nfeature=b\n' > "$workspace/client/src/state.txt"
uv run code-scout change verify "$B" --workspace "$workspace" --format json
# Save the successful run_id as RUN_B, then:
uv run code-scout change finish "$B" --run "$RUN_B" --workspace "$workspace" --format json
uv run code-scout change show "$A" --workspace "$workspace" --format json
```

The final `show` keeps A's completion receipt historical but reports current applicability as `different`, because B changed the aggregate contents. It does not reopen A, transfer ownership, or make B's task window include all earlier dirty work. A retry of `change finish "$A"` returns the historical receipt with that applicability; an initial finish with later edits is rejected as shown above.

With no active attempt, the final gate run is session-scoped and again executes all three declared gates against all three members. Old task passes cannot be combined to substitute for it:

```sh
uv run code-scout change session verify "$SESSION" --workspace "$workspace" --format json
# Save the returned run_id as FINAL_RUN.
uv run code-scout change session finish "$SESSION" --run "$FINAL_RUN" --workspace "$workspace" --format json
```

The final integration gate checks the API, client, and unchanged schema bytes. Session finish writes a historical receipt only when `FINAL_RUN` is verified, fresh for the session lifecycle, and still matches the locked live candidate. It never commits, restores checkpoints, changes a Ralph task queue, or marks a story complete. The finished session rejects attempts and further runs; start a new session for later dirty work.

## Reusing profile evidence for story commands

The fixture's shared story-command map in
[`tests/fixtures/change-verification/README.md`](../tests/fixtures/change-verification/README.md)
aligns `api-check`, `client-check`, and `cross-repository-contract` with their
exact profile commands. The repaired `RUN_A` path above is one execution path:
it supplies command evidence for a criterion that names the same gate and
candidate, without running those commands twice. It does not prove unrelated
story requirements. For example, “the API task included a user-visible release
note” remains unmet/unobserved despite a green `demo` profile because neither a
gate nor a retained inspection observes it.

## What the comparisons mean

| View | Baseline and candidate | What it cannot establish |
| --- | --- | --- |
| Task window | Attempt `before` checkpoint → its run candidate | Exclusive ownership; manual/intervening edits are observed, not attributed. |
| Cumulative session | Session initial checkpoint → run candidate | A Git history or a clean tree. |
| Recorded Git diff | Recorded `HEAD` → current content per repository | Dirty pre-session baseline bytes or aggregate workspace input bytes. |
| Historical receipt | Frozen policy + retained candidate at the time of a run | That later live contents remain verified. |

`VERIFIED` means only that required gates from one frozen profile passed for one retained aggregate candidate with required evidence. It does not imply complete test coverage, every test's execution, requirement completion, release safety, or a graph-backed impact conclusion. Missing graph evidence is advisory and does not prevent this deterministic no-index/no-model path.

## Single-repository variant

The same session, checkpoint, attempt, run, and receipt contracts apply with one editable member. Use the fixture's `single-repo.toml`; it has the same frozen profile shape and one repository gate:

```sh
single=$(mktemp -d "${TMPDIR:-/tmp}/code-scout-verification-single.XXXXXX")
rmdir "$single"
sh "$fixture/bootstrap.sh" "$single"
uv run code-scout change session begin --workspace "$single" \
  --repo api=api --policy single-repo.toml --profile demo --format json
# Save SESSION_ONE; then begin, edit, verify, finish, and session-verify as above.
```

The copied `client` and `schemas` directories are outside this session's declared scope. They are not silently included just because they share a parent.

## Shared-directory limits

Admission and lifecycle operations lock declared Git worktree identities in a canonical order and capture repeatedly to reject unstable reads. This coordinates Code Scout sessions using the same worktree; it does not serialize arbitrary editors, Git commands, or other tools. A concurrent edit can produce an unstable-capture error or `current_applicability=unknown`; rerun only after the workspace is stable. Locks do not provide cross-user or hostile-process protection.

The fixture and this guide are original Code Scout documentation and fixture code. They adapt no ESF source. The implementation ports concepts from ESF under the project’s documented provenance boundary; any future copied ESF code or substantial tests must preserve its MIT and Machinist attribution notices.
