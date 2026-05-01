from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from api.apps import AppManager


@pytest.mark.asyncio
async def test_recover_apps_skips_docker_client_for_kubernetes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AppManager()
    pool = AsyncMock()

    monkeypatch.setenv("SANDBOX_BACKEND", "kubernetes")

    def fail_if_called():
        raise AssertionError("docker client should not be constructed")

    monkeypatch.setattr(manager, "_get_client", fail_if_called)

    recovered = await manager.recover_apps(pool)

    assert recovered == 0
    pool.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_build_and_start_wraps_custom_start_command_in_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AppManager()
    pool = AsyncMock()
    manager._exec = AsyncMock(
        side_effect=[
            (0, b"clone ok"),
            (0, b"build ok"),
            (0, b"start ok"),
        ]
    )
    monkeypatch.setattr("api.apps.asyncio.sleep", AsyncMock())

    await manager._build_and_start(
        pool,
        "app-123",
        "container-123",
        "https://github.com/paradigmxyz/centaur",
        3000,
        build_cmd="cd apps/venue-scout && npm install --no-package-lock && npm run build",
        start_cmd="cd apps/venue-scout && npm start",
    )

    start_call = manager._exec.await_args_list[2].args[1]
    assert start_call == [
        "sh",
        "-c",
        "PORT=3000 nohup sh -lc 'cd /home/agent/app && cd apps/venue-scout && npm start' > /tmp/app.log 2>&1 &",
    ]


@pytest.mark.asyncio
async def test_app_proxy_requires_global_auth_for_unconfigured_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.routers import apps as apps_router

    global_pass = "testpass"
    global_hash = hashlib.sha256(global_pass.encode()).hexdigest()

    app_row = {
        "container_id": "container-public",
        "status": "running",
        "port": 3000,
        "is_public": False,
        "basic_auth_user": None,
        "basic_auth_pass_hash": None,
    }
    config_row = {"password_hash": global_hash}

    pool = AsyncMock()

    def make_request(headers=None):
        return SimpleNamespace(
            headers=headers or {},
            url=SimpleNamespace(query="city=sf"),
            client=SimpleNamespace(host="127.0.0.1"),
            method="GET",
            app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
            body=AsyncMock(return_value=b""),
        )

    async def mock_verify_api_key(request, x_api_key=None):
        raise HTTPException(status_code=401, detail="Missing API key")

    monkeypatch.setattr(apps_router, "verify_api_key", mock_verify_api_key)

    # Unauthenticated request → 401
    pool.fetchrow.return_value = app_row
    response = await apps_router._do_proxy(make_request(), "public-app", "api/scout")
    assert response.status_code == 401

    # Authenticated with global password → 200
    auth_header = "Basic " + base64.b64encode(f"user:{global_pass}".encode()).decode()
    pool.fetchrow.side_effect = [app_row, config_row]

    calls: list[dict[str, str]] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            calls.append({"method": method, "url": url})
            return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(
        apps_router.app_manager,
        "get_container_ip",
        AsyncMock(return_value="10.0.0.8"),
    )
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", FakeAsyncClient)

    response = await apps_router._do_proxy(
        make_request({"authorization": auth_header}),
        "public-app",
        "api/scout",
    )
    assert response.status_code == 200
    assert calls == [
        {
            "method": "GET",
            "url": "http://10.0.0.8:3000/api/scout?city=sf",
        }
    ]


@pytest.mark.asyncio
async def test_app_proxy_requires_auth_only_for_protected_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.routers import apps as apps_router

    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "container_id": "container-private",
        "status": "running",
        "port": 3000,
        "is_public": False,
        "basic_auth_user": "hackathon",
        "basic_auth_pass_hash": hashlib.sha256("secret123".encode()).hexdigest(),
    }

    unauthorized_request = SimpleNamespace(
        headers={},
        url=SimpleNamespace(query=""),
        client=SimpleNamespace(host="127.0.0.1"),
        method="GET",
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
        body=AsyncMock(return_value=b""),
    )
    authorized_request = SimpleNamespace(
        headers={
            "authorization": "Basic aGFja2F0aG9uOnNlY3JldDEyMw==",
        },
        url=SimpleNamespace(query=""),
        client=SimpleNamespace(host="127.0.0.1"),
        method="GET",
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
        body=AsyncMock(return_value=b""),
    )

    calls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            calls.append(url)
            return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(
        apps_router.app_manager,
        "get_container_ip",
        AsyncMock(return_value="10.0.0.9"),
    )
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", FakeAsyncClient)

    unauthorized = await apps_router._do_proxy(unauthorized_request, "private-app", "")
    authorized = await apps_router._do_proxy(authorized_request, "private-app", "")

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert calls == ["http://10.0.0.9:3000/"]


