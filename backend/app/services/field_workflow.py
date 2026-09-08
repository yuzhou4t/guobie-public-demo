"""S05 keeps uploaded bytes, immutable text versions and independent annotations."""

import io
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
from sqlalchemy import func, select

from app.models import FieldMaterial
from app.models.skill_workflows import FieldAnnotation, FieldMaterialProfile, FieldSegment, FieldTextVersion
from app.services import field_ai_access, workflow_runtime
from app.services.field_materials import FieldMaterialValidationError, resolve_field_material_path
from app.services.skill_execution_control import report_progress

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_units(body):
    try:
        with ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(x.file_size for x in entries) > 30_000_000:
                raise ValueError("DOCX 解压内容过大")
            if "word/vbaProject.bin" in archive.namelist():
                raise ValueError("不接受带宏文档")
            xml = archive.read("word/document.xml")
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise ValueError("不接受带外部实体的文档")
        root = ET.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        result = []
        for index, paragraph in enumerate(root.iter(ns + "p"), 1):
            text = "".join(
                (el.text or "") if el.tag == ns + "t" else "\t" if el.tag == ns + "tab" else "\n"
                for el in paragraph.iter()
                if el.tag in {ns + "t", ns + "tab", ns + "br"}
            )
            result.append({"locator": f"paragraph:{index}", "text": text})
        return result
    except (BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
        raise FieldMaterialValidationError(f"DOCX 文本解析失败：{exc}") from exc


def extract_units(body, content_type, *, selected_article=None):
    if content_type == DOCX_TYPE:
        units = docx_units(body)
    elif content_type == "application/pdf":
        pdf = PdfReader(io.BytesIO(body))
        if len(pdf.pages) > 200:
            raise FieldMaterialValidationError("PDF 超过 200 页，请导入选定章节")
        units = [
            {"locator": f"page:{i}", "text": page.extract_text() or ""} for i, page in enumerate(pdf.pages, 1)
        ]
    elif content_type in {"text/plain", "text/markdown"}:
        units = [
            {"locator": f"line:{i}", "text": line}
            for i, line in enumerate(body.decode("utf-8").splitlines(), 1)
        ]
    else:
        units = []
    if selected_article:
        from app.services.tracking_workflow import article_units

        units = article_units(units, selected_article)
    if sum(len(x["text"]) for x in units) > 300_000:
        raise FieldMaterialValidationError("文本超过 30 万字，请导入选定章节；未截断原文")
    return units


def material_units(material):
    path = resolve_field_material_path(material.storage_key, expected_sha256=material.sha256)
    return extract_units(path.read_bytes(), material.content_type)


def latest_version(db, material_id, kind=None):
    query = select(FieldTextVersion).where(FieldTextVersion.material_id == material_id)
    if kind:
        query = query.where(FieldTextVersion.kind == kind)
    return db.scalar(query.order_by(FieldTextVersion.version_no.desc()).limit(1))


def add_version(db, material, units, kind, actor_id, *, parent=None, run_id=None, note=""):
    db.scalar(select(FieldMaterial).where(FieldMaterial.id == material.id).with_for_update())
    number = (
        db.scalar(
            select(func.max(FieldTextVersion.version_no)).where(FieldTextVersion.material_id == material.id)
        )
        or 0
    )
    row = FieldTextVersion(
        material_id=material.id,
        version_no=number + 1,
        kind=kind,
        parent_id=parent.id if parent else None,
        run_id=run_id,
        units=units,
        created_by=actor_id,
        note=note,
    )
    db.add(row)
    db.flush()
    return row


def ensure_raw(db, material, actor_id):
    row = latest_version(db, material.id, "raw")
    if row is None:
        row = add_version(
            db, material, material_units(material), "raw", actor_id, note="导入原文；上传字节独立保留"
        )
    return row


def execute(db, run, config):
    ids = list(dict.fromkeys(config.get("material_ids") or []))
    if not ids or len(ids) > 10:
        raise ValueError("请选择 1 至 10 份同项目材料")
    result = {
        "type": "field_research_workflow",
        "material_ids": [],
        "segment_ids": [],
        "version_ids": [],
        "limitations": [],
    }
    # Validate every model result before writing any derived result.
    prepared = []
    for material_index, material_id in enumerate(ids, 1):
        report_progress(f"读取第 {material_index}/{len(ids)} 份材料，核对授权和原文定位")
        with field_ai_access.authorized_source(db, run, material_id) as access:
            material = access.material
            raw = ensure_raw(db, material, run.requested_by)
            if not any(x["text"].strip() for x in raw.units):
                result["material_ids"].append(material.id)
                result["limitations"].append(
                    f"资料 #{material.id} 无文本层；仅保留图片／扫描件与说明，需补充转写。"
                )
                continue
            profile = db.get(FieldMaterialProfile, material.id)
            report_progress(
                f"原文已读取：{len(raw.units)} 个文本单元，{sum(len(x['text']) for x in raw.units)} 字"
            )
            draft = workflow_runtime.organize(
                raw.units, profile.context if profile else {}, config.get("instruction", "")
            )
            prepared.append((material, raw, draft))
    report_progress("全部批次已校验，正在保存候选整理稿与研究标注")
    for material, raw, draft in prepared:
        clean = add_version(
            db,
            material,
            [x.model_dump() for x in draft.clean_units],
            "clean",
            run.requested_by,
            parent=raw,
            run_id=run.id,
            note="AI 候选整理稿；人工修订另存版本",
        )
        result["material_ids"].append(material.id)
        result["version_ids"].append(clean.id)
        result["limitations"].extend(draft.limitations)
        for candidate in draft.candidates:
            segment = FieldSegment(
                material_id=material.id,
                run_id=run.id,
                raw_version_id=raw.id,
                clean_version_id=clean.id,
                positions={"start": candidate.start_locator, "end": candidate.end_locator},
                original_text=candidate.original_text,
            )
            db.add(segment)
            db.flush()
            payload = candidate.model_dump(exclude={"start_locator", "end_locator", "original_text"})
            db.add(
                FieldAnnotation(
                    segment_id=segment.id,
                    revision_no=1,
                    payload=payload,
                    status="candidate",
                    created_by=run.requested_by,
                )
            )
            result["segment_ids"].append(segment.id)
    result["segment_count"] = len(result["segment_ids"])
    result["result_status"] = "candidate"
    return result
