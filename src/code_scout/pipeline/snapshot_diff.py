"""Pure, deterministic comparisons of retained snapshot observations.

This module deliberately operates on the immutable payload, not the current
workspace or source-revision tables.  A diff therefore says what two scans
recorded; it does not make a claim about the current filesystem.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from code_scout.storage.snapshot_payloads import SnapshotPayload


class SnapshotComparisonError(ValueError):
    """Raised when a historical observation cannot honestly be compared."""


def compare_snapshot_payloads(
    before_payload: SnapshotPayload | Mapping[str, Any] | None,
    after_payload: SnapshotPayload | Mapping[str, Any] | None,
    *,
    before_snapshot_id: str,
    before_content_sha256: str | None,
    after_snapshot_id: str,
    after_content_sha256: str | None,
) -> dict[str, Any]:
    """Compare two retained payloads without reading live state.

    Facts are paired by their repository-qualified entity or relation identity,
    never by the content-derived active-fact ID or source line.  The returned
    dictionaries are JSON-ready and their collections are stable ordered.
    """
    before = _payload_dict(before_payload, before_snapshot_id)
    after = _payload_dict(after_payload, after_snapshot_id)
    before_records = _records_by_identity(before)
    after_records = _records_by_identity(after)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    pairing_uncertainties: list[dict[str, Any]] = []
    all_keys = sorted(set(before_records) | set(after_records))
    for key in all_keys:
        earlier = before_records.get(key, [])
        later = after_records.get(key, [])
        _unchanged, earlier_remaining, later_remaining = _match_unchanged(
            earlier, later
        )
        # Exact matches are deliberately discarded: they have no delta.  Pair a
        # changed observation only when its provider/source identity identifies a
        # single residual on each side.  List position is not evidence of a
        # relationship.
        changed_pairs, earlier_remaining, later_remaining, ambiguity = (
            _match_changed_by_provider_source(earlier_remaining, later_remaining)
        )
        pairing_uncertainties.extend(ambiguity)
        for before_record, after_record in changed_pairs:
            delta = _modified_record(before_record, after_record)
            if delta is not None:
                modified.append(delta)
        for record in later_remaining:
            added.append(
                {"identity": record["durable_identity"], "after": _reference(record)}
            )
        for record in earlier_remaining:
            removed.append(
                {
                    "identity": record["durable_identity"],
                    "before": _reference(record),
                    "status": "absent_from_later_snapshot",
                    "interpretation": (
                        "Absent from the later snapshot; this is not proof that "
                        "the software deleted the record."
                    ),
                }
            )

    scope = _value_difference(before["scan_scope"], after["scan_scope"])
    coverage = _value_difference(before["coverage"], after["coverage"])
    uncertainties = pairing_uncertainties + _coverage_uncertainties(
        before["coverage"], after["coverage"]
    )
    if scope is not None:
        uncertainties.append(
            {
                "kind": "scope_changed",
                "message": (
                    "Snapshot scan scopes differ; record absence may reflect "
                    "different extraction selection."
                ),
            }
        )
    return {
        "before_snapshot": {
            "id": before_snapshot_id,
            "content_sha256": before_content_sha256,
        },
        "after_snapshot": {
            "id": after_snapshot_id,
            "content_sha256": after_content_sha256,
        },
        "added": added,
        "removed": removed,
        "modified": modified,
        "scope_difference": scope,
        "coverage_difference": coverage,
        "uncertainties": uncertainties,
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
        },
    }


# A short alias reads naturally at the storage/command boundary in the next
# story, while keeping the implementation independently testable here.
compare_snapshots = compare_snapshot_payloads


def _payload_dict(
    payload: SnapshotPayload | Mapping[str, Any] | None, snapshot_id: str
) -> dict[str, Any]:
    if payload is None:
        raise SnapshotComparisonError(
            f"Snapshot {snapshot_id} has historical content unavailable; "
            "it cannot be compared."
        )
    value = payload.to_dict() if isinstance(payload, SnapshotPayload) else dict(payload)
    required = {
        "active_facts",
        "resolved_entities",
        "revision_references",
        "scan_scope",
        "coverage",
    }
    missing = sorted(required - set(value))
    if missing:
        raise SnapshotComparisonError(
            f"Snapshot {snapshot_id} has an unsupported historical payload "
            f"missing: {', '.join(missing)}."
        )
    return value


def _records_by_identity(payload: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    repository_lookup = _repository_lookup(payload)
    revision_lookup = {
        item["id"]: item
        for item in payload["revision_references"]
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_fact in payload["active_facts"]:
        if not isinstance(raw_fact, Mapping):
            continue
        fact = dict(raw_fact)
        durable_identity = _durable_identity(fact, repository_lookup, revision_lookup)
        key = _canonical(durable_identity)
        records[key].append({"fact": fact, "durable_identity": durable_identity})
    for values in records.values():
        values.sort(key=lambda item: _canonical(_comparison_value(item["fact"])))
    return records


def _match_unchanged(
    earlier: list[dict[str, Any]], later: list[dict[str, Any]]
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Consume identical normalized observations, including duplicate copies."""
    before_by_value: dict[str, list[dict[str, Any]]] = defaultdict(list)
    after_by_value: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in earlier:
        before_by_value[_normalized_observation(record)].append(record)
    for record in later:
        after_by_value[_normalized_observation(record)].append(record)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    before_remaining: list[dict[str, Any]] = []
    after_remaining: list[dict[str, Any]] = []
    for value in sorted(set(before_by_value) | set(after_by_value)):
        old, new = before_by_value[value], after_by_value[value]
        count = min(len(old), len(new))
        pairs.extend(zip(old[:count], new[:count], strict=True))
        before_remaining.extend(old[count:])
        after_remaining.extend(new[count:])
    return pairs, _ordered_records(before_remaining), _ordered_records(after_remaining)


