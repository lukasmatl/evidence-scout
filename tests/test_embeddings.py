from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_scout.evidence.embeddings import LocalEmbeddingProvider, cosine_similarity


def test_local_embedding_provider_exposes_model_id_and_dimensions() -> None:
    provider = LocalEmbeddingProvider(dimensions=32)

    vector = provider.embed_text("BillingService handles checkout payments.")

    assert provider.model_id == "code-scout-local-hash-v1"
    assert provider.dimensions == 32
    assert len(vector) == 32
    assert any(value != 0.0 for value in vector)


def test_local_embedding_provider_is_deterministic_across_process_runs() -> None:
    first_vector = _embed_in_subprocess("BillingService handles checkout payments.")
    second_vector = _embed_in_subprocess("BillingService handles checkout payments.")

    assert first_vector == second_vector


def test_local_embedding_provider_handles_empty_text() -> None:
    provider = LocalEmbeddingProvider(dimensions=16)

    vector = provider.embed_text("")

    assert vector == (0.0,) * 16
    assert cosine_similarity(vector, provider.embed_text("billing")) == 0.0


def test_cosine_similarity_orders_related_text_above_unrelated_text() -> None:
    provider = LocalEmbeddingProvider(dimensions=128)
    query = provider.embed_text("billing checkout payments")
    related = provider.embed_text(
        "Billing checkout payments are handled by BillingService."
    )
    partially_related = provider.embed_text(
        "Checkout pages render payment confirmation state."
    )
    unrelated = provider.embed_text(
        "User profile avatars and notification preferences."
    )

    ranked = sorted(
        [
            ("unrelated", cosine_similarity(query, unrelated)),
            ("partially_related", cosine_similarity(query, partially_related)),
            ("related", cosine_similarity(query, related)),
        ],
        key=lambda result: result[1],
        reverse=True,
    )

    assert [label for label, _score in ranked] == [
        "related",
        "partially_related",
        "unrelated",
    ]


def test_local_embedding_provider_rejects_invalid_dimensions() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        LocalEmbeddingProvider(dimensions=0)


def test_cosine_similarity_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="matching dimensions"):
        cosine_similarity((1.0, 0.0), (1.0,))


def _embed_in_subprocess(text: str) -> list[float]:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from code_scout.evidence.embeddings import LocalEmbeddingProvider; "
                "print(json.dumps(LocalEmbeddingProvider(dimensions=32).embed_text("
                f"{text!r})))"
            ),
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env=environment,
    )
    return json.loads(result.stdout)
