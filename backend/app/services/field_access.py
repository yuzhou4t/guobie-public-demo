"""One authority for private originals, project uses, model inputs and derived outputs."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import get_settings
from app.models import CapabilityRun, FieldMaterial
from app.models.field_community import (
    FieldAssetVersion,
    FieldCommunityEvent,
    FieldProjectGrant,
    FieldProjectLink,
    FieldRunBinding,
    FieldRunRightsReview,
)
from app.services.community_auth import utc
from app.services.reader_access import ROLE_PERMISSIONS, ReaderAccessDenied, require_case_permission

GRANT_ACTIONS = frozenset({"read", "ai", "cite", "download"})


class FieldAccessDenied(ReaderAccessDenied):
    pass


@dataclass(frozen=True)
class FieldAccess:
    material: FieldMaterial
    asset: FieldAssetVersion
    grant: FieldProjectGrant | None


def ensure_asset(db, material):
    db.scalar(select(FieldMaterial).where(FieldMaterial.id == material.id).with_for_update())
    asset = db.scalar(
        select(FieldAssetVersion)
        .where(FieldAssetVersion.material_id == material.id)
        .order_by(FieldAssetVersion.version_no.desc())
        .limit(1)
    )
    if asset is None:
        asset = FieldAssetVersion(
            material_id=material.id, version_no=1, sha256=material.sha256, storage_key=material.storage_key
        )
        db.add(asset)
        db.flush()
    return asset


def grant_alive(grant):
    return bool(
        grant
        and grant.status == "approved"
        and (not grant.expires_at or utc(grant.expires_at) > datetime.now(UTC))
    )


def audit(db, actor_id, material_id, action, **details):
    db.add(FieldCommunityEvent(actor_id=actor_id, material_id=material_id, action=action, details=details))


def owner_project_link(db, material, actor_id, case_id, *, permissions=None, ai_provider=None):
    """Explicit project upload/link by the uploader; never used by a read request."""
    if material.owner_id != actor_id:
        raise FieldAccessDenied("只有上传者可直接授权自己的资料")
    asset = ensure_asset(db, material)
    grant = db.scalar(
        select(FieldProjectGrant).where(
            FieldProjectGrant.asset_version_id == asset.id, FieldProjectGrant.research_case_id == case_id
        )
    )
    if grant is None:
        grant = FieldProjectGrant(
            asset_version_id=asset.id,
            research_case_id=case_id,
            requested_by=actor_id,
            purpose="上传者选定资料用于本项目",
            permissions=list(permissions or GRANT_ACTIONS),
            ai_provider=ai_provider or get_settings().agent_runtime,
            status="approved",
            decided_by=actor_id,
            decided_at=datetime.now(UTC),
        )
        db.add(grant)
        db.flush()
    elif not grant_alive(grant):
        raise FieldAccessDenied("该项目授权已经撤销或过期，请先重新审批授权")
    link = db.scalar(
        select(FieldProjectLink).where(
            FieldProjectLink.asset_version_id == asset.id, FieldProjectLink.research_case_id == case_id
        )
    )
    if link is None:
        link = FieldProjectLink(
            asset_version_id=asset.id,
            research_case_id=case_id,
            grant_id=grant.id,
            added_by=actor_id,
            active=True,
        )
        db.add(link)
    else:
        link.active = True
    db.flush()
    return link


def resolve_field_material_access(
    db, actor, material_id, project_id=None, action="read", *, asset_version_id=None, require_link=True
):
    material = db.get(FieldMaterial, material_id)
    if not material or not actor or actor.status != "active":
        raise FieldAccessDenied("资料不存在或当前账号无权访问")
    query = select(FieldAssetVersion).where(FieldAssetVersion.material_id == material_id)
    if asset_version_id is not None:
        query = query.where(FieldAssetVersion.id == asset_version_id)
    asset = db.scalar(query.order_by(FieldAssetVersion.version_no.desc()).limit(1))
    if not asset or asset.sha256 != material.sha256 or asset.storage_key != material.storage_key:
        raise FieldAccessDenied("资料版本不可用，请重新核对授权")
    if project_id is None:
        if actor.id != material.owner_id:
            raise FieldAccessDenied("请指定已获授权的项目")
        return FieldAccess(material, asset, None)
    required_role = {"read": "view", "ai": "run_skill", "export": "download"}.get(action, action)
    try:
        member = require_case_permission(db, project_id, actor, required_role)
    except ReaderAccessDenied as exc:
        raise FieldAccessDenied("当前成员没有该项目的资料使用权限") from exc
    if (
        material.privacy_level == "restricted"
        and actor.id != material.owner_id
        and "view_restricted" not in ROLE_PERMISSIONS[member.role]
    ):
        raise FieldAccessDenied("当前成员不能查看受限材料")
    grant = db.scalar(
        select(FieldProjectGrant)
        .where(
            FieldProjectGrant.asset_version_id == asset.id, FieldProjectGrant.research_case_id == project_id
        )
        .execution_options(populate_existing=True)
    )
    permission = {
        "view": "read",
        "revise": "read",
        "confirm": "read",
        "run_skill": "ai",
        "export": "cite",
    }.get(action, action)
    if not grant_alive(grant) or permission not in grant.permissions:
        raise FieldAccessDenied("该项目未获此用途授权，或授权已撤销／过期")
    if permission == "ai" and grant.ai_provider != get_settings().agent_runtime:
        raise FieldAccessDenied("当前 AI 处理服务不在资料授权范围内，请重新申请")
    if require_link:
        link = db.scalar(
            select(FieldProjectLink).where(
                FieldProjectLink.grant_id == grant.id,
                FieldProjectLink.asset_version_id == asset.id,
                FieldProjectLink.research_case_id == project_id,
                FieldProjectLink.active.is_(True),
            )
        )
        if not link:
            raise FieldAccessDenied("请先将已授权资料加入当前项目")
    return FieldAccess(material, asset, grant)


def project_material_ids(db, case_id, *, action="read"):
    pairs = db.execute(
        select(FieldAssetVersion.material_id, FieldProjectGrant)
        .join(FieldProjectLink, FieldProjectLink.asset_version_id == FieldAssetVersion.id)
        .join(FieldProjectGrant, FieldProjectGrant.id == FieldProjectLink.grant_id)
        .where(
            FieldProjectLink.research_case_id == case_id,
            FieldProjectLink.active.is_(True),
            FieldProjectGrant.research_case_id == case_id,
        )
    )
    return {material_id for material_id, grant in pairs if grant_alive(grant) and action in grant.permissions}


def run_material_ids(db, run):
    ids = set()
    config = (run.input_snapshot or {}).get("config") or {}
    for value in config.get("material_ids", []):
        if isinstance(value, int):
            ids.add(value)

    def walk(value):
        if isinstance(value, dict):
            if value.get("object_type") == "field_material" and isinstance(value.get("object_id"), int):
                ids.add(value["object_id"])
            for key, child in value.items():
                if key in {"material_id", "field_material_id", "upload_id"} and isinstance(child, int):
                    ids.add(child)
                elif key in {"material_ids", "field_material_ids"} and isinstance(child, list):
                    ids.update(x for x in child if isinstance(x, int))
                elif (
                    key in {"source_ref", "source_id", "evidence_id"}
                    and isinstance(child, str)
                    and child.startswith("field-material:")
                ):
                    raw_id = child.split(":", 1)[1]
                    if raw_id.isdigit():
                        ids.add(int(raw_id))
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(config)
    walk(run.output or {})
    ids.update(
        db.scalars(
            select(FieldAssetVersion.material_id)
            .join(FieldRunBinding, FieldRunBinding.asset_version_id == FieldAssetVersion.id)
            .where(FieldRunBinding.run_id == run.id)
        )
    )
    return ids


def enforce_run_access(db, run, actor, action="read", *, ignore_review_lock=False):
    if not ignore_review_lock and db.scalar(
        select(FieldRunRightsReview.id)
        .where(FieldRunRightsReview.run_id == run.id, FieldRunRightsReview.reviewed_at.is_(None))
        .limit(1)
    ):
        raise FieldAccessDenied("资料授权已变，成果锁定待复核")
    for material_id in run_material_ids(db, run):
        resolve_field_material_access(db, actor, material_id, run.research_case_id, action)


def bind_run(db, run, access):
    prior = db.scalar(
        select(FieldRunBinding).where(
            FieldRunBinding.run_id == run.id, FieldRunBinding.asset_version_id == access.asset.id
        )
    )
    if not prior:
        db.add(
            FieldRunBinding(
                run_id=run.id,
                asset_version_id=access.asset.id,
                grant_id=access.grant.id,
                snapshot={
                    "material_id": access.material.id,
                    "sha256": access.asset.sha256,
                    "permissions": access.grant.permissions,
                    "ai_provider": access.grant.ai_provider,
                    "model": get_settings().agent_model,
                    "purpose": "s05_analysis",
                },
            )
        )
        db.flush()


def revoke_grant(db, grant, actor):
    asset = db.get(FieldAssetVersion, grant.asset_version_id)
    material = db.get(FieldMaterial, asset.material_id)
    if material.owner_id != actor.id:
        raise FieldAccessDenied("只有上传者可以撤销资料使用授权")
    grant.status = "revoked"
    grant.decided_by, grant.decided_at = actor.id, datetime.now(UTC)
    lock_grant_runs(db, grant)
    audit(db, actor.id, material.id, "grant_revoked", grant_id=grant.id, project_id=grant.research_case_id)
    db.flush()


def lock_grant_runs(db, grant):
    """Renewal also requires explicit review of previously derived artifacts."""
    asset = db.get(FieldAssetVersion, grant.asset_version_id)
    material = db.get(FieldMaterial, asset.material_id)
    for run in db.scalars(
        select(CapabilityRun).where(CapabilityRun.research_case_id == grant.research_case_id)
    ):
        if material.id not in run_material_ids(db, run):
            continue
        review = db.scalar(
            select(FieldRunRightsReview).where(
                FieldRunRightsReview.run_id == run.id, FieldRunRightsReview.grant_id == grant.id
            )
        )
        if review is None:
            db.add(FieldRunRightsReview(run_id=run.id, grant_id=grant.id))
        else:
            review.locked_at = datetime.now(UTC)
            review.reviewed_at, review.reviewed_by = None, None
    db.flush()
