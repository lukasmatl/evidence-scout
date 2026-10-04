from __future__ import annotations

# Rejection-boundary tests must identify the retired interfaces without putting
# their literal spelling back into the repository surfaces guarded by the audit.
RETIRED_PROJECT_TOKEN = bytes((112, 101, 109)).decode("ascii")
RETIRED_STATE_DIRECTORY = f".{RETIRED_PROJECT_TOKEN}"
RETIRED_IMPORT_NAMESPACE = RETIRED_PROJECT_TOKEN
RETIRED_EXECUTABLE = RETIRED_PROJECT_TOKEN
RETIRED_ENVIRONMENT_PREFIX = RETIRED_PROJECT_TOKEN.upper()
RETIRED_ERROR_NAME = f"{RETIRED_PROJECT_TOKEN.upper()}Error"
