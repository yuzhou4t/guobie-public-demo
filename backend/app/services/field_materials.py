from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings


class FieldMaterialValidationError(ValueError):
    pass


_TYPE_BY_SUFFIX = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
_CANONICAL_SUFFIX = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


@dataclass(frozen=True)
class StoredFieldMaterial:
    storage_key: str
    path: Path
    content_type: str
    byte_size: int
    sha256: str
    created: bool


def _storage_root(settings: Settings) -> Path:
    configured = settings.field_material_storage_dir.expanduser()
    if configured.is_absolute():
        return configured.resolve()
    project_root = Path(__file__).resolve().parents[3]
    return (project_root / configured).resolve()


def _validate_filename(filename: str) -> tuple[str, str]:
    normalized = filename.strip()
    if (
        not normalized
        or len(normalized) > 240
        or "\x00" in normalized
        or "/" in normalized
        or "\\" in normalized
        or normalized in {".", ".."}
    ):
        raise FieldMaterialValidationError("文件名无效")
    suffix = Path(normalized).suffix.lower()
    if suffix not in _TYPE_BY_SUFFIX:
        raise FieldMaterialValidationError("仅支持 TXT、Markdown、DOCX、PDF、JPG 和 PNG")
    return normalized, suffix


def _decode_content(content_base64: str, *, maximum_bytes: int) -> bytes:
    encoded = content_base64.strip()
    if not encoded or len(encoded) > ((maximum_bytes + 2) // 3) * 4 + 8:
        raise FieldMaterialValidationError(f"单个文件不得超过 {maximum_bytes // 1024 // 1024} MB")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FieldMaterialValidationError("文件内容不是有效的 Base64") from exc
    if not body:
        raise FieldMaterialValidationError("不能上传空文件")
    if len(body) > maximum_bytes:
        raise FieldMaterialValidationError(f"单个文件不得超过 {maximum_bytes // 1024 // 1024} MB")
    return body


def _validate_content(body: bytes, *, suffix: str, reported_content_type: str) -> str:
    expected_content_type = _TYPE_BY_SUFFIX[suffix]
    normalized_reported = reported_content_type.split(";", 1)[0].strip().lower()
    if suffix == ".docx":
        from app.services.field_workflow import docx_units

        docx_units(body)
    if normalized_reported not in {"", "application/octet-stream", expected_content_type}:
        raise FieldMaterialValidationError("文件扩展名与浏览器报告的内容类型不一致")
    if expected_content_type == "application/pdf" and not body.startswith(b"%PDF-"):
        raise FieldMaterialValidationError("PDF 文件头校验失败")
    if expected_content_type == "image/png" and not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FieldMaterialValidationError("PNG 文件头校验失败")
    if expected_content_type == "image/jpeg" and not body.startswith(b"\xff\xd8\xff"):
        raise FieldMaterialValidationError("JPG 文件头校验失败")
    if expected_content_type.startswith("text/"):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FieldMaterialValidationError("文本文件必须使用 UTF-8 编码") from exc
        if "\x00" in text:
            raise FieldMaterialValidationError("文本文件包含不允许的空字节")
    return expected_content_type


def store_field_material(
    *,
    filename: str,
    reported_content_type: str,
    content_base64: str,
    settings: Settings | None = None,
) -> StoredFieldMaterial:
    active_settings = settings or get_settings()
    _, suffix = _validate_filename(filename)
    body = _decode_content(
        content_base64,
        maximum_bytes=active_settings.field_material_max_bytes,
    )
    content_type = _validate_content(
        body,
        suffix=suffix,
        reported_content_type=reported_content_type,
    )
    digest = hashlib.sha256(body).hexdigest()
    storage_key = f"{digest[:2]}/{digest}{_CANONICAL_SUFFIX[content_type]}"
    root = _storage_root(active_settings)
    destination = (root / storage_key).resolve()
    if not destination.is_relative_to(root):
        raise FieldMaterialValidationError("文件存储路径无效")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise FieldMaterialValidationError("已存在文件的完整性校验失败")
        created = False
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".field-material-", dir=destination.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination)
            created = True
        finally:
            temporary_path.unlink(missing_ok=True)
    return StoredFieldMaterial(
        storage_key=storage_key,
        path=destination,
        content_type=content_type,
        byte_size=len(body),
        sha256=digest,
        created=created,
    )


def resolve_field_material_path(
    storage_key: str,
    *,
    expected_sha256: str,
    settings: Settings | None = None,
) -> Path:
    active_settings = settings or get_settings()
    root = _storage_root(active_settings)
    path = (root / storage_key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError(storage_key)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise FieldMaterialValidationError("上传文件完整性校验失败")
    return path
