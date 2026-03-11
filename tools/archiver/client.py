"""ArchiverClient — pattern wrapper around archiver modules."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from shared.tool_sdk import secret

from .download.orchestrator import download_source
from .ingest.parse import parse_manifest
from .utils import (
    FileRecord,
    compute_file_hash,
    detect_mime_type,
    dump_json,
    file_record_to_dict,
)


class ArchiverClient:
    """Extraction-first client for investment document parsing."""

    _SLACK_ALLOWED_HOSTS = {"files.slack.com", "files-pri.slack.com", "slack.com"}

    def _download_slack_file(self, url: str, filename: str, output_dir: Path) -> Path:
        host = (urlparse(url).hostname or "").lower()
        if host not in self._SLACK_ALLOWED_HOSTS:
            raise ValueError(f"Refusing to send Slack token to non-Slack host: {host}")

        token = secret("SLACK_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN not set")

        safe_name = Path(filename or Path(urlparse(url).path).name or "slack-file").name or "slack-file"
        target = output_dir / safe_name
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = output_dir / f"{stem}-{counter}{suffix}"
            counter += 1

        with httpx.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120.0,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            with target.open("wb") as fh:
                for chunk in response.iter_bytes():
                    if chunk:
                        fh.write(chunk)
        return target

    def download(
        self,
        source_url: str,
        output_dir: str,
        company: str | None = None,
        account: str | None = None,
        password: str | None = None,
        email: str | None = None,
        max_depth: int = 3,
    ) -> dict:
        output_dir = Path(output_dir)
        payload = download_source(
            source_url=source_url,
            output_dir=output_dir,
            company=company,
            account=account,
            password=password,
            email=email,
            max_depth=max_depth,
        )
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(dump_json(payload))
        return payload

    def _manifest_with_context(
        self, manifest_path: Path, context: dict | None
    ) -> Path:
        if not context:
            return manifest_path
        data = json.loads(manifest_path.read_text())
        existing = data.get("context") or {}
        data["context"] = {**existing, **context}
        tmp = Path(tempfile.mktemp(suffix=".ctx.json", dir=manifest_path.parent))
        tmp.write_text(json.dumps(data))
        return tmp

    def parse(self, manifest_path: str, context: dict | None = None) -> dict:
        path = Path(manifest_path)
        path = self._manifest_with_context(path, context)
        return parse_manifest(path)

    def extract_manifest(self, manifest_path: str, context: dict | None = None) -> dict:
        """Reducto-first extraction from an existing manifest."""
        return self.parse(manifest_path, context=context)

    def _build_manifest_from_local_files(
        self,
        file_paths: list[str],
        context: dict | None = None,
        source_url: str | None = None,
    ) -> Path:
        records: list[dict[str, Any]] = []
        for raw in file_paths:
            path = Path(raw).expanduser()
            if path.exists() and path.is_file():
                resolved = path.resolve()
                record = FileRecord(
                    source_url=source_url or "local://manual",
                    source_type="local",
                    file_path=str(resolved),
                    filename=resolved.name,
                    file_hash=compute_file_hash(resolved),
                    size_bytes=resolved.stat().st_size,
                    mime_type=detect_mime_type(resolved),
                )
                records.append(file_record_to_dict(record))
            else:
                records.append(
                    {
                        "source_url": source_url or "local://manual",
                        "source_type": "local",
                        "file_path": str(path),
                        "filename": path.name,
                        "file_hash": "",
                        "size_bytes": 0,
                        "mime_type": None,
                        "status": "error",
                        "error": "File not found",
                    }
                )

        payload: dict[str, Any] = {
            "status": "ok",
            "source_url": source_url or "local://manual",
            "source_type": "local",
            "files": records,
        }
        if context:
            payload["context"] = context

        manifest_path = Path(tempfile.mktemp(suffix=".manifest.json"))
        manifest_path.write_text(json.dumps(payload))
        return manifest_path

    def extract_files(
        self,
        file_paths: list[str],
        context: dict | None = None,
        source_url: str | None = None,
    ) -> dict:
        """Reducto-first extraction directly from local files."""
        if not file_paths:
            return {"status": "error", "error": "file_paths cannot be empty", "files": []}
        manifest = self._build_manifest_from_local_files(
            file_paths=file_paths,
            context=context,
            source_url=source_url,
        )
        return parse_manifest(manifest)

    def extract_slack_files(
        self,
        files: list[dict[str, str]],
        context: dict | None = None,
    ) -> dict:
        """Download Slack private files and run Reducto extraction on them."""
        if not files:
            return {"status": "error", "error": "files cannot be empty", "files": []}

        download_dir = Path(tempfile.mkdtemp(prefix="archiver-slack-"))
        try:
            return self._extract_slack_files_inner(files, context, download_dir)
        finally:
            shutil.rmtree(download_dir, ignore_errors=True)

    def _extract_slack_files_inner(
        self,
        files: list[dict[str, str]],
        context: dict | None,
        download_dir: Path,
    ) -> dict:
        downloaded_paths: list[str] = []
        download_errors: list[dict[str, Any]] = []

        for index, item in enumerate(files):
            url = str(item.get("url") or "").strip()
            name = str(item.get("name") or "").strip()
            fallback_name = name or f"slack-file-{index + 1}"
            if not url:
                download_errors.append(
                    {
                        "source_url": "slack://upload",
                        "source_type": "slack",
                        "file_path": "",
                        "filename": fallback_name,
                        "file_hash": "",
                        "size_bytes": 0,
                        "mime_type": None,
                        "status": "error",
                        "error": "Missing file URL",
                    }
                )
                continue
            try:
                downloaded = self._download_slack_file(url, fallback_name, download_dir)
                downloaded_paths.append(str(downloaded))
            except Exception as exc:
                download_errors.append(
                    {
                        "source_url": url,
                        "source_type": "slack",
                        "file_path": str(download_dir / fallback_name),
                        "filename": fallback_name,
                        "file_hash": "",
                        "size_bytes": 0,
                        "mime_type": None,
                        "status": "error",
                        "error": str(exc),
                    }
                )

        if not downloaded_paths:
            return {
                "status": "error",
                "error": "Slack file download failed",
                "source": "slack://upload",
                "files": download_errors,
            }

        parsed = self.extract_files(
            file_paths=downloaded_paths,
            context=context,
            source_url="slack://upload",
        )
        parsed_files = parsed.get("files")
        if isinstance(parsed_files, list) and download_errors:
            parsed_files.extend(download_errors)
        if download_errors and parsed.get("status") == "ok":
            parsed["status"] = "partial"
        return parsed

    def extract_source(
        self,
        source_url: str,
        output_dir: str,
        company: str | None = None,
        account: str | None = None,
        password: str | None = None,
        email: str | None = None,
        max_depth: int = 3,
        context: dict | None = None,
    ) -> dict:
        """Download source and run Reducto extraction in one call."""
        download_payload = self.download(
            source_url=source_url,
            output_dir=output_dir,
            company=company,
            account=account,
            password=password,
            email=email,
            max_depth=max_depth,
        )
        if download_payload.get("status") != "ok":
            return {
                "status": "error",
                "error": "Download stage failed",
                "source": source_url,
                "download": download_payload,
                "files": [],
            }
        if not download_payload.get("files"):
            return {
                "status": "error",
                "error": "Download stage produced no files",
                "source": source_url,
                "download": download_payload,
                "files": [],
            }
        manifest_path = Path(output_dir) / "manifest.json"
        return self.extract_manifest(str(manifest_path), context=context)


def _client() -> ArchiverClient:
    return ArchiverClient()
