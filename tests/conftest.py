from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_default_graphify_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "code_scout.harvest.core.shutil.which",
        lambda _command: None,
        raising=False,
    )
