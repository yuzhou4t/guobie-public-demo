"""Authenticated country collections: public descriptions never expose originals."""

import base64
import io
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select

from app.api.research import (
    DbSession,
    ReaderActor,
    _case_permission,
    _field_material_summary,
    _require_catalog_country,
    require_internal_reader_access,
    require_reader_write_access,
)
from app.core.config import get_settings
from app.models import CapabilityRun, FieldMaterial, ResearchCase, User
from app.models.field_community import (
    FieldAssetVersion,
    FieldComment,
    FieldProjectGrant,
    FieldProjectLink,
    FieldPublication,
    FieldRunRightsReview,
    ReaderAccount,
)
from app.models.skill_workflows import FieldMaterialProfile
from app.services import field_access
from app.services.community_auth import utc
from app.services.field_materials import resolve_field_material_path, store_field_material


def registered(db: DbSession, actor: ReaderActor):
    account = db.get(ReaderAccount, actor.id)
    if not account or not account.password_hash:
        raise HTTPException(401, "请先建立或登录平台账号")


router = APIRouter(
    prefix="/api/v1/reader/field-library",
    tags=["field-community"],
    dependencies=[Depends(require_internal_reader_access), Depends(registered)],
)
WRITE = [Depends(require_reader_write_access)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicationInput(Input):
    base_revision_no: int = Field(default=0, ge=0)
    public_title: str = Field(min_length=1, max_length=240)
    introduction: str = Field(default="", max_length=3000)
    excerpts: str = Field(default="", max_length=6000)
    share_image: bool = False
    status: Literal["draft", "published", "withdrawn"] = "draft"


class GrantRequest(Input):
    research_case_id: int = Field(gt=0)
    asset_version_id: int = Field(gt=0)
    purpose: str = Field(min_length=3, max_length=1500)
    permissions: list[Literal["read", "ai", "cite", "download"]] = Field(default_factory=lambda: ["read"])
    ai_provider: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def read_required(self):
        if "read" not in self.permissions:
            raise ValueError("项目使用申请必须包含阅读权限")
        return self


class GrantDecision(Input):
    status: Literal["approved", "rejected"]
    permissions: list[Literal["read", "ai", "cite", "download"]]
    expires_at: datetime | None = None


class ProjectLink(Input):
    research_case_id: int = Field(gt=0)


class CommentInput(Input):
    body: str = Field(min_length=1, max_length=3000)
    parent_id: int | None = Field(default=None, gt=0)


class CommentRevision(Input):
    base_revision_no: int
    body: str | None = Field(default=None, min_length=1, max_length=3000)
    hidden: bool | None = None


def material_or_404(db, material_id):
    material = db.get(FieldMaterial, material_id)
    if not material:
        raise HTTPException(404, "资料不可用")
    return material


def owned(db, material_id, actor):
    material = material_or_404(db, material_id)
    if material.owner_id != actor.id:
        raise HTTPException(403, "只有上传者可以管理这份资料")
    return material


def public_payload(db, material, publication):
    author = db.get(User, material.owner_id)
    return {
        "id": material.id,
        "asset_version_id": publication.asset_version_id,
        "country_iso3": material.country_iso3,
        "title": publication.public_title,
        "introduction": publication.introduction,
        "excerpts": publication.excerpts,
        "author": author.display_name,
        "material_type": material.material_type,
        "visibility": "published",
        "publication_revision": publication.revision_no,
        "image_url": f"/api/v1/reader/field-library/{material.id}/public-image"
        if publication.image_key
        else None,
    }


def private_payload(db, material, project_id=None):
    profile = db.get(FieldMaterialProfile, material.id)
    asset = db.scalar(
        select(FieldAssetVersion)
        .where(FieldAssetVersion.material_id == material.id)
        .order_by(FieldAssetVersion.version_no.desc())
        .limit(1)
    )
    payload = {
        **_field_material_summary(material),
        "context": profile.context if profile else {},
        "asset_version_id": asset.id if asset else None,
        "visibility": "private",
        "project_id": project_id,
    }
    if material.content_type in {"image/jpeg", "image/png"}:
        payload["original_image_url"] = f"/api/v1/reader/field-library/{material.id}/image" + (
            f"?research_case_id={project_id}" if project_id else ""
        )
    if project_id:
        payload["download_url"] += f"?research_case_id={project_id}"
    return payload


@router.get("/context")
def context(db: DbSession, actor: ReaderActor):
    cases = []
    for case in db.scalars(select(ResearchCase).where(ResearchCase.status == "active")):
        try:
            _case_permission(db, case.id, actor, "edit")
        except HTTPException:
            continue
        cases.append(
            {"id": case.id, "title": case.title, "country_iso3": (case.scope or {}).get("country_iso3")}
        )
    return {
        "projects": cases,
        "ai_provider": get_settings().agent_runtime,
        "ai_model": get_settings().agent_model,
        "upload_max_bytes": get_settings().field_material_max_bytes,
    }


@router.get("")
def listing(
    db: DbSession,
    actor: ReaderActor,
    response: Response,
    country_iso3: str = Query(pattern=r"^[A-Z]{3}$"),
    view: Literal["shared", "mine", "authorized"] = "shared",
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
    before_id: int | None = Query(default=None, gt=0),
):
    _require_catalog_country(country_iso3)
    statement = select(FieldMaterial).where(FieldMaterial.country_iso3 == country_iso3)
    if view == "shared":
        statement = statement.join(FieldPublication, FieldPublication.material_id == FieldMaterial.id).where(
            FieldPublication.status == "published"
        )
        if q:
            statement = statement.where(
                or_(
                    FieldPublication.public_title.icontains(q, autoescape=True),
                    FieldPublication.introduction.icontains(q, autoescape=True),
                )
            )
    elif view == "mine":
        statement = statement.where(FieldMaterial.owner_id == actor.id)
        if q:
            statement = statement.where(FieldMaterial.title.icontains(q, autoescape=True))
    else:
        # Build the authorized set without exposing private counts or metadata from other grants.
        allowed = set()
        for grant, asset in db.execute(
            select(FieldProjectGrant, FieldAssetVersion)
            .join(FieldAssetVersion, FieldAssetVersion.id == FieldProjectGrant.asset_version_id)
            .join(FieldMaterial, FieldMaterial.id == FieldAssetVersion.material_id)
            .where(FieldMaterial.country_iso3 == country_iso3, FieldProjectGrant.status == "approved")
        ):
            try:
                field_access.resolve_field_material_access(
                    db, actor, asset.material_id, grant.research_case_id, require_link=False
                )
                allowed.add(asset.material_id)
            except field_access.FieldAccessDenied:
                continue
        statement = statement.where(FieldMaterial.id.in_(allowed))
        if q:
            statement = statement.where(FieldMaterial.title.icontains(q, autoescape=True))
    if before_id:
        statement = statement.where(FieldMaterial.id < before_id)
    rows = list(db.scalars(statement.order_by(FieldMaterial.id.desc()).limit(limit + 1)))
    items = []
    for material in rows[:limit]:
        if view == "shared":
            item = public_payload(db, material, db.get(FieldPublication, material.id))
        else:
            item = private_payload(db, material)
            # A project-less file URL cannot give a non-owner an implicit authorization.
            if material.owner_id != actor.id:
                item.pop("download_url", None)
        item["is_owner"] = material.owner_id == actor.id
        items.append(item)
    response.headers["Cache-Control"] = "private, no-store"
    return {"items": items, "next_before_id": rows[limit - 1].id if len(rows) > limit else None}


@router.get("/{material_id}")
def detail(
    material_id: int,
    db: DbSession,
    actor: ReaderActor,
    response: Response,
    research_case_id: int | None = Query(default=None, gt=0),
):
    material = material_or_404(db, material_id)
    publication = db.get(FieldPublication, material_id)
    private = material.owner_id == actor.id
    if research_case_id:
        field_access.resolve_field_material_access(
            db, actor, material_id, research_case_id, require_link=False
        )
        private = True
    if not private and (not publication or publication.status != "published"):
        raise HTTPException(404, "资料不可用")
    result = (
        private_payload(db, material, research_case_id)
        if private
        else public_payload(db, material, publication)
    )
    result["is_owner"] = material.owner_id == actor.id
    if result["is_owner"] and publication:
        result["publication"] = {**public_payload(db, material, publication), "status": publication.status}
    comments = []
    for comment in db.scalars(
        select(FieldComment)
        .where(FieldComment.material_id == material_id)
        .order_by(FieldComment.created_at, FieldComment.id)
        .limit(200)
    ):
        author = db.get(User, comment.author_id)
        comments.append(
            {
                "id": comment.id,
                "parent_id": comment.parent_id,
                "author": author.display_name,
                "body": "此评论已隐藏" if comment.hidden else comment.body,
                "hidden": comment.hidden,
                "created_at": comment.created_at,
                "revision_no": comment.revision_no,
                "can_edit": comment.author_id == actor.id,
                "can_hide": comment.author_id == actor.id or actor.role == "admin",
            }
        )
    result["comments"] = comments
    grants = []
    for grant in db.scalars(
        select(FieldProjectGrant).join(FieldAssetVersion).where(FieldAssetVersion.material_id == material_id)
    ):
        if material.owner_id != actor.id:
            try:
                _case_permission(db, grant.research_case_id, actor, "view")
            except HTTPException:
                continue
        case = db.get(ResearchCase, grant.research_case_id)
        grants.append(
            {
                "id": grant.id,
                "research_case_id": grant.research_case_id,
                "project_title": case.title,
                "purpose": grant.purpose,
                "permissions": grant.permissions,
                "ai_provider": grant.ai_provider,
                "status": grant.status,
                "expires_at": grant.expires_at,
                "asset_version_id": grant.asset_version_id,
            }
        )
    result["grants"] = grants
    response.headers["Cache-Control"] = "private, no-store"
    return result


def sanitized_image(material):
    path = resolve_field_material_path(material.storage_key, expected_sha256=material.sha256)
    try:
        with Image.open(path) as original:
            if original.width * original.height > 30_000_000:
                raise ValueError("图片像素超过公开预览上限")
            original.load()
            corrected = ImageOps.exif_transpose(original).convert("RGB")
            corrected.thumbnail((2048, 2048))
            # Recreate pixel-only image: EXIF, GPS, comments and text chunks are not copied.
            clean = Image.new("RGB", corrected.size)
            clean.paste(corrected)
            buffer = io.BytesIO()
            clean.save(buffer, format="PNG")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "图片无法生成安全预览，请检查格式与像素大小") from exc
    return store_field_material(
        filename="public-preview.png",
        reported_content_type="image/png",
        content_base64=base64.b64encode(buffer.getvalue()).decode(),
    )


