from fastapi import APIRouter, Request

from app.schemas.svn import HealthPayload

router = APIRouter()


@router.get("/health", response_model=HealthPayload)
def health(request: Request) -> HealthPayload:
    provider_name = getattr(request.app.state, "provider_name", "unknown")
    provider = getattr(request.app.state, "provider", None)
    available = None
    client = getattr(provider, "client", None)
    if client is not None and hasattr(client, "available"):
        available = bool(client.available())
    credential_source = getattr(request.app.state, "credential_source", "svn_cli_cache")
    return HealthPayload(status="ok", provider=provider_name, svn_cli_available=available, credential_source=credential_source)
