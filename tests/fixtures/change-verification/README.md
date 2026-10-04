# Change-verification fixture

`bootstrap.sh WORKSPACE` copies this fixture into an empty disposable directory,
initializes three local Git repositories, commits only their fixture baselines,
then introduces deliberately uncommitted API/client work and an uncommitted API
file intended for deletion. The `schemas` checkout is a reference member.

The walkthrough is in `docs/change-verification-workflow.md`. It must remain
unchanged after bootstrap; the scenario never writes the reference checkout.

## Shared story-command evidence

The `demo` profile intentionally names the reusable command checks below. One
fresh task run executes them once for the same candidate; an agent should cite
the matching gate ID instead of rerunning the identical command as a separate
story check.

| Shared story command | Profile gate ID | What a green gate establishes |
| --- | --- | --- |
| `python3 scripts/check.py` in `api` | `api-check` | The API state has the fixture's required prefix. |
| `python3 scripts/check.py` in `client` | `client-check` | The client state has the fixture's required prefix. |
| `python3 integration/check_contracts.py` | `cross-repository-contract` | API/client contract declarations match the unchanged reference schema. |

A green `demo` profile deliberately does **not** establish the story-specific
criterion “the API task included a user-visible release note”: no fixture gate
or retained inspection covers that requirement. Report it as unmet or
unobserved even when all three gates pass. `bootstrap.sh` remains runnable with
only Git, `sh`, and `python3`; it does not require a product implementation.