@router.post("/{material_id}/publication", dependencies=WRITE)
def publish(material_id: int, payload: PublicationInput, db: DbSession, actor: ReaderActor):
    material = owned(db, material_id, actor)
    asset = field_access.ensure_asset(db, material)
    row = db.scalar(
        select(FieldPublication).where(FieldPublication.material_id == material_id).with_for_update()
    )
    if (row.revision_no if row else 0) != payload.base_revision_no:
        raise HTTPException(409, "公开介绍已有新版本，请刷新后编辑")
    if not payload.public_title.strip():
        raise HTTPException(422, "请填写公开标题")
    if row is None:
        row = FieldPublication(
            material_id=material_id,
            asset_version_id=asset.id,
            public_title=payload.public_title.strip(),
            revision_no=0,
        )
        db.add(row)
    image = None
    if payload.share_image:
        if material.content_type not in {"image/png", "image/jpeg"}:
            raise HTTPException(422, "只有图片资料可以附加公开图片预览")
        image = sanitized_image(material)
    row.public_title, row.introduction, row.excerpts = (
        payload.public_title.strip(),
        payload.introduction,
        payload.excerpts,
    )
    row.status, row.revision_no = payload.status, row.revision_no + 1
    row.image_key, row.image_sha256 = (image.storage_key, image.sha256) if image else (None, None)
    field_access.audit(
        db, actor.id, material_id, "publication_revised", status=row.status, revision=row.revision_no
    )
    db.commit()
    return {"id": material_id, "revision_no": row.revision_no, "status": row.status}


