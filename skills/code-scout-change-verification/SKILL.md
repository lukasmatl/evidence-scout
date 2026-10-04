---
name: code-scout-change-verification
description: Manage an explicit Code Scout workspace verification session, task checkpoints, frozen gates, and receipts without static search, scanning, or model-backed exploration. Use when asked to run required profile checks, begin/resume/abandon/finish a verification task, inspect a verification receipt, or verify a completed multi-repository workspace.
---

# Code Scout Change Verification

Use this skill for deterministic local workspace verification. It is distinct
from `code-scout -q ... --engine verify`, which is model-backed claim analysis,
and from Git staged/base impact review. Do not require a scan, static query,
model, staging, commit, clean Git tree, or populated graph index.

## Establish scope and identity

Before a state-changing command, identify the authorized workspace, policy and
profile, selected repository IDs/paths and roles, and any supplied session,
change, or run IDs. A session begins with explicit members: use `--repo` for
editable repositories and `--reference-repo` for repositories that must stay at
their admitted contents. Dirty work and repositories with no `HEAD` are valid
starts; checkpoints capture current files rather than inventing a commit.

```sh
code-scout change session begin --workspace /work/product \
  --repo api=api --repo client=client --reference-repo schemas=schemas \
  --policy verification.toml --profile default --format json
```

Record the returned `session_id` and admission checkpoint. Never infer members
from the current directory alone, recreate a session because an ID is missing,
or replace an unknown/contradictory ID with a new baseline. Inspect first:

```sh
code-scout change session show SESSION_ID --workspace /work/product --format json
code-scout change show CHANGE_ID --workspace /work/product --format json
code-scout change evidence RUN_ID --workspace /work/product --format json
```

These inspections are read-only. A successful inspection exit status means the
record was readable, not that its run passed or still applies. Report missing,
corrupt, contradictory, or currently different evidence explicitly.

## Task lifecycle

Start an authorized task only after session identity is confirmed:

```sh
code-scout change begin US-123 --session SESSION_ID --workspace /work/product --format json
code-scout change verify CHANGE_ID --workspace /work/product --format json
code-scout change finish CHANGE_ID --run RUN_ID --workspace /work/product --format json
```

Save the returned `change_id`, before-checkpoint, and fresh `run_id`. Task
verification runs the whole frozen profile once against one aggregate candidate;
use that receipt as evidence for matching command-based story criteria instead
of running identical project commands directly again. Focused development tests
remain useful diagnostics but are not a final receipt. A later final session run
is separate and reruns the entire frozen profile for the combined state.

Tasks are sequential observational windows, not file ownership. A later task
may edit the same file without a commit; its before-checkpoint is the actual
state after the preceding task. Retry a failed or repaired active attempt with a
new `change verify` run. To stop it without rollback, retain its evidence and
close it:

```sh
code-scout change abandon CHANGE_ID --workspace /work/product --format json
```

On resume, reuse the recorded active change and checkpoint. If observation
begins after edits already happened, begin a new attempt only for later observed
work and say that earlier work is unobserved; do not claim an earlier task
baseline or attribution.

## Finish the overall session only when authorized

Finishing a task is not authorization to finish the session. When the user has
authorized final combined-workspace verification and no task is active, run and
finish the session explicitly:

```sh
code-scout change session verify SESSION_ID --workspace /work/product --format json
code-scout change session finish SESSION_ID --run FINAL_RUN_ID --workspace /work/product --format json
```

Do not aggregate old task passes in place of this fresh run. Do not rebaseline,
weaken policy, disable gates, commit, stage, reset, or alter Ralph completion
flags after integrity drift or a failed/invalidated receipt. Report the result
and preserve the original IDs and checkpoints. Read [REFERENCE.md](REFERENCE.md)
for report requirements, result boundaries, and lifecycle limits.
