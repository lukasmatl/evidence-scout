from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

Vector = tuple[float, ...]


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str:
        """Stable provider/model identifier stored with generated vectors."""

    @property
    def dimensions(self) -> int:
        """Number of float values returned by this provider."""

    def embed_text(self, text: str) -> Vector:
        """Return an embedding vector for text."""


@dataclass(frozen=True)
class LocalEmbeddingProvider:
    dimensions: int = 128
    model_id: str = "code-scout-local-hash-v1"

    def __post_init__(self) -> None:
        if self.dimensions <= 0:
            msg = "Embedding dimensions must be greater than zero"
            raise ValueError(msg)

    def embed_text(self, text: str) -> Vector:
        vector = [0.0] * self.dimensions
        for feature, weight in _features(text):
            digest = hashlib.blake2b(
                feature.encode("utf-8"),
                digest_size=16,
                person=b"code-scout-emb",
            ).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 == 0 else -1.0
            vector[index] += sign * weight

        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0.0:
            return tuple(vector)
        return tuple(value / magnitude for value in vector)


def cosine_similarity(left: Vector, right: Vector) -> float:
    if len(left) != len(right):
        msg = "Cosine similarity requires vectors with matching dimensions"
        raise ValueError(msg)

    left_magnitude = math.sqrt(sum(value * value for value in left))
    right_magnitude = math.sqrt(sum(value * value for value in right))
    if left_magnitude == 0.0 or right_magnitude == 0.0:
        return 0.0

    dot_product = sum(
        left_value * right_value
        for left_value, right_value in zip(left, right, strict=True)
    )
    return dot_product / (left_magnitude * right_magnitude)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _features(text: str) -> tuple[tuple[str, float], ...]:
    tokens = _TOKEN_RE.findall(text.lower())
    features: list[tuple[str, float]] = []
    for token in tokens:
        features.append((f"token:{token}", 1.0))
        features.extend((f"tri:{ngram}", 0.25) for ngram in _trigrams(token))
    return tuple(features)


def _trigrams(token: str) -> tuple[str, ...]:
    if len(token) < 3:
        return ()
    return tuple(token[index : index + 3] for index in range(len(token) - 2))


__all__ = [
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "Vector",
    "cosine_similarity",
]
