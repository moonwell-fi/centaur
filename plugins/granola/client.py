"""Granola API client using reverse-engineered endpoints.

Based on: https://github.com/getprobo/reverse-engineering-granola-api
"""

import json
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.granola.ai"
USER_AGENT = "Granola/5.354.0"


class GranolaClient:
    """Client for Granola meeting notes API."""

    def __init__(self):
        self._token: str | None = None

    def _get_credentials(self) -> str:
        """Load access token from Granola's supabase.json file."""
        if self._token:
            return self._token

        creds_path = Path.home() / "Library/Application Support/Granola/supabase.json"

        if not creds_path.exists():
            raise RuntimeError(
                f"Granola credentials not found at: {creds_path}\n"
                "Make sure Granola desktop app is installed and you're logged in."
            )

        try:
            with open(creds_path) as f:
                data = json.load(f)

            workos_tokens = json.loads(data["workos_tokens"])
            access_token = workos_tokens.get("access_token")

            if not access_token:
                raise RuntimeError("No access_token found in workos_tokens")

            self._token = access_token
            return access_token
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Failed to parse credentials: {e}") from e

    def _headers(self) -> dict[str, str]:
        """Get request headers with auth."""
        return {
            "Authorization": f"Bearer {self._get_credentials()}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
            "X-Client-Version": "5.354.0",
        }

    def _post(self, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make authenticated POST request to Granola API."""
        url = f"{API_BASE}{endpoint}"
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, headers=self._headers(), json=data or {})
            response.raise_for_status()
            return response.json()

    def list_documents(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Fetch list of meeting notes."""
        data = {
            "limit": limit,
            "offset": offset,
            "include_last_viewed_panel": True,
        }
        response = self._post("/v2/get-documents", data)
        return response.get("docs", [])

    def get_document(self, doc_id: str) -> dict[str, Any]:
        """Fetch a single document by ID."""
        docs = self.get_documents_batch([doc_id])
        if not docs:
            raise RuntimeError(f"Document not found: {doc_id}")
        return docs[0]

    def get_documents_batch(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch multiple documents by IDs."""
        data = {"ids": doc_ids}
        response = self._post("/v1/get-documents-batch", data)
        return response.get("documents") or response.get("docs", [])

    def get_transcript(self, doc_id: str) -> dict[str, Any]:
        """Fetch transcript for a document."""
        data = {"id": doc_id}
        try:
            return self._post("/v1/get-document-transcript", data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise RuntimeError(f"No transcript found for document: {doc_id}") from e
            raise

    def list_workspaces(self) -> list[dict[str, Any]]:
        """Fetch all workspaces (organizations) accessible to user."""
        response = self._post("/v1/get-workspaces", {})
        return response.get("workspaces", [])

    def list_folders(self) -> list[dict[str, Any]]:
        """Fetch all document lists (folders)."""
        try:
            response = self._post("/v2/get-document-lists", {})
        except httpx.HTTPStatusError:
            response = self._post("/v1/get-document-lists", {})

        return response.get("document_lists") or response.get("lists", [])

    @staticmethod
    def prosemirror_to_markdown(content: dict[str, Any] | None) -> str:
        """Convert ProseMirror JSON to Markdown."""
        if not content or not isinstance(content, dict) or "content" not in content:
            return ""

        def process_node(node: dict[str, Any]) -> str:
            if not isinstance(node, dict):
                return ""

            node_type = node.get("type", "")
            children = node.get("content", [])
            text = node.get("text", "")

            if node_type == "heading":
                level = node.get("attrs", {}).get("level", 1)
                heading_text = "".join(process_node(child) for child in children)
                return f"{'#' * level} {heading_text}\n\n"

            elif node_type == "paragraph":
                para_text = "".join(process_node(child) for child in children)
                return f"{para_text}\n\n"

            elif node_type == "bulletList":
                items = []
                for item in children:
                    if item.get("type") == "listItem":
                        item_content = "".join(
                            process_node(child) for child in item.get("content", [])
                        )
                        items.append(f"- {item_content.strip()}")
                return "\n".join(items) + "\n\n"

            elif node_type == "orderedList":
                items = []
                for i, item in enumerate(children, 1):
                    if item.get("type") == "listItem":
                        item_content = "".join(
                            process_node(child) for child in item.get("content", [])
                        )
                        items.append(f"{i}. {item_content.strip()}")
                return "\n".join(items) + "\n\n"

            elif node_type == "text":
                result = text
                marks = node.get("marks", [])
                for mark in marks:
                    mark_type = mark.get("type", "")
                    if mark_type == "bold":
                        result = f"**{result}**"
                    elif mark_type == "italic":
                        result = f"*{result}*"
                    elif mark_type == "code":
                        result = f"`{result}`"
                    elif mark_type == "link":
                        href = mark.get("attrs", {}).get("href", "")
                        result = f"[{result}]({href})"
                return result

            elif node_type == "codeBlock":
                code = "".join(process_node(child) for child in children)
                lang = node.get("attrs", {}).get("language", "")
                return f"```{lang}\n{code}\n```\n\n"

            elif node_type == "blockquote":
                quote_text = "".join(process_node(child) for child in children)
                lines = quote_text.strip().split("\n")
                return "\n".join(f"> {line}" for line in lines) + "\n\n"

            elif node_type == "horizontalRule":
                return "---\n\n"

            return "".join(process_node(child) for child in children)

        return process_node(content).strip()

    @staticmethod
    def extract_notes_content(doc: dict[str, Any]) -> str:
        """Extract the notes content from a document as markdown."""
        last_viewed = doc.get("last_viewed_panel")
        if (
            last_viewed
            and isinstance(last_viewed, dict)
            and last_viewed.get("content")
            and isinstance(last_viewed["content"], dict)
            and last_viewed["content"].get("type") == "doc"
        ):
            return GranolaClient.prosemirror_to_markdown(last_viewed["content"])

        return ""


def _client() -> GranolaClient:
    return GranolaClient()
