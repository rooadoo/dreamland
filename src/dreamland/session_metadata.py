from __future__ import annotations

from collections.abc import Iterable

SESSION_ORIGIN_METADATA_KEY = "dreamland.session.origin"
SESSION_TAGS_METADATA_KEY = "dreamland.session.tags"
SESSION_LABEL_METADATA_KEY = "dreamland.session.label"
SESSION_METADATA_METADATA_KEY = "dreamland.session.metadata"
MCP_SESSION_ORIGIN = "mcp"


def normalize_session_tags(tags: Iterable[object] | None) -> tuple[str, ...]:
    if tags is None:
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        value = tag.strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


def session_tags_from_metadata(metadata: dict[str, object]) -> tuple[str, ...]:
    value = metadata.get(SESSION_TAGS_METADATA_KEY)
    if isinstance(value, str):
        return normalize_session_tags(value.split(","))
    if isinstance(value, (list, tuple, set, frozenset)):
        return normalize_session_tags(value)
    return ()


def session_label_from_metadata(metadata: dict[str, object]) -> str | None:
    value = metadata.get(SESSION_LABEL_METADATA_KEY)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def session_origin_from_metadata(metadata: dict[str, object]) -> str | None:
    value = metadata.get(SESSION_ORIGIN_METADATA_KEY)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def langfuse_tags_for_session(
    *,
    metadata: dict[str, object],
) -> list[str]:
    profile_name = state_profile_name_from_metadata(metadata)
    return list(
        normalize_session_tags(
            ("dreamland", profile_name, *session_tags_from_metadata(metadata))
        )
    )


def state_profile_name_from_metadata(metadata: dict[str, object]) -> str:
    """Return the selected saved world's identity for operational records."""
    value = metadata.get("state_profile.name")
    return value.strip() if isinstance(value, str) else ""