@router.get("/{material_id}/public-image")
def public_image(material_id: int, db: DbSession, actor: ReaderActor):
    row = db.get(FieldPublication, material_id)
    if not row or row.status != "published" or not row.image_key:
        raise HTTPException(404, "公开图片不可用")
    path = resolve_field_material_path(row.image_key, expected_sha256=row.image_sha256)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/{material_id}/requests", dependencies=WRITE)
def request_grant(material_id: int, payload: GrantRequest, db: DbSession, actor: ReaderActor):
    material = material_or_404(db, material_id)
    _case_permission(db, payload.research_case_id, actor, "edit")
    publication = db.get(FieldPublication, material_id)
    existing = db.scalar(
        select(FieldProjectGrant)
        .where(
            FieldProjectGrant.asset_version_id == payload.asset_version_id,
            FieldProjectGrant.research_case_id == payload.research_case_id,
        )
        .with_for_update()
    )
    if (
        material.owner_id != actor.id
        and not (publication and publication.status == "published")
        and not existing
    ):
        raise HTTPException(404, "资料不可用")
    asset = db.get(FieldAssetVersion, payload.asset_version_id)
    if not asset or asset.material_id != material_id:
        raise HTTPException(422, "请选择这份资料的有效版本")
    if existing and field_access.grant_alive(existing):
        raise HTTPException(409, "该项目已有有效授权；修改用途前需上传者撤销旧授权")
    if "ai" in payload.permissions and payload.ai_provider != get_settings().agent_runtime:
        raise HTTPException(422, "请核对当前 AI 处理服务")
    if existing:
        field_access.lock_grant_runs(db, existing)
    row = existing or FieldProjectGrant(
        asset_version_id=asset.id,
        research_case_id=payload.research_case_id,
        requested_by=actor.id,
        purpose=payload.purpose,
    )
    row.status, row.permissions, row.purpose = (
        "pending",
        sorted(set(payload.permissions)),
        payload.purpose.strip(),
    )
    row.requested_by, row.ai_provider = actor.id, payload.ai_provider if "ai" in payload.permissions else ""
    row.expires_at, row.decided_by, row.decided_at = None, None, None
    db.add(row)
    db.flush()
    field_access.audit(db, actor.id, material_id, "grant_requested", grant_id=row.id)
    db.commit()
    return {"id": row.id, "status": row.status}