def _match_changed_by_provider_source(
    earlier: list[dict[str, Any]], later: list[dict[str, Any]]
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Pair only residual observations with an unambiguous stable source."""
    before_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    after_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in earlier:
        before_by_source[_provider_source_identity(record)].append(record)
    for record in later:
        after_by_source[_provider_source_identity(record)].append(record)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    before_remaining: list[dict[str, Any]] = []
    after_remaining: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []
    for source in sorted(set(before_by_source) | set(after_by_source)):
        old, new = before_by_source[source], after_by_source[source]
        if len(old) == len(new) == 1:
            pairs.append((old[0], new[0]))
            continue
        before_remaining.extend(old)
        after_remaining.extend(new)
        if old and new:
            uncertainties.append(
                {
                    "kind": "ambiguous_observation_pairing",
                    "identity": old[0]["durable_identity"],
                    "provider_source": json.loads(source),
                    "message": (
                        "Multiple changed observations share a provider/source "
                        "identity, so they were left as unpaired additions and "
                        "removals rather than being reported as modifications."
                    ),
                }
            )
    return (
        pairs,
        _ordered_records(before_remaining),
        _ordered_records(after_remaining),
        uncertainties,
    )


def _normalized_observation(record: Mapping[str, Any]) -> str:
    return _canonical(_comparison_value(record["fact"]))


def _provider_source_identity(record: Mapping[str, Any]) -> str:
    """Return source identity without volatile revisions or source coordinates."""
    fact = record["fact"]
    evidence_sources = []
    for evidence in fact.get("evidence", ()):
        if not isinstance(evidence, Mapping):
            continue
        metadata = evidence.get("metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        evidence_sources.append(
            {
                "repository_id": metadata.get("repository_id"),
                "file_path": evidence.get("file_path"),
                "extractor_name": evidence.get("extractor_name"),
            }
        )
    return _canonical(
        {
            "extractor": fact.get("extractor"),
            "repository_id": record["durable_identity"].get("repository_id"),
            "source_file": fact.get("source_file"),
            "evidence_sources": sorted(evidence_sources, key=_canonical),
        }
    )


def _ordered_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda item: _canonical(_comparison_value(item["fact"])))


def _repository_lookup(payload: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    entities = payload["resolved_entities"]
    files = {
        item.get("id"): item.get("repository_id")
        for item in entities.get("files", ())
        if isinstance(item, Mapping)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("repository_id"), str)
    }
    lookup: dict[tuple[str, str], str] = {
        ("repository", item["id"]): item["id"]
        for item in entities.get("repositories", ())
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    lookup.update(
        {("file", identifier): repository for identifier, repository in files.items()}
    )
    for item in entities.get("symbols", ()):
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            repository = files.get(item.get("file_id"))
            if repository:
                lookup[("symbol", item["id"])] = repository
    for entity_type in ("services", "contracts", "concepts", "decisions"):
        for item in entities.get(entity_type, ()):
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                repository = item.get("repository_id")
                if isinstance(repository, str):
                    lookup[(entity_type.removesuffix("s"), item["id"])] = repository
    return lookup


def _durable_identity(
    fact: Mapping[str, Any],
    repositories: Mapping[tuple[str, str], str],
    revisions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    identity = fact.get("identity", {})
    source_repository = _source_repository(fact, revisions)
    if (
        isinstance(identity, Mapping)
        and "entity_type" in identity
        and "entity_id" in identity
    ):
        entity_type, entity_id = (
            str(identity["entity_type"]),
            str(identity["entity_id"]),
        )
        return {
            "kind": "entity",
            "entity_type": entity_type,
            "repository_id": repositories.get(
                (entity_type, entity_id), source_repository
            ),
            "entity_id": entity_id,
            "extractor": fact.get("extractor"),
        }
    if isinstance(identity, Mapping) and {"subject", "relation", "object"} <= set(
        identity
    ):
        return {
            "kind": "relation",
            "subject": _durable_reference(
                identity["subject"], repositories, source_repository
            ),
            "relation": identity["relation"],
            "object": _durable_reference(
                identity["object"], repositories, source_repository
            ),
            "extractor": fact.get("extractor"),
        }
    # Legacy payloads without the durable shape cannot safely be paired by ID.
    return {"kind": "unpaired_legacy_fact", "id": fact.get("id")}


def _durable_reference(
    reference: Any,
    repositories: Mapping[tuple[str, str], str],
    source_repository: str | None,
) -> dict[str, Any]:
    value = reference if isinstance(reference, Mapping) else {}
    entity_type, entity_id = str(value.get("type")), str(value.get("id"))
    return {
        "type": entity_type,
        "repository_id": repositories.get((entity_type, entity_id), source_repository),
        "id": entity_id,
    }


def _source_repository(
    fact: Mapping[str, Any], revisions: Mapping[str, Mapping[str, Any]]
) -> str | None:
    for evidence in fact.get("evidence", ()):  # facts may retain several observations
        if not isinstance(evidence, Mapping):
            continue
        metadata = evidence.get("metadata", {})
        if not isinstance(metadata, Mapping):
            continue
        repository = metadata.get("repository_id")
        if isinstance(repository, str):
            return repository
        revision_id = metadata.get("source_revision_id")
        revision = revisions.get(revision_id) if isinstance(revision_id, str) else None
        if revision and isinstance(revision.get("repository_id"), str):
            return str(revision["repository_id"])
    return None


def _modified_record(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any] | None:
    before_fact, after_fact = before["fact"], after["fact"]
    property_changes = _mapping_changes(
        before_fact.get("properties", {}), after_fact.get("properties", {})
    )
    changes: dict[str, Any] = {}
    if property_changes:
        changes["properties"] = property_changes
    for field in (
        "confidence",
        "conflict_metadata",
        "extractor_version",
        "source_file",
    ):
        delta = _value_difference(before_fact.get(field), after_fact.get(field))
        if delta is not None:
            changes[field] = delta
    source_revisions = _source_revision_changes(before_fact, after_fact)
    if source_revisions:
        changes["source_revisions"] = source_revisions
    assessments = _assessment_changes(before_fact, after_fact)
    if assessments:
        changes["assessment_transitions"] = assessments
    evidence_delta = _value_difference(
        _evidence_comparison(before_fact), _evidence_comparison(after_fact)
    )
    if evidence_delta is not None:
        changes["evidence"] = evidence_delta
    if not changes:
        return None
    return {
        "identity": before["durable_identity"],
        "before": _reference(before),
        "after": _reference(after),
        "changes": changes,
    }


def _reference(record: Mapping[str, Any]) -> dict[str, Any]:
    fact = record["fact"]
    return {
        "fact_id": fact.get("id"),
        "source_file": fact.get("source_file"),
        "evidence": _evidence_comparison(fact),
    }


def _source_revision_changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    old = _source_references(before)
    new = _source_references(after)
    return [
        {
            "before": old[index] if index < len(old) else None,
            "after": new[index] if index < len(new) else None,
        }
        for index in range(max(len(old), len(new)))
        if (old[index] if index < len(old) else None)
        != (new[index] if index < len(new) else None)
    ]


def _source_references(fact: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for evidence in fact.get("evidence", ()):
        metadata = evidence.get("metadata", {}) if isinstance(evidence, Mapping) else {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        values.append(
            {
                "repository_id": metadata.get("repository_id"),
                "source_revision_id": metadata.get("source_revision_id"),
                "content_sha256": metadata.get("content_sha256"),
                "unavailable_reason": metadata.get(
                    "source_revision_unavailable_reason"
                ),
            }
        )
    return sorted(values, key=_canonical)


def _assessment_changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    old, new = _assessments(before), _assessments(after)
    return [
        {
            "before": old[index] if index < len(old) else None,
            "after": new[index] if index < len(new) else None,
        }
        for index in range(max(len(old), len(new)))
        if (old[index] if index < len(old) else None)
        != (new[index] if index < len(new) else None)
    ]


def _assessments(fact: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for value in (fact.get("properties", {}), fact.get("conflict_metadata", {})):
        if isinstance(value, Mapping) and isinstance(value.get("assessment"), Mapping):
            values.append(dict(value["assessment"]))
    for evidence in fact.get("evidence", ()):
        metadata = evidence.get("metadata", {}) if isinstance(evidence, Mapping) else {}
        if isinstance(metadata, Mapping) and isinstance(
            metadata.get("assessment"), Mapping
        ):
            values.append(dict(metadata["assessment"]))
    return sorted(values, key=_canonical)


def _evidence_comparison(fact: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = []
    for evidence in fact.get("evidence", ()):
        if not isinstance(evidence, Mapping):
            continue
        metadata = evidence.get("metadata", {})
        values.append(
            {
                "file_path": evidence.get("file_path"),
                "extractor_name": evidence.get("extractor_name"),
                "confidence": evidence.get("confidence"),
                "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
                "source": _source_references({"evidence": (evidence,)})[0],
            }
        )
    return sorted(values, key=_canonical)


def _comparison_value(fact: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fact.items() if key not in {"id", "identity"}}


def _mapping_changes(before: Any, after: Any) -> dict[str, dict[str, Any]]:
    old = before if isinstance(before, Mapping) else {}
    new = after if isinstance(after, Mapping) else {}
    return {
        str(key): {"before": old.get(key), "after": new.get(key)}
        for key in sorted(set(old) | set(new), key=str)
        if old.get(key) != new.get(key)
    }


def _value_difference(before: Any, after: Any) -> dict[str, Any] | None:
    return None if before == after else {"before": before, "after": after}


def _coverage_uncertainties(before: Any, after: Any) -> list[dict[str, str]]:
    old, new = (
        before if isinstance(before, Mapping) else None,
        after if isinstance(after, Mapping) else None,
    )
    if old is None or new is None or not old or not new:
        return [
            {
                "kind": "coverage_unknown",
                "message": "One or both snapshots have unknown coverage.",
            }
        ]
    if old != new:
        return [
            {
                "kind": "coverage_changed",
                "message": (
                    "Coverage differs; absence may reflect what was scanned "
                    "rather than deletion."
                ),
            }
        ]
    return []


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
