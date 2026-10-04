from __future__ import annotations

import code_scout.core.errors as errors
from code_scout.core.errors import (
    CodeScoutError,
    ConfigError,
    ExportError,
    ExtractionError,
    StorageError,
)
from tests.identity_boundaries import RETIRED_ERROR_NAME


def test_public_errors_share_the_code_scout_base_class() -> None:
    assert issubclass(ConfigError, CodeScoutError)
    assert issubclass(StorageError, CodeScoutError)
    assert issubclass(ExtractionError, CodeScoutError)
    assert issubclass(ExportError, CodeScoutError)
    assert not hasattr(errors, RETIRED_ERROR_NAME)
