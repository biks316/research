"""Generate .docx/.pdf exports on first request, cache them in S3, and stream them back."""

from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

import structlog

from app.pipeline import exporter
from app.repositories.reports import ReportRepository
from app.services.report_service import ReportService
from app.services.storage import CHUNK_SIZE, Storage

log = structlog.get_logger(__name__)

MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


@dataclass
class Export:
    chunks: AsyncIterator[bytes]
    media_type: str
    filename: str
    size: int


def safe_filename(title: str, ext: str, max_length: int = 120) -> str:
    """A filesystem- and header-safe file name derived from the report title."""
    name = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .") or "report"
    return f"{name[:max_length].rstrip(' .')}.{ext}"


def content_disposition(filename: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode() or "report"
    ascii_name = ascii_name.replace('"', "'")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


async def _iter_bytes(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), CHUNK_SIZE):
        yield data[start : start + CHUNK_SIZE]


class ExportService:
    def __init__(self, reports: ReportService, storage: Storage) -> None:
        self._reports = reports
        self._storage = storage

    async def export(self, user_id: str, report_id: uuid.UUID, fmt: str) -> Export:
        report = await self._reports.get_report(user_id, report_id)  # 404 / 409 REPORT_NOT_READY
        async with self._reports.db.session() as session:
            row = await ReportRepository(session).get(report_id)
        version = row.content_version if row else 1
        key = f"exports/{report_id}/{version}.{fmt}"
        filename = safe_filename(report.title, fmt)
        media_type = MEDIA_TYPES[fmt]

        size = await self._storage.size(key)
        if size is not None:
            log.info("export_cache_hit", key=key)
            return Export(self._storage.stream(key), media_type, filename, size)

        render = exporter.render_docx if fmt == "docx" else exporter.render_pdf
        data = await asyncio.to_thread(render, report)
        await self._storage.put(key, data, media_type)
        log.info("export_generated", key=key, bytes=len(data))
        return Export(_iter_bytes(data), media_type, filename, len(data))
