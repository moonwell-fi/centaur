"""Apps router — deploy, manage, and proxy to long-lived web app containers."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from api.apps import app_manager
from api.deps import verify_api_key

log = structlog.get_logger()

# ── Management router (requires API key) ─────────────────────────────────────
#
# Uses /_manage prefix so management endpoints (GET/DELETE/POST by name) don't
# shadow the proxy catch-all at /apps/{name}.  The top-level collection routes
# (POST /apps, GET /apps) stay at the /apps root.

router = APIRouter(
    prefix="/apps",
    tags=["apps"],
    dependencies=[Depends(verify_api_key)],
)


class DeployRequest(BaseModel):
    name: str
    repo_url: str
    port: int = 3000
    is_public: bool = False
    basic_auth_user: str | None = None
    basic_auth_pass: str | None = None
    env: dict[str, str] | None = None
    build_cmd: str | None = None
    start_cmd: str | None = None
    created_by: str | None = None


class AccessRequest(BaseModel):
    is_public: bool


@router.post("")
async def deploy_app(request: Request, body: DeployRequest):
    """Deploy a new app from a GitHub repo."""
    pool = request.app.state.db_pool

    existing = await pool.fetchrow("SELECT id FROM apps WHERE name = $1", body.name)
    if existing:
        raise HTTPException(status_code=409, detail=f"App '{body.name}' already exists")

    if body.is_public and (body.basic_auth_user or body.basic_auth_pass):
        raise HTTPException(
            status_code=400,
            detail="Public apps cannot also set basic auth credentials",
        )

    pass_hash = (
        hashlib.sha256(body.basic_auth_pass.encode()).hexdigest()
        if body.basic_auth_pass
        else None
    )

    try:
        result = await app_manager.deploy(
            pool,
            name=body.name,
            repo_url=body.repo_url,
            port=body.port,
            is_public=body.is_public,
            basic_auth_user=body.basic_auth_user,
            basic_auth_pass_hash=pass_hash,
            env_json=body.env,
            build_cmd=body.build_cmd,
            start_cmd=body.start_cmd,
            created_by=body.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return JSONResponse(status_code=201, content=result)


@router.get("")
async def list_apps(request: Request):
    """List all deployed apps."""
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        "SELECT id, name, repo_url, status, port, is_public, created_by, created_at, updated_at "
        "FROM apps ORDER BY created_at DESC"
    )
    return {
        "apps": [
            {
                "id": r["id"],
                "name": r["name"],
                "repo_url": r["repo_url"],
                "status": r["status"],
                "port": r["port"],
                "is_public": r["is_public"],
                "created_by": r["created_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/_manage/{name}")
async def get_app(request: Request, name: str):
    """Get app details."""
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "SELECT id, name, repo_url, container_id, status, port, "
        "is_public, basic_auth_user, basic_auth_pass_hash, env_json, build_cmd, "
        "start_cmd, created_by, "
        "build_log, error_text, created_at, updated_at FROM apps WHERE name = $1",
        name,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    env_json = row["env_json"]
    if isinstance(env_json, str):
        env_json = json.loads(env_json)

    return {
        "id": row["id"],
        "name": row["name"],
        "repo_url": row["repo_url"],
        "container_id": row["container_id"][:12] if row["container_id"] else None,
        "status": row["status"],
        "port": row["port"],
        "is_public": row["is_public"],
        "has_basic_auth": bool(
            row["basic_auth_user"] or row["basic_auth_pass_hash"]
        ),
        "env": env_json,
        "build_cmd": row["build_cmd"],
        "start_cmd": row["start_cmd"],
        "created_by": row["created_by"],
        "build_log": row["build_log"],
        "error_text": row["error_text"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/_manage/{name}/access")
async def update_app_access(request: Request, name: str, body: AccessRequest):
    """Update whether an app is publicly reachable without auth."""
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "UPDATE apps SET is_public = $2, updated_at = NOW() WHERE name = $1 "
        "RETURNING name, is_public, basic_auth_user, basic_auth_pass_hash",
        name,
        body.is_public,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    return {
        "name": row["name"],
        "is_public": row["is_public"],
        "has_basic_auth": bool(
            row["basic_auth_user"] or row["basic_auth_pass_hash"]
        ),
    }


@router.delete("/_manage/{name}")
async def delete_app(request: Request, name: str):
    """Stop and remove an app."""
    pool = request.app.state.db_pool
    try:
        await app_manager.stop_app(pool, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "name": name}


@router.post("/_manage/{name}/restart")
async def restart_app(request: Request, name: str):
    """Restart an app (full rebuild)."""
    pool = request.app.state.db_pool
    try:
        result = await app_manager.restart_app(pool, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


@router.get("/_manage/{name}/logs")
async def get_app_logs(request: Request, name: str, tail: int = 200):
    """Get app build and runtime logs."""
    pool = request.app.state.db_pool
    try:
        logs = await app_manager.get_logs(pool, name, tail=tail)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"name": name, "logs": logs}


# ── Reverse proxy (NO API key — uses basic auth only) ────────────────────────
#
# Separate router so browser/public traffic can reach apps at /apps/{name}/
# without an API key.  Basic auth is checked per-app if configured.
# Registered AFTER the management router so explicit routes match first.

proxy_router = APIRouter(prefix="/apps", tags=["app-proxy"])


def _decode_basic_auth(request: Request) -> tuple[str, str] | None:
    """Return decoded basic-auth credentials when present and valid."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(auth_header[6:]).decode()
        username, password = decoded.split(":", 1)
        return username, password
    except Exception:
        return None


