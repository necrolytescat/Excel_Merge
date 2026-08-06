from __future__ import annotations

import re
import urllib.parse

from fastapi import APIRouter, Depends, Query, Request

from app.schemas.svn import (
    ContentPayload,
    EndpointPayload,
    InfoPayload,
    ListPayload,
    LogPayload,
    SVNConfigPayload,
    SVNConfigUpdatePayload,
    EndpointCatalogPayload,
    BranchCandidatesPayload,
    BranchMatchPayload,
    EndpointRegistryPayload,
    SnapshotRequestPayload,
    SnapshotResponsePayload,
)
from app.services.svn_service import SVNService
from app.services.snapshot_service import SnapshotService
from core.svn_provider import SVNProviderError

router = APIRouter(prefix="/svn", tags=["svn"])


def get_service(request: Request) -> SVNService:
    return request.app.state.svn_service


def _endpoint(url: str, revision: int | str, path_filter: str) -> EndpointPayload:
    filters = [item.strip() for item in (path_filter or "").split(",") if item.strip()]
    return EndpointPayload(url=url, revision=revision, path_filter=filters)


@router.get("/config", response_model=SVNConfigPayload)
def get_config(request: Request) -> SVNConfigPayload:
    return SVNConfigPayload(
        provider=str(getattr(request.app.state, "provider_name", "unknown")),
        server_url=str(getattr(request.app.state, "default_url", "")),
        credential_source=str(getattr(request.app.state, "credential_source", "svn_cli_cache")),
    )


@router.post("/config", response_model=SVNConfigPayload)
def save_config(
    payload: SVNConfigUpdatePayload,
    request: Request,
    service: SVNService = Depends(get_service),
) -> SVNConfigPayload:
    # 复用统一地址校验；不会读取或接收任何凭据。
    service.endpoint(EndpointPayload(url=payload.server_url, revision="HEAD"))
    request.app.state.config_store.save_server_url(payload.server_url)
    request.app.state.default_url = payload.server_url
    return get_config(request)


_ALLOWED_REGION_CODES = {"TC", "KR", "BT", "JP"}
_SAFE_ENDPOINT_VALUE = re.compile(r"^[A-Za-z0-9._xX-]*$")


def _validate_endpoint_catalog(payload: EndpointCatalogPayload) -> dict:
    regions = payload.regions
    if set(regions) != _ALLOWED_REGION_CODES:
        raise SVNProviderError("SVN_INVALID_ENDPOINT_CONFIG", "端点目录必须包含 TC、KR、BT、JP 四个区域")
    result = {}
    for code, item in regions.items():
        for value in (item.trunk_branch, item.fix_pattern):
            if not _SAFE_ENDPOINT_VALUE.fullmatch(value) or ".." in value:
                raise SVNProviderError("SVN_INVALID_ENDPOINT_CONFIG", f"{code} 端点配置包含非法字符")
        result[code] = item.model_dump()
    return result


@router.get("/endpoint-catalog", response_model=EndpointCatalogPayload)
def get_endpoint_catalog(request: Request) -> EndpointCatalogPayload:
    return EndpointCatalogPayload(regions=getattr(request.app.state, "endpoint_catalog", {}))


@router.post("/endpoint-catalog", response_model=EndpointCatalogPayload)
def save_endpoint_catalog(payload: EndpointCatalogPayload, request: Request) -> EndpointCatalogPayload:
    catalog = _validate_endpoint_catalog(payload)
    request.app.state.config_store.save_endpoint_catalog(catalog)
    request.app.state.endpoint_catalog = catalog
    return EndpointCatalogPayload(regions=catalog)

def get_snapshot_service(request: Request) -> SnapshotService:
    return request.app.state.snapshot_service


@router.get("/endpoints", response_model=EndpointRegistryPayload)
def get_endpoints(request: Request) -> EndpointRegistryPayload:
    return EndpointRegistryPayload(endpoints=getattr(request.app.state, "endpoint_registry", []))


@router.post("/endpoints", response_model=EndpointRegistryPayload)
def save_endpoints(
    payload: EndpointRegistryPayload,
    request: Request,
) -> EndpointRegistryPayload:
    registry = SnapshotService.normalize_registry(
        [item.model_dump() for item in payload.endpoints]
    )
    request.app.state.config_store.save_endpoint_registry(registry)
    request.app.state.endpoint_registry = registry
    return EndpointRegistryPayload(endpoints=registry)


@router.post("/endpoints/{endpoint_id}/discover", response_model=EndpointRegistryPayload)
def discover_endpoint(
    endpoint_id: str,
    request: Request,
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
) -> EndpointRegistryPayload:
    registry = snapshot_service.discover_and_bind(
        getattr(request.app.state, "endpoint_registry", []),
        endpoint_id=endpoint_id,
    )
    request.app.state.config_store.save_endpoint_registry(registry)
    request.app.state.endpoint_registry = registry
    return EndpointRegistryPayload(endpoints=registry)


@router.post("/snapshots", response_model=SnapshotResponsePayload)
def create_snapshot(
    payload: SnapshotRequestPayload,
    request: Request,
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
) -> SnapshotResponsePayload:
    records = getattr(request.app.state, "endpoint_registry", [])
    snapshot = snapshot_service.create_snapshot(
        records,
        source_id=payload.source.endpoint_id,
        target_id=payload.target.endpoint_id,
    )
    registry = snapshot_service.bind_snapshot_scopes(records, snapshot)
    request.app.state.config_store.save_endpoint_registry(registry)
    request.app.state.endpoint_registry = registry
    return snapshot

