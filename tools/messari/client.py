"""Messari API client."""

import os
from typing import Any

import httpx

BASE_URL_V1 = "https://data.messari.io/api/v1"
BASE_URL_V2 = "https://data.messari.io/api/v2"


class MessariClient:
    """Client for Messari API."""

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.getenv("MESSARI_API_KEY")
        if not self._api_key:
            raise RuntimeError("MESSARI_API_KEY not set.")
        self._client = httpx.Client(
            headers={"x-messari-api-key": self._api_key},
            timeout=30.0,
        )

    def _request(self, endpoint: str, version: int = 1, params: dict | None = None) -> dict[str, Any]:
        """Make request to Messari API."""
        base_url = BASE_URL_V1 if version == 1 else BASE_URL_V2
        url = f"{base_url}{endpoint}"
        response = self._client.get(url, params=params)
        if response.status_code >= 400:
            try:
                error = response.json()
                msg = error.get("status", {}).get("error_message", response.text)
            except Exception:
                msg = response.text
            raise RuntimeError(f"Messari API error ({response.status_code}): {msg}")
        return response.json()

    def list_assets(self, limit: int = 20, fields: str | None = None) -> list[dict]:
        """List all assets."""
        params: dict[str, Any] = {"limit": limit}
        if fields:
            params["fields"] = fields
        data = self._request("/assets", version=1, params=params)
        return data.get("data", [])

    def get_asset(self, asset_key: str) -> dict:
        """Get asset by slug or ID."""
        data = self._request(f"/assets/{asset_key}", version=1)
        return data.get("data", {})

    def get_asset_metrics(self, asset_key: str) -> dict:
        """Get metrics for an asset."""
        data = self._request(f"/assets/{asset_key}/metrics", version=1)
        return data.get("data", {})

    def get_asset_profile(self, asset_key: str) -> dict:
        """Get profile for an asset (v2)."""
        data = self._request(f"/assets/{asset_key}/profile", version=2)
        return data.get("data", {})

    def get_asset_markets(self, asset_key: str) -> list[dict]:
        """Get markets for an asset."""
        data = self._request(f"/assets/{asset_key}/markets", version=1)
        return data.get("data", [])

    def get_news(self, limit: int = 10) -> list[dict]:
        """Get latest news."""
        params: dict[str, Any] = {"limit": limit}
        data = self._request("/news", version=1, params=params)
        return data.get("data", [])

    def get_timeseries(
        self,
        asset_key: str,
        metric: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        """Get timeseries data for an asset metric."""
        params: dict[str, Any] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request(
            f"/assets/{asset_key}/metrics/{metric}/time-series", version=1, params=params
        )
        return data.get("data", {})

    def raw_request(
        self, endpoint: str, version: int = 1, params: dict | None = None
    ) -> dict:
        """Make a raw API call to any endpoint."""
        return self._request(endpoint, version=version, params=params)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


try:
    from shared.tool_sdk import secret
except ImportError:

    def secret(key: str, default: str | None = None) -> str:  # type: ignore[misc]
        val = os.environ.get(key)
        if val:
            return val
        if default is not None:
            return default
        raise KeyError(f"Missing env var '{key}'")


def _client() -> MessariClient:
    return MessariClient(api_key=secret("MESSARI_API_KEY"))
