from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from shared.models import EmbeddingRecord

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md"}


@dataclass(frozen=True)
class ExtractedMemo:
    document_id: str
    memo_name: str
    relative_path: str
    content_hash: str
    content: str
    stage_hint: str
    type_hint: str


def _normalize_paths(paths: list[str]) -> list[Path]:
    normalized: list[Path] = []
    for raw in paths:
        value = raw.strip()
        if not value:
            continue
        normalized.append(Path(value).expanduser())
    return normalized


def resolve_memo_paths(paths: tuple[str, ...]) -> list[Path]:
    if paths:
        return _normalize_paths(list(paths))
    return []


def discover_memo_files(paths: list[Path], max_files: int = 3000) -> list[Path]:
    discovered: list[Path] = []
    for base in paths:
        if base.is_file():
            if base.suffix.lower() in SUPPORTED_EXTENSIONS:
                discovered.append(base.resolve())
            continue
        if not base.exists() or not base.is_dir():
            continue
        for file in sorted(base.rglob("*")):
            if not file.is_file():
                continue
            if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            discovered.append(file.resolve())
            if len(discovered) >= max_files:
                return discovered
    return discovered


def _infer_stage(name: str) -> str:
    lowered = name.lower()
    if "pre-seed" in lowered:
        return "pre-seed"
    if "seed" in lowered:
        return "seed"
    if "series a" in lowered or "a+" in lowered:
        return "series_a"
    if "series b" in lowered:
        return "series_b"
    if "series c" in lowered:
        return "series_c"
    if "series d" in lowered:
        return "series_d"
    if "update" in lowered:
        return "update"
    if "one-pager" in lowered or "one pager" in lowered:
        return "one_pager"
    return "unknown"


def _infer_company_type(name: str) -> str:
    lowered = name.lower()
    protocol_markers = (
        "protocol",
        "l2",
        "dex",
        "staking",
        "token",
        "dao",
        "defi",
        "onchain",
        "rollup",
    )
    ai_markers = ("ai ", " ai", "model", "llm", "inference")
    public_markers = ("equities", "public", "earnings", "macro")
    if any(marker in lowered for marker in protocol_markers):
        return "crypto_protocol"
    if any(marker in lowered for marker in ai_markers):
        return "ai_startup"
    if any(marker in lowered for marker in public_markers):
        return "public_equities"
    if any(marker in lowered for marker in ("series", "seed", "memo", "one-pager", "one pager")):
        return "software_business"
    return "unknown"


def _load_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError(
            "pypdf is required for PDF ingest. Install with `uv add pypdf` before running ingest."
        ) from exc
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(text)
    return "\n\n".join(pages).strip()


def _load_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return _load_pdf_text(path)
    return path.read_text(errors="ignore").strip()


def _document_id(relative_path: str) -> str:
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]


def _hash_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _chunk_text(content: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", content).strip()
    if not normalized:
        return []
    if len(normalized) <= chunk_chars:
        return [normalized]

    paragraphs = [paragraph.strip() for paragraph in normalized.split("\n\n") if paragraph.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= chunk_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap_chars:] if overlap_chars > 0 else ""
            current = f"{tail}\n\n{paragraph}".strip()
        else:
            chunks.append(paragraph[:chunk_chars])
            current = paragraph[max(chunk_chars - overlap_chars, 0) :]
    if current:
        chunks.append(current)
    return chunks


def extract_memos(files: list[Path], root_paths: list[Path]) -> tuple[list[ExtractedMemo], list[str]]:
    memos: list[ExtractedMemo] = []
    skipped: list[str] = []
    for file in files:
        try:
            content = _load_text(file)
        except Exception as exc:
            skipped.append(f"{file}: {exc}")
            continue
        if not content:
            skipped.append(f"{file}: empty_content")
            continue

        relative = str(file)
        for root in root_paths:
            try:
                relative = str(file.relative_to(root))
                break
            except ValueError:
                continue

        memo_name = file.name
        memos.append(
            ExtractedMemo(
                document_id=_document_id(relative),
                memo_name=memo_name,
                relative_path=relative,
                content_hash=_hash_text(content),
                content=content,
                stage_hint=_infer_stage(memo_name),
                type_hint=_infer_company_type(memo_name),
            )
        )
    return memos, skipped


def build_embedding_records(
    memos: list[ExtractedMemo],
    source: str,
    kind: str,
    chunk_chars: int,
    overlap_chars: int,
) -> list[EmbeddingRecord]:
    records: list[EmbeddingRecord] = []
    for memo in memos:
        chunks = _chunk_text(memo.content, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
        for index, chunk in enumerate(chunks):
            source_id = f"{memo.document_id}:{memo.content_hash[:12]}:{index}"
            records.append(
                EmbeddingRecord(
                    source=source,
                    kind=kind,
                    source_id=source_id,
                    content=chunk[:8000],
                    metadata={
                        "document_id": memo.document_id,
                        "memo_name": memo.memo_name,
                        "relative_path": memo.relative_path,
                        "content_hash": memo.content_hash,
                        "stage_hint": memo.stage_hint,
                        "type_hint": memo.type_hint,
                        "chunk_index": index,
                    },
                )
            )
    return records