@router.post("/grants/{grant_id}/decision", dependencies=WRITE)
def decide_grant(grant_id: int, payload: GrantDecision, db: DbSession, actor: ReaderActor):
    row = db.scalar(select(FieldProjectGrant).where(FieldProjectGrant.id == grant_id).with_for_update())
    if not row:
        raise HTTPException(404, "申请不存在")
    asset = db.get(FieldAssetVersion, row.asset_version_id)
    owned(db, asset.material_id, actor)
    if row.status != "pending":
        raise HTTPException(409, "这项申请已经处理")
    if not set(payload.permissions) <= set(row.permissions) or (
        payload.status == "approved" and "read" not in payload.permissions
    ):
        raise HTTPException(422, "批准范围须包含阅读，且不能超出申请用途")
    if payload.expires_at and utc(payload.expires_at) <= datetime.now(UTC):
        raise HTTPException(422, "授权截止时间应晚于当前时间")
    row.status, row.permissions, row.expires_at = (
        payload.status,
        sorted(set(payload.permissions)),
        payload.expires_at,
    )
    row.decided_by, row.decided_at = actor.id, datetime.now(UTC)
    field_access.audit(
        db,
        actor.id,
        asset.material_id,
        "grant_decided",
        grant_id=row.id,
        status=row.status,
        permissions=row.permissions,
    )
    db.commit()
    return {"id": row.id, "status": row.status}


@router.post("/grants/{grant_id}/revoke", dependencies=WRITE)
def revoke(grant_id: int, db: DbSession, actor: ReaderActor):
    row = db.scalar(select(FieldProjectGrant).where(FieldProjectGrant.id == grant_id).with_for_update())
    if not row:
        raise HTTPException(404, "授权不存在")
    field_access.revoke_grant(db, row, actor)
    db.commit()
    return {"id": row.id, "status": row.status}


