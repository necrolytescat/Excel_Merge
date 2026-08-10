"""Canonical ETag and opaque cursor rules shared by M3 monitor APIs."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any


class MonitorCursorError(ValueError):
    pass


def canonical_json(data: Any) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def response_etag(data: dict[str, Any], *, exclude_as_of: bool = False) -> str:
    projection = dict(data)
    if exclude_as_of:
        projection.pop("as_of", None)
    digest = hashlib.sha256(canonical_json(projection)).hexdigest()
    return f'"{digest}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    expected = etag.removeprefix("W/")
    for candidate in if_none_match.split(","):
        token = candidate.strip()
        if token == "*" or token.removeprefix("W/") == expected:
            return True
    return False


def _filter_hash(filters: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(filters)).hexdigest()


def encode_cursor(
    *,
    scope: str,
    filters: dict[str, Any],
    sort_values: list[str],
) -> str:
    raw = canonical_json(
        {
            "v": 1,
            "scope": scope,
            "filter": _filter_hash(filters),
            "sort": sort_values,
        }
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(
    value: str,
    *,
    scope: str,
    filters: dict[str, Any],
    sort_size: int,
) -> list[str]:
    try:
        padding = "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MonitorCursorError("invalid monitor cursor") from error
    if (
        not isinstance(data, dict)
        or data.get("v") != 1
        or data.get("scope") != scope
        or data.get("filter") != _filter_hash(filters)
        or not isinstance(data.get("sort"), list)
        or len(data["sort"]) != sort_size
        or not all(isinstance(item, str) for item in data["sort"])
    ):
        raise MonitorCursorError("invalid monitor cursor")
    return data["sort"]
