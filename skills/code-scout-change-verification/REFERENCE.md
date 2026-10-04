# Change Verification Reference

Use this reference after selecting the dedicated verification skill. The
commands execute only the session's already-frozen profile; they do not perform
static discovery, scans, graph queries, or model calls.

## Admission and checkpoints

`change session begin` requires an explicit policy/profile and one or more
explicit Git members. `--repo ID=PATH` admits an editable member;
`--reference-repo ID=PATH` admits a reference member. Member IDs, roots, and
worktree identities must agree with the recorded session. Nested repositories
need separate explicit registration. Do not silently add, duplicate, or guess a
member.

The initial checkpoint retains live selected contents, including dirty files and
an unborn repository's no-`HEAD` state. Git provenance is reported separately;
neither a clean tree nor a commit is required. A reference member must remain
equal to its admitted contents. A protected policy/helper change, reference
drift, candidate drift, missing evidence, or contradictory identity is an
integrity finding: retain and report it. Never cure it by creating a replacement
session, recapturing a baseline, editing the policy, or making a required gate
optional.

The stored state is under `.code-scout/verification/`. It is runtime evidence,
not a substitute for a session ID supplied through an agent handoff. Validate
IDs with the three show/evidence commands before acting if identity is uncertain.

## Lifecycle decision table

| Situation | Action | Boundary to report |
| --- | --- | --- |
| New authorized work | Begin an explicit multi-member session, then `change begin LABEL --session SESSION_ID`. | Save session/change/checkpoint IDs and roles. |
| Dirty or unborn start | Admit it normally. | Initial checkpoint, not a fabricated Git baseline, is the start. |
| Repair after a failed gate | Keep the active attempt and rerun `change verify CHANGE_ID`. | Each run has its own candidate and receipt; no result reuse. |
| Stop/retry later | `change abandon CHANGE_ID`, then begin the next authorized attempt. | Abandon does not restore files or certify the task. |
| Continue a supplied task | `change show CHANGE_ID` and reuse its active attempt. | Missing/wrong ID is a blocker, never an excuse to reset. |
| Need historical/current evidence | `change show`, `change session show`, or `change evidence`. | Inspection is read-only; report historical outcome and current applicability separately. |
| Finish one task | Verify its active attempt, then `change finish CHANGE_ID --run RUN_ID`. | Candidate, session, policy, run, and live content must match. |
| All authoring complete and explicitly authorized | `change session verify`, then `change session finish`. | Fresh session run is required; old task receipts cannot be combined. |

An attempt's task-window comparison covers its retained before-checkpoint to its
run candidate. The cumulative comparison covers session initial checkpoint to
candidate. A recorded Git comparison is separate. Sequential edits, including
edits to the same file, are observed in the next task window but do not prove
exclusive ownership or retrospectively assign intervening manual edits. A task
started after work was already present can only attribute observations from its
recorded before-checkpoint forward.

## Gate and receipt boundaries

Each fresh `change verify` or `change session verify` executes every declared
gate sequentially against one captured aggregate candidate. Its profile and
gate IDs are the authority for executable checks. A single fresh run supplies
the receipt for a story criterion only when that criterion names the same
profile gate/command and its scope matches; do not duplicate exact commands just
to produce agent output. Focused tests run while developing are separate,
diagnostic observations. The final session run deliberately executes the full
profile again because it must bind the complete, later combined candidate.

`VERIFIED` means required gates passed for that retained candidate under that
frozen policy with required evidence retained. It does **not** establish every
story requirement, test execution/coverage, behavior outside declared gates,
release safety, or current applicability after later edits. Gate output prose
cannot alter the recorded outcome. `change evidence` validates retained evidence;
its zero exit only says inspection succeeded, even for a historical failed run.

Use these outcome distinctions in reports:

- `ERROR`: setup/execution/persistence interruption or essential evidence issue.
- `INVALIDATED`: policy, protected input, tool, or candidate drift.
- `FAILED`: a required gate failed or timed out.
- `INCOMPLETE`: a required gate or essential receipt is absent.
- `VERIFIED`: declared required gates passed with integrity and evidence.

## Agent report contract

For each story criterion, write a compact evidence map rather than a blanket
success claim:

| Story criterion | Supporting evidence | Status/limit |
| --- | --- | --- |
| Named command criterion | Profile ID, gate ID, run ID, candidate/policy digest, gate outcome | Supported only if the matching fresh gate passed. |
| Specific behavior or regression | Focused test name/result, retained inspection, or cited source review | State exactly what was observed; a green profile alone is insufficient. |
| Impact/caller/test candidate | Retained impact artifact or separately inspected source | Advisory unless the receipt records execution; missing graph evidence stays a gap. |
| No available evidence | None | Unmet or unobserved; name the next needed test/inspection. |

Include session/change/run/checkpoint IDs, member roles, policy and candidate
digests, run scope, required-gate outcomes, evidence/persistence gaps, advisory
impact limits, and current applicability. Mark missing acceptance evidence even
when the overall run is `VERIFIED`. Do not declare the story complete or update
Ralph/scheduler completion flags; that remains an external decision.

## Read-only and recovery boundaries

`change show`, `change session show`, and `change evidence` are inspection-only.
Do not run gates merely because a user asked to inspect a receipt, and do not
substitute staged/HEAD/current source for missing historical checkpoint data. A
live-source inspection can answer a new question but cannot reconstruct past
attribution.

If IDs, members, policy digest, checkpoint, or receipt disagree, stop the
lifecycle action and report the exact conflict. If gates modify files or a
workspace is unstable, report drift/invalidated evidence and wait for a stable,
authorized retry. Verification locks coordinate Code Scout operations but do not
prove authorship against arbitrary concurrent editors. No operation here stages,
commits, resets, restores checkpoints, scans, or invokes a model.
