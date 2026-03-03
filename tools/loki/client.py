"""Loki HTTP API client for LogQL queries, labels, and log streams."""

from typing import Any

import httpx
from shared.tool_sdk import secret


class LokiClient:
    """Client for the Loki HTTP API.

    Queries logs via LogQL, lists labels/values, and retrieves log streams.
    Connects directly to the Loki instance (default: http://loki:3100).
    """

    def __init__(self, url: str | None = None, timeout: float = 30.0):
        self._url = url
        self.timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def base_url(self) -> str:
        return (self._url or secret("LOKI_URL", "http://loki:3100")).rstrip("/")

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
    ) -> Any:
        resp = self.client.request(method, path, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"Loki API error ({resp.status_code}): {resp.text}")
        return resp.json()

    # -- Queries ---------------------------------------------------------------

    def query(
        self,
        query: str,
        limit: int = 100,
        start: str | None = None,
        end: str | None = None,
        direction: str = "backward",
    ) -> dict:
        """Run a LogQL query. Uses range query if start is set, otherwise instant.

        Args:
            query: LogQL expression (e.g. '{container_name=~".*api.*"} |= "error"').
            limit: Max log lines to return.
            start: Range start (RFC3339, Unix epoch seconds, or nanoseconds).
            end: Range end. Defaults to now.
            direction: 'backward' (newest first) or 'forward' (oldest first).
        """
        params: dict[str, Any] = {"query": query, "limit": limit, "direction": direction}
        if start:
            params["start"] = start
            if end:
                params["end"] = end
            return self._request("GET", "/loki/api/v1/query_range", params=params)
        return self._request("GET", "/loki/api/v1/query", params=params)

    def labels(self, start: str | None = None, end: str | None = None) -> list[str]:
        """List all known label names.

        Args:
            start: Optional start time filter (RFC3339 or epoch).
            end: Optional end time filter.
        """
        params: dict[str, str] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request("GET", "/loki/api/v1/labels", params=params or None)
        return data.get("data", [])

    def label_values(
        self, label: str, start: str | None = None, end: str | None = None
    ) -> list[str]:
        """Get all values for a specific label.

        Args:
            label: Label name (e.g. 'container_name', 'job').
            start: Optional start time filter.
            end: Optional end time filter.
        """
        params: dict[str, str] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request(
            "GET", f"/loki/api/v1/label/{label}/values", params=params or None
        )
        return data.get("data", [])

    def series(
        self,
        match: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        """Find log series matching a label selector.

        Args:
            match: Label matcher (e.g. '{job="api"}').
            start: Optional start time filter.
            end: Optional end time filter.
        """
        params: dict[str, str] = {"match[]": match}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request("GET", "/loki/api/v1/series", params=params)
        return data.get("data", [])

    def ready(self) -> bool:
        """Check if Loki is ready to serve requests."""
        resp = self.client.get("/ready")
        return resp.status_code == 200

    # -- Lifecycle -------------------------------------------------------------

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> LokiClient:
    return LokiClient()
