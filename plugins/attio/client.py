"""Attio API client."""

import os
from typing import Any

import httpx


class AttioClient:
    """Authenticated Attio CRM API client."""

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.getenv("ATTIO_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "ATTIO_API_KEY not set.\nGenerate one at https://app.attio.com/settings/developers"
            )
        self._client = httpx.Client(
            base_url="https://api.attio.com/v2",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Make authenticated request to Attio API."""
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                error = response.json()
                msg = error.get("message", response.text)
            except Exception:
                msg = response.text
            raise RuntimeError(f"Attio API error ({response.status_code}): {msg}")
        return response.json()

    def list_objects(self) -> list[dict]:
        """List all objects in workspace."""
        data = self._request("GET", "/objects")
        return data.get("data", [])

    def get_object(self, object_slug: str) -> dict:
        """Get object by slug or ID."""
        data = self._request("GET", f"/objects/{object_slug}")
        return data.get("data", {})

    def list_attributes(self, object_slug: str) -> list[dict]:
        """List attributes for an object."""
        data = self._request("GET", f"/objects/{object_slug}/attributes")
        return data.get("data", [])

    def query_records(
        self,
        object_slug: str,
        filter_obj: dict | None = None,
        sorts: list[dict] | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict]:
        """Query records for an object with optional filtering."""
        body: dict[str, Any] = {"limit": limit, "offset": offset}
        if filter_obj:
            body["filter"] = filter_obj
        if sorts:
            body["sorts"] = sorts
        data = self._request("POST", f"/objects/{object_slug}/records/query", json=body)
        return data.get("data", [])

    def get_record(self, object_slug: str, record_id: str) -> dict:
        """Get a specific record by ID."""
        data = self._request("GET", f"/objects/{object_slug}/records/{record_id}")
        return data.get("data", {})

    def create_record(self, object_slug: str, values: dict) -> dict:
        """Create a new record."""
        body = {"data": {"values": values}}
        data = self._request("POST", f"/objects/{object_slug}/records", json=body)
        return data.get("data", {})

    def update_record(self, object_slug: str, record_id: str, values: dict) -> dict:
        """Update an existing record."""
        body = {"data": {"values": values}}
        data = self._request("PATCH", f"/objects/{object_slug}/records/{record_id}", json=body)
        return data.get("data", {})

    def delete_record(self, object_slug: str, record_id: str) -> bool:
        """Delete a record."""
        self._request("DELETE", f"/objects/{object_slug}/records/{record_id}")
        return True

    def assert_record(self, object_slug: str, matching_attribute: str, values: dict) -> dict:
        """Create or update a record based on matching attribute."""
        body = {"data": {"values": values}}
        data = self._request(
            "PUT",
            f"/objects/{object_slug}/records",
            params={"matching_attribute": matching_attribute},
            json=body,
        )
        return data.get("data", {})

    def list_lists(self) -> list[dict]:
        """List all lists in workspace."""
        data = self._request("GET", "/lists")
        return data.get("data", [])

    def get_list(self, list_id: str) -> dict:
        """Get list by ID or slug."""
        data = self._request("GET", f"/lists/{list_id}")
        return data.get("data", {})

    def query_entries(
        self,
        list_id: str,
        filter_obj: dict | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict]:
        """Query entries in a list."""
        body: dict[str, Any] = {"limit": limit, "offset": offset}
        if filter_obj:
            body["filter"] = filter_obj
        data = self._request("POST", f"/lists/{list_id}/entries/query", json=body)
        return data.get("data", [])

    def create_entry(
        self, list_id: str, parent_record_id: str, values: dict | None = None
    ) -> dict:
        """Create a new entry in a list."""
        body: dict[str, Any] = {"data": {"parent_record_id": parent_record_id}}
        if values:
            body["data"]["entry_values"] = values
        data = self._request("POST", f"/lists/{list_id}/entries", json=body)
        return data.get("data", {})

    def list_notes(self, parent_object: str, parent_record_id: str) -> list[dict]:
        """List notes for a record."""
        data = self._request(
            "GET",
            "/notes",
            params={"parent_object": parent_object, "parent_record_id": parent_record_id},
        )
        return data.get("data", [])

    def create_note(
        self, parent_object: str, parent_record_id: str, title: str, content: str
    ) -> dict:
        """Create a note for a record."""
        body = {
            "data": {
                "parent_object": parent_object,
                "parent_record_id": parent_record_id,
                "title": title,
                "format": "plaintext",
                "content": content,
            }
        }
        data = self._request("POST", "/notes", json=body)
        return data.get("data", {})

    def list_tasks(
        self,
        linked_object: str | None = None,
        linked_record_id: str | None = None,
        is_completed: bool | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """List tasks with optional filters."""
        params: dict[str, Any] = {"limit": limit}
        if linked_object:
            params["linked_object"] = linked_object
        if linked_record_id:
            params["linked_record_id"] = linked_record_id
        if is_completed is not None:
            params["is_completed"] = str(is_completed).lower()
        data = self._request("GET", "/tasks", params=params)
        return data.get("data", [])

    def create_task(
        self,
        content: str,
        deadline: str | None = None,
        assignees: list[str] | None = None,
        linked_records: list[dict] | None = None,
    ) -> dict:
        """Create a new task."""
        body: dict[str, Any] = {
            "data": {
                "content": content,
                "format": "plaintext",
            }
        }
        if deadline:
            body["data"]["deadline_at"] = deadline
        if assignees:
            body["data"]["assignees"] = [{"workspace_member_id": a} for a in assignees]
        if linked_records:
            body["data"]["linked_records"] = linked_records
        data = self._request("POST", "/tasks", json=body)
        return data.get("data", {})

    def list_workspace_members(self) -> list[dict]:
        """List workspace members."""
        data = self._request("GET", "/workspace_members")
        return data.get("data", [])

    def get_self(self) -> dict:
        """Get info about the current API token."""
        data = self._request("GET", "/self")
        return data.get("data", {})

    def close(self):
        """Close the underlying HTTP client."""
        self._client.close()


def _client() -> AttioClient:
    """Factory for plugin SDK integration."""
    from shared.plugin_sdk import secret

    return AttioClient(api_key=secret("ATTIO_API_KEY"))