def _project_root_url(url: str) -> str:
    """从当前 Trunk_* 地址推导 Resource 项目根目录；其他地址保持不变。"""
    parsed = urllib.parse.urlsplit(url.strip())
    path = parsed.path.rstrip("/")
    parts = path.split("/") if path else []
    last = urllib.parse.unquote(parts[-1]) if parts else ""
    if re.fullmatch(r"Trunk_[A-Za-z0-9_-]+", last, re.IGNORECASE):
        parts.pop()
        path = "/".join(parts) or "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _pattern_regex(pattern: str) -> re.Pattern[str] | None:
    value = (pattern or "").strip()
    if not value:
        return None
    # 配置中的 x 表示由分隔符包围的数字版本段，避免把 fix 单词里的 x 当作占位符。
    tokens = []
    for index, char in enumerate(value):
        previous = value[index - 1] if index else ""
        following = value[index + 1] if index + 1 < len(value) else ""
        if char.casefold() == "x" and (not previous or previous in "-_.") and (not following or following in "-_."):
            tokens.append(r"[0-9]+")
        else:
            tokens.append(re.escape(char))
    return re.compile(r"^" + "".join(tokens) + r"$", re.IGNORECASE)


def _configured_branch_matches(
    branches: list[str],
    *,
    base_url: str,
    catalog: dict,
    region: str | None,
) -> list[BranchMatchPayload]:
    selected_region = region.strip().upper() if region else ""
    matches: list[BranchMatchPayload] = []
    for code, config in catalog.items():
        if selected_region and code.upper() != selected_region:
            continue
        trunk = str(config.get("trunk_branch", "")).strip()
        fix_pattern = str(config.get("fix_pattern", "")).strip()
        fix_regex = _pattern_regex(fix_pattern)
        for branch in branches:
            if trunk and branch.casefold() == trunk.casefold():
                matches.append(BranchMatchPayload(
                    region=code,
                    track="DEV",
                    label=f"{config.get('display_name', code)} · {branch}",
                    branch=branch,
                    url=f"{base_url.rstrip('/')}/{urllib.parse.quote(branch, safe='~:@-_.')}",
                    match_type="TRUNK",
                ))
            if fix_regex and fix_regex.fullmatch(branch):
                matches.append(BranchMatchPayload(
                    region=code,
                    track="FIX",
                    label=f"{config.get('display_name', code)} · {branch}",
                    branch=branch,
                    url=f"{base_url.rstrip('/')}/branches/{urllib.parse.quote(branch, safe='~:@-_.')}",
                    match_type="FIX",
                ))
    return sorted(matches, key=lambda item: (item.region, item.match_type, item.branch.casefold()))


@router.get("/branch-candidates", response_model=BranchCandidatesPayload)
def branch_candidates(
    request: Request,
    url: str = Query(..., min_length=1),
    revision: int | str = Query("HEAD"),
    region: str | None = Query(None, max_length=16),
    service: SVNService = Depends(get_service),
) -> BranchCandidatesPayload:
    base_url = _project_root_url(url)
    payload = _endpoint(base_url, revision, "")
    root_entries = service.children(payload)
    root_dirs = sorted({entry.path.rsplit("/", 1)[-1] for entry in root_entries if entry.kind == "dir"})
    branch_names: list[str] = []
    if any(name.casefold() == "branches" for name in root_dirs):
        branch_entries = service.children(payload, "branches")
        branch_names = sorted({entry.path.rsplit("/", 1)[-1] for entry in branch_entries if entry.kind == "dir"})
    catalog = getattr(request.app.state, "endpoint_catalog", {}) if request is not None else {}
    matches = _configured_branch_matches(root_dirs + branch_names, base_url=base_url, catalog=catalog, region=region)
    trunk_branches = sorted({item.branch for item in matches if item.match_type == "TRUNK"}, key=str.casefold)
    fix_branches = sorted({item.branch for item in matches if item.match_type == "FIX"}, key=str.casefold)
    return BranchCandidatesPayload(
        base_url=base_url,
        revision=revision,
        trunk_branches=trunk_branches,
        fix_branches=fix_branches,
        matches=matches,
    )

@router.post("/probe", response_model=InfoPayload)
def probe(payload: EndpointPayload, service: SVNService = Depends(get_service)) -> InfoPayload:
    return service.probe(payload)


@router.get("/tree", response_model=ListPayload)
def tree(
    url: str = Query(..., min_length=1),
    revision: int | str = Query("HEAD"),
    prefix: str = Query(""),
    path_filter: str = Query(""),
    extension: str = Query(".csv"),
    service: SVNService = Depends(get_service),
) -> ListPayload:
    entries = service.tree(_endpoint(url, revision, path_filter), prefix)
    normalized_extension = extension.strip().lower()
    if normalized_extension:
        entries = [entry for entry in entries if entry.kind == "dir" or entry.path.lower().endswith(normalized_extension)]
    return ListPayload(entries=entries)


@router.get("/log", response_model=LogPayload)
def logs(
    url: str = Query(..., min_length=1),
    revision: int | str = Query("HEAD"),
    rev_from: int | str | None = Query(None),
    rev_to: int | str | None = Query(None),
    path_filter: str = Query(""),
    service: SVNService = Depends(get_service),
) -> LogPayload:
    commits = service.logs(_endpoint(url, revision, path_filter), rev_from, rev_to)
    return LogPayload(commits=commits)


@router.get("/content", response_model=ContentPayload)
def content(
    url: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
    revision: int | str = Query("HEAD"),
    preview_limit: int | None = Query(None, ge=1),
    path_filter: str = Query(""),
    service: SVNService = Depends(get_service),
) -> ContentPayload:
    return service.content(_endpoint(url, revision, path_filter), path, preview_limit)


