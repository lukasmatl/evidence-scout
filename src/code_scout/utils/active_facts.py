from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_scout.models.facts import ActiveFact


def merge_active_facts_by_id(*facts: ActiveFact) -> tuple[ActiveFact, ...]:
    merged: dict[str, ActiveFact] = {}
    for fact in facts:
        existing = merged.get(fact.id)
        if existing is None:
            merged[fact.id] = fact
            continue
        merged[fact.id] = replace(
            existing,
            evidence=(*existing.evidence, *fact.evidence),
            confidence=max(existing.confidence, fact.confidence),
        )
    return tuple(merged.values())