@pytest.mark.asyncio
async def test_app_proxy_allows_x_api_key_for_unconfigured_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.routers import apps as apps_router

    app_row = {
        "container_id": "container-public",
        "status": "running",
        "port": 3000,
        "is_public": False,
        "basic_auth_user": None,
        "basic_auth_pass_hash": None,
    }

    pool = AsyncMock()
    pool.fetchrow.return_value = app_row

    request = SimpleNamespace(
        headers={"x-api-key": "test-key-123"},
        url=SimpleNamespace(query=""),
        client=SimpleNamespace(host="10.0.0.1"),
        method="GET",
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
        body=AsyncMock(return_value=b""),
    )

    api_key_received: list[str | None] = []

    async def mock_verify_api_key(request, x_api_key=None):
        api_key_received.append(x_api_key)

    monkeypatch.setattr(apps_router, "verify_api_key", mock_verify_api_key)

    calls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            calls.append(url)
            return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(
        apps_router.app_manager,
        "get_container_ip",
        AsyncMock(return_value="10.0.0.5"),
    )
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", FakeAsyncClient)

    response = await apps_router._do_proxy(request, "public-app", "")

    assert response.status_code == 200
    assert api_key_received == ["test-key-123"]
    assert calls == ["http://10.0.0.5:3000/"]


@pytest.mark.asyncio
async def test_app_proxy_allows_public_apps_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.routers import apps as apps_router

    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "container_id": "container-public",
        "status": "running",
        "port": 3000,
        "is_public": True,
        "basic_auth_user": None,
        "basic_auth_pass_hash": None,
    }

    request = SimpleNamespace(
        headers={},
        url=SimpleNamespace(query="city=nyc"),
        client=SimpleNamespace(host="10.0.0.2"),
        method="GET",
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
        body=AsyncMock(return_value=b""),
    )

    async def fail_verify_api_key(request, x_api_key=None):
        raise AssertionError("public apps should not require API keys")

    async def fail_global_auth(request, pool):
        raise AssertionError("public apps should not require global auth")

    monkeypatch.setattr(apps_router, "verify_api_key", fail_verify_api_key)
    monkeypatch.setattr(apps_router, "_check_global_auth", fail_global_auth)

    calls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, headers=None, content=None):
            calls.append(url)
            return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(
        apps_router.app_manager,
        "get_container_ip",
        AsyncMock(return_value="10.0.0.6"),
    )
    monkeypatch.setattr(apps_router.httpx, "AsyncClient", FakeAsyncClient)

    response = await apps_router._do_proxy(request, "public-app", "")

    assert response.status_code == 200
    assert calls == ["http://10.0.0.6:3000/?city=nyc"]


@pytest.mark.asyncio
async def test_update_app_access_sets_public_flag() -> None:
    from api.routers import apps as apps_router

    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "name": "venue-scout",
        "is_public": True,
        "basic_auth_user": None,
        "basic_auth_pass_hash": None,
    }
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))

    response = await apps_router.update_app_access(
        request,
        "venue-scout",
        apps_router.AccessRequest(is_public=True),
    )

    assert response == {
        "name": "venue-scout",
        "is_public": True,
        "has_basic_auth": False,
    }
    pool.fetchrow.assert_awaited_once_with(
        "UPDATE apps SET is_public = $2, updated_at = NOW() WHERE name = $1 "
        "RETURNING name, is_public, basic_auth_user, basic_auth_pass_hash",
        "venue-scout",
        True,
    )
