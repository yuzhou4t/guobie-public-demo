from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.collectors.base import (
    CandidateDocument,
    Diagnostic,
    FetchedResource,
    ParseResult,
    clean_text,
    official_url,
    parse_datetime,
    tuple_of_text,
)


def _fallback_title(url: str) -> str | None:
    filename = unquote(PurePosixPath(urlsplit(url).path).name)
    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]
    return clean_text(filename.replace("_", " ").replace("-", " "))


class PdfCollector:
    collector_type = "pdf"

    def parse(
        self,
        resource: FetchedResource,
        config: Mapping[str, Any],
        *,
        link_role: str = "unknown",
    ) -> ParseResult:
        profile = config.get("profile")
        if profile == "bounded_metadata":
            return self._parse_bounded_metadata(resource, link_role=link_role)
        if profile is not None:
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_pdf_config",
                        message=f"unsupported PDF profile: {profile}",
                        level="error",
                    ),
                )
            )
        if not resource.body.lstrip().startswith(b"%PDF-"):
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_pdf_header",
                        message="resource does not start with a PDF file header",
                        level="error",
                    ),
                )
            )

        try:
            reader = PdfReader(BytesIO(resource.body), strict=False)
        except (PdfReadError, OSError, ValueError) as exc:
            return ParseResult(diagnostics=(Diagnostic(code="invalid_pdf", message=str(exc), level="error"),))

        diagnostics: list[Diagnostic] = []
        if reader.is_encrypted:
            try:
                decrypted = reader.decrypt("")
            except (PdfReadError, TypeError, ValueError):
                decrypted = 0
            if not decrypted:
                diagnostics.append(
                    Diagnostic(
                        code="pdf_encrypted",
                        message="PDF is encrypted and cannot be parsed without a password",
                        level="error",
                    )
                )
                candidate = CandidateDocument(
                    discovery_url=resource.request_url,
                    canonical_url=official_url(resource.final_url, link_role),
                    title=_fallback_title(resource.final_url),
                    media_type="application/pdf",
                    resource_sha256=resource.sha256,
                    locator=resource.final_url,
                    source_metadata={"encrypted": True},
                )
                return ParseResult(candidates=(candidate,), diagnostics=tuple(diagnostics))

        metadata = reader.metadata
        page_text: list[str] = []
        for index, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except (KeyError, TypeError, ValueError) as exc:
                diagnostics.append(
                    Diagnostic(
                        code="pdf_page_parse_failed",
                        message=str(exc),
                        locator=f"pages.{index + 1}",
                    )
                )
                continue
            if text.strip():
                page_text.append(text.strip())

        body_text = "\n\n".join(page_text).strip() or None
        if body_text is None:
            diagnostics.append(
                Diagnostic(
                    code="ocr_required",
                    message="PDF has no extractable text layer and requires OCR",
                )
            )

        metadata_title = clean_text(metadata.title) if metadata else None
        metadata_author = clean_text(metadata.author) if metadata else None
        metadata_subject = clean_text(metadata.subject) if metadata else None
        creation_date = parse_datetime(metadata.creation_date) if metadata else None
        modification_date = parse_datetime(metadata.modification_date) if metadata else None
        source_metadata = {
            "page_count": len(reader.pages),
            **({"creation_date": creation_date.isoformat()} if creation_date else {}),
            **({"modification_date": modification_date.isoformat()} if modification_date else {}),
        }
        candidate = CandidateDocument(
            discovery_url=resource.request_url,
            canonical_url=official_url(resource.final_url, link_role),
            title=metadata_title or _fallback_title(resource.final_url),
            authors=tuple_of_text(metadata_author),
            summary=metadata_subject,
            body_text=body_text,
            media_type="application/pdf",
            resource_sha256=resource.sha256,
            locator=resource.final_url,
            source_metadata=source_metadata,
        )
        return ParseResult(candidates=(candidate,), diagnostics=tuple(diagnostics))

    def _parse_bounded_metadata(
        self,
        resource: FetchedResource,
        *,
        link_role: str,
    ) -> ParseResult:
        if resource.status_code != 206 or not resource.body.startswith(b"%PDF-"):
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_pdf_prefix",
                        message="bounded PDF metadata requires a verified partial PDF response",
                        level="error",
                    ),
                )
            )
        try:
            total_bytes = int(resource.headers["content-range"].rsplit("/", 1)[1])
        except (KeyError, ValueError, IndexError):
            return ParseResult(
                diagnostics=(
                    Diagnostic(
                        code="invalid_content_range",
                        message="bounded PDF metadata requires a total byte count",
                        level="error",
                    ),
                )
            )

        source_metadata = {
            "metadata_only": True,
            "partial_content": True,
            "content_length": total_bytes,
            "prefix_sha256": resource.sha256,
            **({"etag": resource.etag} if resource.etag else {}),
            **({"last_modified": resource.last_modified} if resource.last_modified else {}),
        }
        candidate = CandidateDocument(
            discovery_url=resource.request_url,
            canonical_url=official_url(resource.final_url, link_role),
            title=_fallback_title(resource.final_url),
            media_type="application/pdf",
            resource_sha256=None,
            locator=resource.final_url,
            source_metadata=source_metadata,
        )
        return ParseResult(candidates=(candidate,))