def _is_valid_basic_auth(
    request: Request,
    *,
    expected_user: str | None,
    expected_password_hash: str | None,
) -> bool:
    """Validate per-app basic auth credentials."""
    credentials = _decode_basic_auth(request)
    if credentials is None:
        return False

    username, password = credentials
    return (
        username == expected_user
        and expected_password_hash is not None
        and hashlib.sha256(password.encode()).hexdigest() == expected_password_hash
    )


async def _check_global_auth(request: Request, pool) -> bool:
    """Check password against the global hash in app_config."""
    credentials = _decode_basic_auth(request)
    if credentials is None:
        return False
    _, password = credentials
    row = await pool.fetchrow("SELECT password_hash FROM app_config WHERE id = 1")
    if not row:
        return False
    return hashlib.sha256(password.encode()).hexdigest() == row["password_hash"]


async def _do_proxy(request: Request, name: str, path: str):
    """Shared proxy logic used by both path-based and subdomain routes."""
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "SELECT container_id, status, port, is_public, basic_auth_user, basic_auth_pass_hash "
        "FROM apps WHERE name = $1",
        name,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    if row["is_public"]:
        authorized = True
    elif row["basic_auth_user"] or row["basic_auth_pass_hash"]:
        authorized = _is_valid_basic_auth(
            request,
            expected_user=row["basic_auth_user"],
            expected_password_hash=row["basic_auth_pass_hash"],
        )
    else:
        authorized = await _check_global_auth(request, pool)
        if not authorized:
            try:
                await verify_api_key(
                    request,
                    x_api_key=request.headers.get("x-api-key"),
                )
                authorized = True
            except HTTPException:
                pass

    if not authorized:
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": 'Basic realm="Centaur App"'},
        )

    if row["status"] != "running":
        raise HTTPException(
            status_code=503,
            detail=f"App '{name}' is not running (status: {row['status']})",
        )

    container_ip = await app_manager.get_container_ip(row["container_id"])
    if not container_ip:
        raise HTTPException(status_code=503, detail="Cannot resolve app container IP")

    target_path = f"/{path}" if path else "/"
    target_url = f"http://{container_ip}:{row['port']}{target_path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Forward headers (excluding hop-by-hop)
    forward_headers: dict[str, str] = {}
    skip = {"host", "authorization", "connection", "transfer-encoding"}
    for k, v in request.headers.items():
        if k.lower() not in skip:
            forward_headers[k] = v
    forward_headers["x-forwarded-for"] = request.client.host if request.client else ""
    forward_headers["x-forwarded-proto"] = request.headers.get(
        "x-forwarded-proto", "https"
    )

    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body if body else None,
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="App is not responding")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Filter hop-by-hop response headers
    resp_headers: dict[str, str] = {}
    skip_resp = {
        "transfer-encoding",
        "connection",
        "content-encoding",
        "content-length",
    }
    for k, v in resp.headers.items():
        if k.lower() not in skip_resp:
            resp_headers[k] = v

    return StreamingResponse(
        content=iter([resp.content]),
        status_code=resp.status_code,
        headers=resp_headers,
    )


@proxy_router.api_route(
    "/{name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_to_app(request: Request, name: str, path: str):
    """Reverse proxy requests to app containers at /apps/{name}/{path}."""
    return await _do_proxy(request, name, path)


@proxy_router.api_route(
    "/{name}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_to_app_root(request: Request, name: str):
    """Reverse proxy requests to app containers at /apps/{name} (no trailing path)."""
    return await _do_proxy(request, name, "")
