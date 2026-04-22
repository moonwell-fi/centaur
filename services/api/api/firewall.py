from __future__ import annotations

import os

import httpx


def control_url() -> str:
    return os.environ.get("FIREWALL_HEALTH_URL", "http://firewall:8081").rstrip("/")


def control_headers() -> dict[str, str]:
    token = (os.environ.get("FIREWALL_CONTROL_TOKEN") or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def fetch_control_secret(key: str) -> str | None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            f"{control_url()}/secrets/{key}",
            headers=control_headers(),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    value = payload.get("value")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
