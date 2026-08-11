"""Discover fixed-branch options for M3 without mutating SVN or endpoint config."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re
from threading import RLock
import time
from typing import Any
import urllib.parse

from app.schemas.svn import BranchCandidatesPayload, BranchMatchPayload, EndpointPayload
from app.services.svn_service import SVNService
from core.svn_provider import SVNProviderError


EndpointRecords = Callable[[], Sequence[Mapping[str, Any]]]
EndpointCatalog = Callable[[], Mapping[str, Mapping[str, Any]]]
ServerUrl = Callable[[], str]


def project_root_url(url: str) -> str:
    """Derive the project root from a configured Trunk_* URL."""
    parsed = urllib.parse.urlsplit(url.strip())
    path = parsed.path.rstrip("/")
    parts = path.split("/") if path else []
    last = urllib.parse.unquote(parts[-1]) if parts else ""
    if re.fullmatch(r"Trunk_[A-Za-z0-9_-]+", last, re.IGNORECASE):
        parts.pop()
        path = "/".join(parts) or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, "")
    )


def _pattern_regex(pattern: str) -> re.Pattern[str] | None:
    value = (pattern or "").strip()
    if not value:
        return None
    tokens: list[str] = []
    for index, char in enumerate(value):
        previous = value[index - 1] if index else ""
        following = value[index + 1] if index + 1 < len(value) else ""
        if (
            char.casefold() == "x"
            and (not previous or previous in "-_.")
            and (not following or following in "-_.")
        ):
            tokens.append(r"[0-9]+")
        else:
            tokens.append(re.escape(char))
    return re.compile(r"^" + "".join(tokens) + r"$", re.IGNORECASE)


def configured_branch_matches(
    branches: Sequence[str],
    *,
    base_url: str,
    catalog: Mapping[str, Mapping[str, Any]],
    region: str | None,
) -> list[BranchMatchPayload]:
    selected_region = region.strip().upper() if region else ""
    matches: list[BranchMatchPayload] = []
    for code, config in catalog.items():
        if selected_region and code.upper() != selected_region:
            continue
        trunk = str(config.get("trunk_branch", "")).strip()
        fix_regex = _pattern_regex(str(config.get("fix_pattern", "")))
        for branch in branches:
            if trunk and branch.casefold() == trunk.casefold():
                matches.append(
                    BranchMatchPayload(
                        region=code,
                        track="DEV",
                        label=f"{config.get('display_name', code)} · {branch}",
                        branch=branch,
                        url=(
                            f"{base_url.rstrip('/')}/"
                            f"{urllib.parse.quote(branch, safe='~:@-_.')}"
                        ),
                        match_type="TRUNK",
                    )
                )
            if fix_regex and fix_regex.fullmatch(branch):
                matches.append(
                    BranchMatchPayload(
                        region=code,
                        track="FIX",
                        label=f"{config.get('display_name', code)} · {branch}",
                        branch=branch,
                        url=(
                            f"{base_url.rstrip('/')}/branches/"
                            f"{urllib.parse.quote(branch, safe='~:@-_.')}"
                        ),
                        match_type="FIX",
                    )
                )
    return sorted(
        matches,
        key=lambda item: (item.region, item.match_type, item.branch.casefold()),
    )


def discover_branch_candidates(
    service: SVNService,
    *,
    url: str,
    revision: int | str = "HEAD",
    catalog: Mapping[str, Mapping[str, Any]],
    region: str | None = None,
) -> BranchCandidatesPayload:
    base_url = project_root_url(url)
    payload = EndpointPayload(url=base_url, revision=revision)
    root_entries = service.children(payload)
    root_dirs = sorted(
        {
            entry.path.rsplit("/", 1)[-1]
            for entry in root_entries
            if entry.kind == "dir"
        }
    )
    branch_names: list[str] = []
    if any(name.casefold() == "branches" for name in root_dirs):
        branch_entries = service.children(payload, "branches")
        branch_names = sorted(
            {
                entry.path.rsplit("/", 1)[-1]
                for entry in branch_entries
                if entry.kind == "dir"
            }
        )
    matches = configured_branch_matches(
        root_dirs + branch_names,
        base_url=base_url,
        catalog=catalog,
        region=region,
    )
    return BranchCandidatesPayload(
        base_url=base_url,
        revision=revision,
        trunk_branches=sorted(
            {item.branch for item in matches if item.match_type == "TRUNK"},
            key=str.casefold,
        ),
        fix_branches=sorted(
            {item.branch for item in matches if item.match_type == "FIX"},
            key=str.casefold,
        ),
        matches=matches,
    )


def endpoint_id_for_match(match: BranchMatchPayload) -> str:
    value = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        f"{match.region}_{match.track}_{match.branch}",
    )
    return value[:128]


def endpoint_record_for_match(match: BranchMatchPayload) -> dict[str, Any]:
    return {
        "id": endpoint_id_for_match(match),
        "region": match.region,
        "track": match.track,
        "label": match.label,
        "url": match.url,
        "logical_scopes": ["TABLE"],
        "physical_path_filters": {},
        "enabled": True,
    }


class MonitorEndpointCatalog:
    """Merge configured endpoints with read-only SVN branch discovery results."""

    def __init__(
        self,
        service: SVNService,
        *,
        server_url: ServerUrl,
        endpoint_catalog: EndpointCatalog,
        endpoint_registry: EndpointRecords,
        cache_seconds: float = 60.0,
    ) -> None:
        self.service = service
        self.server_url = server_url
        self.endpoint_catalog = endpoint_catalog
        self.endpoint_registry = endpoint_registry
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._lock = RLock()
        self._cache_key: tuple[str, str] | None = None
        self._cached_matches: tuple[BranchMatchPayload, ...] = ()
        self._cache_expires_at = 0.0

    def _matches(self) -> tuple[BranchMatchPayload, ...]:
        url = self.server_url().strip()
        catalog = self.endpoint_catalog()
        catalog_key = repr(
            sorted(
                (code, sorted(config.items()))
                for code, config in catalog.items()
            )
        )
        cache_key = (url, catalog_key)
        now = time.monotonic()
        with self._lock:
            if self._cache_key == cache_key and now < self._cache_expires_at:
                return self._cached_matches
        if not url:
            return ()
        try:
            payload = discover_branch_candidates(
                self.service,
                url=url,
                catalog=catalog,
            )
        except (SVNProviderError, OSError, RuntimeError, ValueError):
            with self._lock:
                if self._cache_key == cache_key:
                    return self._cached_matches
            raise
        matches = tuple(payload.matches)
        with self._lock:
            self._cache_key = cache_key
            self._cached_matches = matches
            self._cache_expires_at = now + self.cache_seconds
        return matches

    def records(self) -> list[dict[str, Any]]:
        configured = [dict(record) for record in self.endpoint_registry()]
        try:
            matches = self._matches()
        except (SVNProviderError, OSError, RuntimeError, ValueError):
            matches = ()

        configured_ids = {str(record.get("id", "")) for record in configured}
        configured_urls = {
            str(record.get("url", "")).rstrip("/").casefold()
            for record in configured
            if str(record.get("url", "")).strip()
        }
        discovered: list[dict[str, Any]] = []
        for match in matches:
            record = endpoint_record_for_match(match)
            if record["id"] in configured_ids:
                continue
            if record["url"].rstrip("/").casefold() in configured_urls:
                continue
            discovered.append(record)
        return sorted(
            configured + discovered,
            key=lambda item: (
                str(item.get("label", "")).casefold(),
                str(item.get("id", "")).casefold(),
            ),
        )
