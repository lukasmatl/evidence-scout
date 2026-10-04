from __future__ import annotations

import hashlib
import posixpath
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from code_scout.utils.identifiers import slugify_identifier

EntityMarkdownType = Literal[
    "service",
    "contract",
    "concept",
    "repository",
    "decision",
]

ENTITY_MARKDOWN_FOLDERS: dict[EntityMarkdownType, str] = {
    "service": "services",
    "contract": "contracts",
    "concept": "concepts",
    "repository": "repositories",
    "decision": "decisions",
}

_DISPLAY_NAME_FIELDS: dict[EntityMarkdownType, tuple[str, ...]] = {
    "service": ("name",),
    "contract": ("name",),
    "concept": ("name",),
    "repository": ("name",),
    "decision": ("title", "name"),
}


def entity_markdown_path(
    entity_type: EntityMarkdownType,
    row: Mapping[str, Any],
) -> str:
    """Return the default relative markdown path for one entity row."""

    return _path_for_stem(entity_type, _base_stem(entity_type, row))


def entity_markdown_paths(
    entity_type: EntityMarkdownType,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Return deterministic unique relative markdown paths by entity id."""

    base_stems = {entity_id(row): _base_stem(entity_type, row) for row in rows}
    duplicate_stems = {
        stem for stem, count in Counter(base_stems.values()).items() if count > 1
    }

    resolved_stems: dict[str, str] = {}
    for row in sorted(rows, key=lambda item: entity_id(item)):
        row_id = entity_id(row)
        stem = base_stems[row_id]
        if (
            stem in duplicate_stems
            and _usable_display_name(entity_type, row) is not None
        ):
            stem = f"{stem}-{_entity_id_stem(row_id)}"
        resolved_stems[row_id] = stem

    resolved_stems = _dedupe_stems(resolved_stems)
    return {
        row_id: _path_for_stem(entity_type, stem)
        for row_id, stem in sorted(resolved_stems.items())
    }


def entity_markdown_link(
    *,
    from_page_path: str,
    target_entity_type: str,
    target_entity_id: str,
    label: str,
    paths_by_type: Mapping[str, Mapping[str, str]],
) -> str:
    """Render a relative markdown link to a generated entity page when known."""

    inline_label = _readable_inline_label(label, target_entity_id)
    if target_entity_type not in ENTITY_MARKDOWN_FOLDERS:
        return inline_label

    target_path = paths_by_type.get(target_entity_type, {}).get(target_entity_id)
    if target_path is None:
        return inline_label

    href = _relative_page_path(from_page_path, target_path)
    return f"[{_escape_link_label(inline_label)}]({href})"


def entity_id(row: Mapping[str, Any]) -> str:
    row_id = row.get("id")
    if not isinstance(row_id, str) or not row_id.strip():
        raise ValueError("entity row must include a non-empty string id")
    return row_id


def _base_stem(entity_type: EntityMarkdownType, row: Mapping[str, Any]) -> str:
    display_name = _usable_display_name(entity_type, row)
    if display_name is not None:
        return slugify_identifier(
            display_name, fallback=_entity_id_stem(entity_id(row))
        )
    return _entity_id_stem(entity_id(row))


def _usable_display_name(
    entity_type: EntityMarkdownType,
    row: Mapping[str, Any],
) -> str | None:
    for field in _DISPLAY_NAME_FIELDS[entity_type]:
        value = row.get(field)
        if isinstance(value, str) and slugify_identifier(value, fallback=""):
            return value
    return None


def _entity_id_stem(row_id: str) -> str:
    return slugify_identifier(row_id, fallback="entity")


def _dedupe_stems(stems_by_id: dict[str, str]) -> dict[str, str]:
    deduped = dict(stems_by_id)
    seen: dict[str, str] = {}
    for row_id, stem in sorted(deduped.items()):
        if stem not in seen:
            seen[stem] = row_id
            continue
        deduped[row_id] = f"{stem}-{_stable_suffix(row_id)}"

    if len(set(deduped.values())) != len(deduped):
        return _dedupe_stems(deduped)
    return deduped


def _stable_suffix(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def _path_for_stem(entity_type: EntityMarkdownType, stem: str) -> str:
    return f"{ENTITY_MARKDOWN_FOLDERS[entity_type]}/{stem}.md"


def _relative_page_path(from_page_path: str, target_page_path: str) -> str:
    start = posixpath.dirname(from_page_path) or "."
    relative = posixpath.relpath(target_page_path, start=start)
    if relative.startswith("../") or relative.startswith("./"):
        return relative
    return f"./{relative}"


def _readable_inline_label(label: str, fallback: str) -> str:
    if label.strip():
        return label
    return fallback


def _escape_link_label(label: str) -> str:
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