@router.post("/{material_id}/projects", dependencies=WRITE)
def link_project(material_id: int, payload: ProjectLink, db: DbSession, actor: ReaderActor):
    material = material_or_404(db, material_id)
    _case_permission(db, payload.research_case_id, actor, "edit")
    case = db.get(ResearchCase, payload.research_case_id)
    country = (case.scope or {}).get("country_iso3")
    if country and material.country_iso3 and country != material.country_iso3:
        raise HTTPException(422, "请选择同一国家的研究项目")
    if material.owner_id == actor.id:
        link = field_access.owner_project_link(db, material, actor.id, case.id)
    else:
        access = field_access.resolve_field_material_access(
            db, actor, material_id, case.id, require_link=False
        )
        link = db.scalar(
            select(FieldProjectLink).where(
                FieldProjectLink.asset_version_id == access.asset.id,
                FieldProjectLink.research_case_id == case.id,
            )
        )
        if not link:
            link = FieldProjectLink(
                asset_version_id=access.asset.id,
                research_case_id=case.id,
                grant_id=access.grant.id,
                added_by=actor.id,
                active=True,
            )
            db.add(link)
        else:
            link.active = True
    field_access.audit(db, actor.id, material_id, "project_linked", project_id=case.id)
    db.commit()
    return {"id": link.id, "research_case_id": case.id}


@router.post("/{material_id}/comments", dependencies=WRITE)
def comment(material_id: int, payload: CommentInput, db: DbSession, actor: ReaderActor):
    publication = db.get(FieldPublication, material_id)
    if not publication or publication.status != "published":
        raise HTTPException(404, "资料当前未开放讨论")
    if payload.parent_id:
        parent = db.get(FieldComment, payload.parent_id)
        if not parent or parent.material_id != material_id or parent.hidden:
            raise HTTPException(422, "回复对象不可用")
    if not payload.body.strip():
        raise HTTPException(422, "请填写评论")
    row = FieldComment(
        material_id=material_id,
        author_id=actor.id,
        parent_id=payload.parent_id,
        body=payload.body.strip(),
        revision_no=1,
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "revision_no": row.revision_no}


@router.post("/comments/{comment_id}", dependencies=WRITE)
def revise_comment(comment_id: int, payload: CommentRevision, db: DbSession, actor: ReaderActor):
    row = db.scalar(select(FieldComment).where(FieldComment.id == comment_id).with_for_update())
    if not row or (row.author_id != actor.id and actor.role != "admin"):
        raise HTTPException(403, "不能管理这条评论")
    if row.revision_no != payload.base_revision_no:
        raise HTTPException(409, "评论已有新版本")
    if payload.body is not None:
        if row.author_id != actor.id or not payload.body.strip():
            raise HTTPException(403, "只能修改自己的评论正文")
        row.body = payload.body.strip()
    if payload.hidden is not None:
        # Author cannot unhide an administrator's moderation; use a new comment.
        if row.hidden and not payload.hidden and actor.role != "admin":
            raise HTTPException(403, "请联系管理员恢复已隐藏评论")
        row.hidden = payload.hidden
    row.revision_no += 1
    field_access.audit(db, actor.id, row.material_id, "comment_revised", comment_id=row.id, hidden=row.hidden)
    db.commit()
    return {"id": row.id, "revision_no": row.revision_no}


@router.post("/runs/{run_id}/rights-review", dependencies=WRITE)
def review_rights(run_id: int, db: DbSession, actor: ReaderActor):
    run = db.get(CapabilityRun, run_id)
    if not run:
        raise HTTPException(404, "成果不存在")
    _case_permission(db, run.research_case_id, actor, "confirm")
    field_access.enforce_run_access(db, run, actor, "cite", ignore_review_lock=True)
    for review in db.scalars(
        select(FieldRunRightsReview).where(
            FieldRunRightsReview.run_id == run_id, FieldRunRightsReview.reviewed_at.is_(None)
        )
    ):
        review.reviewed_at, review.reviewed_by = datetime.now(UTC), actor.id
        field_access.audit(db, actor.id, None, "rights_reviewed", run_id=run_id, grant_id=review.grant_id)
    db.commit()
    return {"id": run_id, "status": "reviewed"}


@router.get("/{material_id}/image")
def private_image(material_id: int, db: DbSession, actor: ReaderActor, research_case_id: int | None = None):
    access = field_access.resolve_field_material_access(
        db, actor, material_id, research_case_id, "read", require_link=False
    )
    if access.material.content_type not in {"image/jpeg", "image/png"}:
        raise HTTPException(404, "该资料不是图片")
    path = resolve_field_material_path(access.asset.storage_key, expected_sha256=access.asset.sha256)
    return FileResponse(
        path,
        media_type=access.material.content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
