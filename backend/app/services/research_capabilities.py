from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    AgentRun,
    CapabilityAuditEvent,
    CapabilityConfig,
    CapabilityConfigRevision,
    CapabilityRun,
    CapabilityTemplate,
    Document,
    DocumentEntity,
    DocumentVersion,
    EventMention,
    EvidenceClaim,
    FieldMaterial,
    ResearchCase,
    ResearchCaseDocument,
    ResearchCaseEvent,
    ResearchEntity,
    ResearchEvent,
    Source,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    User,
)
from app.services import project_research as project_workflow
from app.services.agent_runtime import AgentRequest, AgentRuntimeError, EvidenceItem, FactItem, ToolResult
from app.services.country_agent import RegisteredAgentTool, run_country_agent
from app.services.vertical_skills import (
    bilingual_policy_verification,
    contradictory_evidence_context,
    country_brief,
    country_comparison,
    event_timeline,
    field_material_organizer,
    interdisciplinary_evidence,
    material_condition_assessment,
    material_relevance_ranking,
    policy_dynamics,
    policy_impact,
    topic_feasibility,
)


class ResearchCapabilityError(ValueError):
    pass


PUBLIC_CAPABILITY_CATALOG_PATH = Path(__file__).resolve().parents[3] / "data" / "capability_catalog.json"


def _ensure_seeded_config_revision(
    session: Session,
    config: CapabilityConfig,
    template: CapabilityTemplate,
    actor_id: int,
) -> None:
    latest_revision = session.scalar(
        select(CapabilityConfigRevision)
        .where(CapabilityConfigRevision.config_id == config.id)
        .order_by(CapabilityConfigRevision.revision_no.desc())
        .limit(1)
    )
    template_snapshot = {
        "template_id": template.id,
        "catalog_key": template.catalog_key,
        "slug": template.slug,
        "version": template.version,
        "name": template.name,
        "execution_plan": template.execution_plan,
    }
    if (
        latest_revision is not None
        and latest_revision.name == config.name
        and latest_revision.config_snapshot == dict(config.config or {})
        and latest_revision.template_snapshot == template_snapshot
    ):
        return
    revision = CapabilityConfigRevision(
        config_id=config.id,
        revision_no=(latest_revision.revision_no + 1 if latest_revision is not None else 1),
        name=config.name,
        config_snapshot=dict(config.config or {}),
        template_snapshot=template_snapshot,
        created_by=actor_id,
    )
    session.add(revision)
    session.flush()
    session.add(
        CapabilityAuditEvent(
            actor_id=actor_id,
            action="configured" if latest_revision is not None else "created",
            target_type="capability_config",
            target_id=config.id,
            details={"source": "mvp_seed", "revision_id": revision.id},
        )
    )


def seed_public_capability_catalog(
    session: Session, catalog_path: Path = PUBLIC_CAPABILITY_CATALOG_PATH
) -> int:
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    items = payload.get("items")
    expected_codes = [f"S{index:02d}" for index in range(1, 12)]
    if not isinstance(items, list) or [item.get("skill_code") for item in items] != expected_codes:
        raise ResearchCapabilityError("public capability catalog must follow document S01-S11")
    # Withdraw superseded catalog entries without deleting configurations or run snapshots.
    legacy_keys = payload.get("legacy_catalog_keys", [])
    for legacy in session.scalars(
        select(CapabilityTemplate).where(CapabilityTemplate.catalog_key.in_(legacy_keys))
    ):
        legacy.visibility = "internal"
        legacy.status = "retired"
    created = 0
    for item in items:
        template = session.scalar(
            select(CapabilityTemplate).where(CapabilityTemplate.catalog_key == item["catalog_key"])
        )
        if template is None:
            template = CapabilityTemplate(
                catalog_key=item["catalog_key"],
                slug=f"catalog-{item['catalog_key']}",
                version="1.0.0",
                name=item["name"],
            )
            session.add(template)
            created += 1
        template.name = item["name"]
        template.version = item.get("version", template.version)
        template.description = item["description"]
        template.input_schema = item["input_schema"]
        template.output_schema = item["output_schema"]
        template.default_config = {
            "requirement_contract": item["contract"],
            "skill": {
                "skill_code": item["skill_code"],
                "catalog_key": item["catalog_key"],
                "source_channel": payload["source_document"],
                "supported_scopes": item["scopes"],
                "human_checkpoints": ["确认研究范围与材料", "候选结果人工复核", "写回前确认"],
                "evidence_policy": "保留来源、时间、版本和处理记录；结果仅供研究者核查与取舍。",
                "writeback_targets": ["research_case"],
                "cost_risk": {"cost": "运行前估算", "risk": item["contract"]["boundary"]},
            },
        }
        template.category = item["category"]
        template.visibility = "public"
        template.validation_status = item["validation_status"]
        template.execution_plan = {
            "mode": "server_allowlist",
            "executors": item["executors"],
            "acceptance": item.get(
                "acceptance",
                {
                    "result": item["validation_status"],
                    "checks": ["完整需求输入输出", "来源与处理记录", "人工修改确认", "运行门禁"],
                },
            ),
        }
        template.status = "active"
    session.flush()
    return created


def _missing_any_input_groups(
    template: CapabilityTemplate,
    config: dict[str, Any],
) -> list[list[str]]:
    missing = []
    for field_group in template.input_schema.get("required_any_of") or []:
        if not any(
            config.get(field_name) is not None
            and config.get(field_name) != ""
            and config.get(field_name) != []
            for field_name in field_group
        ):
            missing.append(field_group)
    return missing


_APPROVED_INTERNAL_EXECUTORS = {
    "country-brief",
    "bilingual-policy-verification",
    "policy-dynamics",
    "event-timeline",
    "field-material-organizer",
    "interdisciplinary-evidence",
    "material-relevance-ranking",
    "contradictory-evidence-context",
    "country-comparison",
    "policy-impact",
    "material-condition-assessment",
    "policy-impact-graph",
    "event-evidence-matrix",
    "topic-digest",
}


def _execution_slug(template: CapabilityTemplate) -> str:
    if template.visibility != "public":
        return template.slug
    return next(
        (
            item
            for item in (template.execution_plan or {}).get("executors") or []
            if item in _APPROVED_INTERNAL_EXECUTORS
        ),
        "",
    )


def seed_capability_templates(
    session: Session,
    template_path: Path,
    *,
    owner_email: str | None = None,
) -> dict[str, int]:
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    templates = payload.get("templates")
    if not isinstance(templates, list):
        raise ResearchCapabilityError("capability template file must contain a templates list")
    owner = None
    if owner_email:
        owner = session.scalar(select(User).where(User.email == owner_email, User.status == "active"))
        if owner is None:
            raise ResearchCapabilityError("owner_email must identify an active existing user")
    created_templates = 0
    created_configs = 0
    for item in templates:
        runtime_config = {
            key: value
            for key, value in item["default_config"].items()
            if key not in {"skill", "catalog_visibility"}
        }
        template = session.scalar(
            select(CapabilityTemplate).where(
                CapabilityTemplate.slug == item["slug"],
                CapabilityTemplate.version == item["version"],
            )
        )
        if template is None:
            template = CapabilityTemplate(
                slug=item["slug"],
                version=item["version"],
                name=item["name"],
                description=item["description"],
                input_schema=item["input_schema"],
                output_schema=item["output_schema"],
                default_config=item["default_config"],
                status=item.get("status", "active"),
                visibility="internal",
                validation_status="verified",
                execution_plan={"mode": "internal_compatibility", "executors": [item["slug"]]},
            )
            session.add(template)
            session.flush()
            created_templates += 1
        else:
            template.name = item["name"]
            template.description = item["description"]
            template.input_schema = item["input_schema"]
            template.output_schema = item["output_schema"]
            template.default_config = item["default_config"]
            template.status = item.get("status", "active")
            template.visibility = "internal"
            template.validation_status = "verified"
            template.execution_plan = {"mode": "internal_compatibility", "executors": [item["slug"]]}
        for old_template in session.scalars(
            select(CapabilityTemplate).where(
                CapabilityTemplate.slug == item["slug"],
                CapabilityTemplate.id != template.id,
                CapabilityTemplate.status != "retired",
            )
        ):
            old_template.status = "retired"
        if owner is not None and template.status == "active":
            config_name = item.get("demo_name", item["name"])
            config = session.scalar(
                select(CapabilityConfig).where(
                    CapabilityConfig.owner_id == owner.id,
                    CapabilityConfig.name == config_name,
                )
            )
            if config is None:
                config = session.scalar(
                    select(CapabilityConfig)
                    .join(CapabilityTemplate, CapabilityTemplate.id == CapabilityConfig.template_id)
                    .where(
                        CapabilityConfig.owner_id == owner.id,
                        CapabilityConfig.status == "active",
                        CapabilityTemplate.slug == item["slug"],
                    )
                    .order_by(CapabilityConfig.id)
                    .limit(1)
                )
            if config is None:
                session.add(
                    CapabilityConfig(
                        template_id=template.id,
                        owner_id=owner.id,
                        name=config_name,
                        config=runtime_config,
                        status="active",
                        scope_type="personal",
                    )
                )
                created_configs += 1
            else:
                config.template_id = template.id
                config.config = {**runtime_config, **config.config}
                config.config.pop("skill", None)
                config.config.pop("catalog_visibility", None)
    seed_public_capability_catalog(session)
    catalog_slugs = {item["slug"] for item in templates}
    for deprecated_slug in {"policy-impact", "topic-feasibility"} - catalog_slugs:
        for old_template in session.scalars(
            select(CapabilityTemplate).where(
                CapabilityTemplate.slug == deprecated_slug,
                CapabilityTemplate.status != "retired",
            )
        ):
            old_template.status = "retired"
            for old_config in session.scalars(
                select(CapabilityConfig).where(CapabilityConfig.template_id == old_template.id)
            ):
                old_config.status = "disabled"
    session.flush()
    if owner is not None:
        for config in session.scalars(select(CapabilityConfig).where(CapabilityConfig.owner_id == owner.id)):
            template = session.get(CapabilityTemplate, config.template_id)
            if template is not None:
                _ensure_seeded_config_revision(session, config, template, owner.id)
    session.commit()
    return {"templates_created": created_templates, "configs_created": created_configs}


def prepare_mvp_demo(session: Session, template_path: Path) -> dict[str, Any]:
    owner_email = "mvp-demo@local.invalid"
    owner = session.scalar(select(User).where(User.email == owner_email))
    if owner is None:
        owner = User(
            email=owner_email,
            display_name="国别智枢 MVP Demo",
            role="user",
            status="active",
        )
        session.add(owner)
        session.flush()
    elif owner.status != "active":
        owner.status = "active"

    country = session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == "COD",
        )
    )
    if country is None:
        country = ResearchEntity(
            entity_type="country",
            canonical_key="COD",
            canonical_name="刚果民主共和国（刚果（金））",
            aliases=["刚果（金）", "Democratic Republic of the Congo"],
            details={"iso3": "COD", "seed_role": "mvp_structure_only"},
        )
        session.add(country)

    case_title = "刚果（金）关键矿产与武装冲突"
    research_case = session.scalar(
        select(ResearchCase).where(
            ResearchCase.owner_id == owner.id,
            ResearchCase.title == case_title,
        )
    )
    if research_case is None:
        research_case = ResearchCase(
            owner_id=owner.id,
            title=case_title,
            research_question="比较已复核政策事件与安全事件的多源证据，并生成可追溯周报。",
            scope={
                "country_iso3": "COD",
                "seed_role": "mvp_structure_only",
                "scope_revision": 1,
                "context_bindings": {},
            },
            status="active",
        )
        session.add(research_case)
    session.commit()
    session.refresh(owner)
    session.refresh(research_case)

    seeded = seed_capability_templates(session, template_path, owner_email=owner_email)
    verified_defaults = {
        "policy-change-tracking": ("personal", {"country_iso3": "COD"}),
        "country-indicator-alignment": ("personal", {"country_iso3s": ["COD", "ZAF"]}),
        "team-weekly-research-deposit": (
            "research_case",
            {"research_case_id": research_case.id},
        ),
    }
    for catalog_key, (scope_type, default_config) in verified_defaults.items():
        template = session.scalar(
            select(CapabilityTemplate).where(CapabilityTemplate.catalog_key == catalog_key)
        )
        if template is None:
            continue
        existing = session.scalar(
            select(CapabilityConfig).where(
                CapabilityConfig.template_id == template.id,
                CapabilityConfig.owner_id == owner.id,
                CapabilityConfig.research_case_id
                == (research_case.id if scope_type == "research_case" else None),
            )
        )
        if existing is None:
            session.add(
                CapabilityConfig(
                    template_id=template.id,
                    owner_id=owner.id,
                    scope_type=scope_type,
                    research_case_id=(research_case.id if scope_type == "research_case" else None),
                    name=f"{template.name}·演示配置",
                    config=default_config,
                    status="active",
                )
            )
    session.flush()
    configs = list(
        session.scalars(
            select(CapabilityConfig)
            .where(CapabilityConfig.owner_id == owner.id, CapabilityConfig.status == "active")
            .order_by(CapabilityConfig.id)
        )
    )
    ctcpm_version_id = session.scalar(
        select(DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.canonical_url
            == "https://datawarehouse.ctcpm.cd/cadre/juridique/legislation/download/45/legislation"
        )
        .order_by(DocumentVersion.version_no.desc(), DocumentVersion.id.desc())
        .limit(1)
    )
    for config in configs:
        template = session.get(CapabilityTemplate, config.template_id)
        allowed_fields = set((template.input_schema if template else {}).get("allowed_fields") or [])
        needs_demo_case = bool(
            template
            and (
                config.scope_type == "research_case"
                or (
                    template.visibility != "public"
                    and template.slug in {"event-evidence-matrix", "topic-digest"}
                )
            )
            and "research_case_id" in allowed_fields
        )
        if needs_demo_case:
            config.config = {**config.config, "research_case_id": research_case.id}
        elif template and template.visibility == "public" and config.scope_type == "personal":
            config.config = {key: value for key, value in config.config.items() if key != "research_case_id"}
        if template and template.slug == "bilingual-policy-verification" and ctcpm_version_id:
            config.config = {
                **config.config,
                "document_version_ids": [ctcpm_version_id],
            }
        if template is not None:
            _ensure_seeded_config_revision(session, config, template, owner.id)
    session.commit()

    run_ids: list[int] = []
    for config in configs:
        template = session.get(CapabilityTemplate, config.template_id)
        case_id = config.research_case_id or (
            research_case.id
            if template and template.visibility != "public" and template.slug != "country-brief"
            else None
        )
        if template is not None and _missing_any_input_groups(template, config.config):
            continue
        existing = session.scalar(
            select(CapabilityRun)
            .where(
                CapabilityRun.config_id == config.id,
                CapabilityRun.status.in_(("succeeded", "insufficient_data", "failed")),
            )
            .order_by(CapabilityRun.id.desc())
            .limit(1)
        )
        if existing is not None:
            existing_snapshot = existing.input_snapshot or {}
            existing_template = existing_snapshot.get("template") or {}
            same_template = existing_template.get("version") == template.version
            same_runtime = existing_snapshot.get("agent_runtime") in {None, get_settings().agent_runtime}
            if existing.status == "failed" and same_template and same_runtime:
                run_ids.append(existing.id)
                continue
            execution_slug = _execution_slug(template)
            current_output = _build_output(
                session,
                execution_slug,
                case_id,
                config.config,
                config_id=config.id,
            )
            current_has_evidence = _output_has_evidence(execution_slug, current_output)
            existing_has_evidence = _output_has_evidence(execution_slug, existing.output or {})
            if existing.status == "succeeded" and not existing_has_evidence:
                existing.status = "insufficient_data"
                existing.error_code = "insufficient_data"
                existing.error_message = "历史运行为空，已按当前证据门槛更正为证据不足。"
                session.commit()
            if same_template and (existing.status == "succeeded" or not current_has_evidence):
                run_ids.append(existing.id)
                continue
        run_ids.append(run_capability(session, config_id=config.id, research_case_id=case_id).id)
    return {
        **seeded,
        "owner_id": owner.id,
        "research_case_id": research_case.id,
        "config_ids": [config.id for config in configs],
        "run_ids": run_ids,
    }


def run_capability(
    session: Session,
    *,
    config_id: int,
    research_case_id: int | None = None,
    input_overrides: dict[str, Any] | None = None,
    requested_by: int | None = None,
    research_progress: bool = False,
    writing_instruction: str = "",
    workflow_started=None,
) -> CapabilityRun:
    config = session.get(CapabilityConfig, config_id)
    if config is None or config.status not in {"active", "published"}:
        raise ResearchCapabilityError("active capability config not found")
    template = session.get(CapabilityTemplate, config.template_id)
    if template is None or template.status != "active":
        raise ResearchCapabilityError("active capability template not found")
    if config.research_case_id is not None:
        if research_case_id is not None and research_case_id != config.research_case_id:
            raise ResearchCapabilityError("capability config belongs to another project")
        research_case_id = config.research_case_id
    research_case = session.get(ResearchCase, research_case_id) if research_case_id else None
    if research_case_id and research_case is None:
        raise ResearchCapabilityError("research case not found")
    overrides = dict(input_overrides or {})
    allowed_fields = set(template.input_schema.get("allowed_fields") or [])
    unknown_fields = set(overrides) - allowed_fields
    if unknown_fields:
        raise ResearchCapabilityError(
            f"unsupported capability input fields: {', '.join(sorted(unknown_fields))}"
        )
    effective_config = {**config.config, **overrides}
    if research_case_id is not None and "research_case_id" in allowed_fields:
        effective_config["research_case_id"] = research_case_id
    missing_groups = _missing_any_input_groups(template, effective_config)
    if missing_groups:
        raise ResearchCapabilityError(f"capability requires one of: {', '.join(missing_groups[0])}")

    execution_slug = _execution_slug(template)
    if not execution_slug:
        raise ResearchCapabilityError("public capability has no approved internal executor")

    if execution_slug == "field-material-organizer" and effective_config.get("workflow_version") != 1:
        raise ResearchCapabilityError("新田野成果请从 S05 工作流选择已授权资料；旧成果仍可阅读")
    started_at = _utc_now()
    topic_bundle = None
    if execution_slug == "topic-digest":
        topic_bundle = _topic_digest_bundle(
            session,
            research_case_id,
            effective_config,
            started_at=started_at,
            research_progress=research_progress,
        )
    if topic_bundle is not None:
        topic_bundle["writing_instruction"] = writing_instruction
    run = CapabilityRun(
        config_id=config.id,
        research_case_id=research_case_id,
        requested_by=requested_by or config.owner_id,
        status="running",
        input_snapshot={
            "template": {
                "catalog_key": template.catalog_key,
                "slug": template.slug,
                "version": template.version,
                "executor": execution_slug,
            },
            "config": effective_config,
            "input_overrides": overrides,
            "writing_instruction": writing_instruction,
            "research_case_updated_at": (research_case.updated_at.isoformat() if research_case else None),
            **(topic_bundle["snapshot"] if topic_bundle else {}),
        },
        output={},
        started_at=started_at,
    )
    session.add(run)
    session.flush()
    if effective_config.get("workflow_version") == 1 and execution_slug in {
        "field-material-organizer",
        "policy-dynamics",
    }:
        from app.models import CapabilityConfigRevision
        from app.services import field_workflow, tracking_workflow

        run.config_revision_id = session.scalar(
            select(CapabilityConfigRevision.id)
            .where(CapabilityConfigRevision.config_id == config.id)
            .order_by(CapabilityConfigRevision.revision_no.desc())
            .limit(1)
        )
        session.commit()  # Refreshing the UI can now recover this exact running request.
        if workflow_started is not None:
            workflow_started(run)
        try:
            with session.begin_nested():
                runner = (
                    field_workflow.execute
                    if execution_slug == "field-material-organizer"
                    else tracking_workflow.execute
                )
                from app.services.skill_execution_control import checkpoint

                checkpoint()
                run.output = runner(session, run, effective_config)
                checkpoint()
                session.flush()
                if execution_slug == "field-material-organizer" or run.output.get("meaningful_changes"):
                    from app.services import workflow_documents

                    workflow_documents.append(
                        session,
                        run,
                        run.requested_by,
                        workflow_documents.initial_document(session, run),
                        "draft",
                        "处理完成后生成成果草稿",
                    )
            run.status = "succeeded"
        except Exception as exc:
            run.status = "failed"
            run.error_code = "workflow_failed"
            run.error_message = (
                str(exc)[:500]
                if isinstance(exc, (ValueError, AgentRuntimeError))
                else "工作流处理失败；请检查材料或运行环境后重试"
            )
        run.finished_at = _utc_now()
        session.commit()
        return run
    if topic_bundle is not None:
        _generate_topic_digest(session, run, research_case, topic_bundle)
        session.commit()
        return run
    try:
        output = _build_output(
            session,
            execution_slug,
            research_case_id,
            effective_config,
            config_id=config.id,
        )
    except ValueError as exc:
        raise ResearchCapabilityError(str(exc)) from exc
    run.output = output
    if output.get("error_code") == "agent_runtime_required":
        run.status = "failed"
        run.error_code = "agent_runtime_required"
        run.error_message = "已冻结证据包，但当前缺少受控模型运行时，未生成伪译文。"
    elif _output_has_evidence(execution_slug, output):
        run.status = "succeeded"
    else:
        run.status = "insufficient_data"
        run.error_code = "insufficient_data"
        run.error_message = "输入快照没有达到该能力的最低证据门槛，未生成正式研究产物。"
    run.finished_at = _utc_now()
    session.commit()
    return run


def _build_output(
    session: Session,
    slug: str,
    research_case_id: int | None,
    config: dict[str, Any],
    *,
    config_id: int | None = None,
) -> dict[str, Any]:
    if research_case_id is not None:
        config = {**config, "research_case_id": research_case_id}
    if slug == "country-brief":
        return country_brief(session, config)
    if slug == "bilingual-policy-verification":
        return bilingual_policy_verification(session, research_case_id, config)
    if slug == "policy-dynamics":
        return policy_dynamics(session, config, config_id=config_id)
    if slug == "event-timeline":
        return event_timeline(session, research_case_id, config)
    if slug == "field-material-organizer":
        return field_material_organizer(session, research_case_id, config)
    if slug == "interdisciplinary-evidence":
        return interdisciplinary_evidence(session, research_case_id, config)
    if slug == "material-relevance-ranking":
        return material_relevance_ranking(session, research_case_id, config)
    if slug == "contradictory-evidence-context":
        return contradictory_evidence_context(session, research_case_id, config)
    if slug == "country-comparison":
        return country_comparison(session, config)
    if slug == "policy-impact":
        return policy_impact(session, research_case_id, config)
    if slug == "topic-feasibility":
        return topic_feasibility(session, research_case_id, config)
    if slug == "material-condition-assessment":
        return material_condition_assessment(session, research_case_id, config)
    if slug == "policy-impact-graph":
        return policy_impact(session, research_case_id, config)
    if slug == "event-evidence-matrix":
        return _event_evidence_matrix(session, research_case_id)
    if slug == "topic-digest":
        return _topic_digest(session, research_case_id, config)
    raise ResearchCapabilityError(f"unsupported capability template: {slug}")


def _country_brief(session: Session, config: dict[str, Any]) -> dict[str, Any]:
    iso3 = str(config.get("country_iso3", "")).upper()
    if len(iso3) != 3:
        raise ResearchCapabilityError("country-brief config requires country_iso3")
    country = session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == iso3,
        )
    )
    event_count = 0
    events: list[dict[str, Any]] = []
    if country is not None:
        event_rows = list(
            session.scalars(
                select(ResearchEvent)
                .where(
                    ResearchEvent.country_entity_id == country.id,
                    ResearchEvent.review_status == "reviewed",
                )
                .order_by(ResearchEvent.start_at.desc(), ResearchEvent.id.desc())
                .limit(20)
            )
        )
        event_count = len(event_rows)
        events = [
            {"id": item.id, "title": item.title, "start_at": _iso(item.start_at)} for item in event_rows
        ]
    observation_count = session.scalar(
        select(func.count())
        .select_from(StructuredObservation)
        .where(StructuredObservation.country_iso3 == iso3)
    )
    return {
        "type": "country_brief",
        "schema_version": "2.0",
        "country_iso3": iso3,
        "event_count": event_count,
        "structured_observation_count": observation_count or 0,
        "events": events,
        "source_chain": [
            {"kind": "structured_observation", "count": observation_count or 0},
            {"kind": "reviewed_event", "count": event_count},
        ],
        "conflicts": [],
        "evidence_gaps": [
            message
            for present, message in (
                (bool(observation_count), "当前没有可引用结构化观测。"),
                (bool(event_count), "当前没有 reviewed 事件。"),
            )
            if not present
        ],
        "method_note": "仅汇总已入库结构化观测和 reviewed 事件；不以缺失值补零。",
    }


def _policy_dynamics(session: Session, config: dict[str, Any]) -> dict[str, Any]:
    iso3 = str(config.get("country_iso3", "")).upper()
    country = session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country", ResearchEntity.canonical_key == iso3
        )
    )
    if country is None:
        return {"type": "policy_dynamics", "country_iso3": iso3, "materials": [], "events": []}
    events = list(
        session.scalars(
            select(ResearchEvent)
            .where(
                ResearchEvent.country_entity_id == country.id,
                ResearchEvent.event_type == "policy",
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(ResearchEvent.start_at.desc(), ResearchEvent.id.desc())
        )
    )
    event_ids = [event.id for event in events]
    rows = []
    if event_ids:
        rows = list(
            session.execute(
                select(EventMention, DocumentVersion, Document, Source)
                .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    EventMention.event_id.in_(event_ids),
                    EventMention.review_status == "confirmed",
                )
                .order_by(Document.published_at.desc(), DocumentVersion.id.desc())
            )
        )
    return {
        "type": "policy_dynamics",
        "country_iso3": iso3,
        "events": [
            {"id": event.id, "title": event.title, "start_at": _iso(event.start_at)} for event in events
        ],
        "materials": [
            {
                "document_version_id": version.id,
                "title": version.title or document.title,
                "source": source.name,
                "source_url": document.canonical_url or document.discovery_url,
                "published_at": _iso(version.published_at or document.published_at),
                "observed_at": _iso(document.last_seen_at),
                "version_no": version.version_no,
                "mention_summary": mention.mention_summary,
            }
            for mention, version, document, source in rows
        ],
        "version_changes": [
            {"document_version_id": version.id, "version_no": version.version_no}
            for _, version, _, _ in rows
            if version.version_no > 1
        ],
        "reading_queue": [version.id for _, version, _, _ in rows],
        "evidence_gaps": [] if rows else ["当前没有 confirmed 政策材料。"],
        "method_note": "没有来源发布时间时，不以采集时间替代本期发布时间。",
    }


def _event_timeline(
    session: Session,
    research_case_id: int | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    event_ids = [int(item) for item in config.get("event_ids") or []]
    statement = select(ResearchEvent).where(ResearchEvent.review_status == "reviewed")
    if event_ids:
        statement = statement.where(ResearchEvent.id.in_(event_ids))
    elif research_case_id:
        statement = statement.join(ResearchCaseEvent, ResearchCaseEvent.event_id == ResearchEvent.id).where(
            ResearchCaseEvent.research_case_id == research_case_id
        )
    else:
        iso3 = str(config.get("country_iso3", "")).upper()
        country_id = session.scalar(
            select(ResearchEntity.id).where(
                ResearchEntity.entity_type == "country", ResearchEntity.canonical_key == iso3
            )
        )
        if country_id is None:
            return {"type": "event_timeline", "items": [], "evidence_gaps": ["国家未入库。"]}
        statement = statement.where(ResearchEvent.country_entity_id == country_id)
    events = list(session.scalars(statement.order_by(ResearchEvent.start_at, ResearchEvent.id).limit(100)))
    items = []
    for event in events:
        mentions = session.scalar(
            select(func.count())
            .select_from(EventMention)
            .where(EventMention.event_id == event.id, EventMention.review_status == "confirmed")
        )
        items.append(
            {
                "event_id": event.id,
                "title": event.title,
                "start_at": _iso(event.start_at),
                "end_at": _iso(event.end_at),
                "date_precision": event.date_precision,
                "summary": event.summary,
                "place": event.details.get("place") if event.details else None,
                "actors": (event.details or {}).get("actors", []),
                "confirmed_source_mentions": mentions or 0,
            }
        )
    return {
        "type": "event_timeline",
        "items": items,
        "evidence_gaps": [
            f"事件 #{item['event_id']} 没有 confirmed 来源。"
            for item in items
            if not item["confirmed_source_mentions"]
        ],
        "method_note": "仅按登记时间和日期精度排序；不推断因果，不覆盖来源冲突。",
    }


def _field_material_organizer(
    session: Session,
    research_case_id: int | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    if research_case_id is None:
        raise ResearchCapabilityError("field-material-organizer requires research_case_id")
    excerpts = []
    requested_ids = [
        int(item["upload_id"])
        for item in config.get("file_refs") or []
        if isinstance(item, dict) and str(item.get("upload_id") or "").isdigit()
    ]
    allowed_materials = project_workflow.adopted_field_material_ids(session, research_case_id)
    materials = {
        item.id: item
        for item in session.scalars(
            select(FieldMaterial).where(
                FieldMaterial.id.in_(set(requested_ids) & allowed_materials),
                FieldMaterial.research_case_id == research_case_id,
            )
        )
    }
    file_refs = [
        {
            "upload_id": item.id,
            "title": item.title,
            "filename": item.original_filename,
            "content_type": item.content_type,
            "privacy": item.privacy_level,
            "evidence_status": item.evidence_status,
            "captured_on": item.captured_on.isoformat() if item.captured_on else None,
            "sha256": item.sha256,
        }
        for upload_id in requested_ids
        if (item := materials.get(upload_id)) is not None
    ]
    for index, item in enumerate(config.get("transcript_items") or [], start=1):
        if isinstance(item, dict):
            source_ref = str(item.get("source_ref") or "")
            if source_ref.startswith("field-material:"):
                material_id = source_ref.removeprefix("field-material:")
                if not material_id.isdigit() or int(material_id) not in allowed_materials:
                    continue
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        excerpts.append(
            {
                "excerpt_id": f"provided-{index}",
                "timestamp": item.get("timestamp"),
                "speaker": item.get("speaker") or "未标注角色",
                "text": str(item["text"]).strip(),
                "method": item.get("method") or "用户提供转写",
                "privacy": item.get("privacy") or config.get("privacy_mode", "restricted"),
                "source_ref": item.get("source_ref"),
            }
        )
    return {
        "type": "field_material_notes",
        "excerpts": excerpts,
        "file_refs": file_refs,
        "evidence_gaps": [
            *([] if excerpts else ["当前只登记了文件引用；文件内容未自动解析，尚无可引用转写摘录。"]),
            *(
                [f"{len(requested_ids) - len(file_refs)} 个上传引用不存在或不可用。"]
                if len(requested_ids) != len(file_refs)
                else []
            ),
        ],
        "method_note": "用户上传材料保持未复核；未接受音频、未执行自动转写、未推断真实身份。",
    }


def _country_comparison(session: Session, config: dict[str, Any]) -> dict[str, Any]:
    countries = [str(item).upper() for item in config.get("country_iso3s") or []]
    if len(countries) < 2:
        raise ResearchCapabilityError("country-comparison requires at least two countries")
    indicator_codes = [str(item) for item in config.get("indicator_codes") or []]
    statement = (
        select(StructuredObservation, StructuredObservationVersion, StructuredDataset)
        .join(
            StructuredObservationVersion,
            (StructuredObservationVersion.observation_id == StructuredObservation.id)
            & (StructuredObservationVersion.version_no == StructuredObservation.latest_version_no),
        )
        .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
        .where(StructuredObservation.country_iso3.in_(countries))
    )
    if indicator_codes:
        statement = statement.where(StructuredObservation.indicator_code.in_(indicator_codes))
    rows = list(session.execute(statement.order_by(StructuredObservation.period)))
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for observation, version, dataset in rows:
        key = (observation.indicator_code, observation.period, version.unit, dataset.dataset_key)
        group = groups.setdefault(
            key,
            {
                "indicator_code": observation.indicator_code,
                "period": observation.period,
                "unit": version.unit,
                "dataset_key": dataset.dataset_key,
                "values": {},
                "source_urls": {},
            },
        )
        group["values"][observation.country_iso3] = (
            float(version.value) if version.value is not None else None
        )
        group["source_urls"][observation.country_iso3] = version.source_url
    comparable = [item for item in groups.values() if set(item["values"]) == set(countries)]
    return {
        "type": "country_comparison",
        "countries": countries,
        "comparable_rows": comparable,
        "not_comparable": [
            {
                "indicator_code": item["indicator_code"],
                "period": item["period"],
                "reason": "国家、时期、单位或来源未全部对齐",
            }
            for item in groups.values()
            if set(item["values"]) != set(countries)
        ],
        "method_note": "缺失值保持为空；只把同指标、同周期、同单位、同数据集的观测列为可比。",
    }


def _event_evidence_matrix(session: Session, research_case_id: int | None) -> dict[str, Any]:
    if research_case_id is None:
        raise ResearchCapabilityError("event-evidence-matrix requires research_case_id")
    allowed = project_workflow.adopted_mention_ids(session, research_case_id)
    events = list(
        session.scalars(
            select(ResearchEvent)
            .join(ResearchCaseEvent, ResearchCaseEvent.event_id == ResearchEvent.id)
            .where(
                ResearchCaseEvent.research_case_id == research_case_id,
                ResearchEvent.review_status == "reviewed",
                ResearchEvent.id.in_(select(EventMention.event_id).where(EventMention.id.in_(allowed))),
            )
            .order_by(ResearchEvent.start_at, ResearchEvent.id)
        )
    )
    items = []
    for event in events:
        mentions = session.scalar(
            select(func.count())
            .select_from(EventMention)
            .where(
                EventMention.event_id == event.id,
                EventMention.review_status == "confirmed",
                EventMention.id.in_(allowed),
            )
        )
        claims = session.scalar(
            select(func.count())
            .select_from(EvidenceClaim)
            .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
            .where(
                EventMention.event_id == event.id,
                EventMention.review_status == "confirmed",
                EventMention.id.in_(allowed),
                EvidenceClaim.review_status == "confirmed",
            )
        )
        comparisons = session.scalar(
            select(func.count(func.distinct(EvidenceClaim.comparison_key)))
            .select_from(EvidenceClaim)
            .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
            .where(
                EventMention.event_id == event.id,
                EventMention.review_status == "confirmed",
                EventMention.id.in_(allowed),
                EvidenceClaim.review_status == "confirmed",
            )
        )
        sources = session.scalar(
            select(func.count(func.distinct(Document.source_id)))
            .select_from(EventMention)
            .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                EventMention.event_id == event.id,
                EventMention.review_status == "confirmed",
                EventMention.id.in_(allowed),
            )
        )
        items.append(
            {
                "event_id": event.id,
                "title": event.title,
                "source_mentions": mentions or 0,
                "distinct_sources": sources or 0,
                "claims": claims or 0,
                "comparison_groups": comparisons or 0,
            }
        )
    return {
        "type": "event_evidence_matrix",
        "events": items,
        "method_note": "比较组只并列来源主张，不取平均、不判定真伪。",
    }


def _topic_digest(session: Session, research_case_id: int | None, config: dict[str, Any]) -> dict[str, Any]:
    return _topic_digest_bundle(session, research_case_id, config, started_at=_utc_now())["output"]


def _topic_digest_bundle(
    session: Session,
    research_case_id: int | None,
    config: dict[str, Any],
    *,
    started_at: datetime,
    research_progress: bool = False,
) -> dict[str, Any]:
    if research_case_id is None:
        raise ResearchCapabilityError("topic-digest requires research_case_id")
    research_case = session.get(ResearchCase, research_case_id)
    if research_case is None:
        raise ResearchCapabilityError("research case not found")
    if research_case.status != "active":
        raise ResearchCapabilityError("topic-digest requires an active research case")
    frequency = config.get("frequency", "weekly")
    if frequency not in {"daily", "weekly", "on_demand"}:
        raise ResearchCapabilityError("topic-digest frequency must be daily, weekly, or on_demand")
    period_start, period_end = _digest_period(config, frequency, started_at)
    rows = list(
        session.execute(
            select(ResearchCaseDocument, DocumentVersion, Document, Source)
            .join(ResearchCaseDocument, ResearchCaseDocument.document_version_id == DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(ResearchCaseDocument.research_case_id == research_case_id)
            .where(
                or_(
                    select(DocumentEntity.document_version_id)
                    .where(
                        DocumentEntity.document_version_id == DocumentVersion.id,
                        DocumentEntity.review_status == "confirmed",
                    )
                    .exists(),
                    select(EventMention.document_version_id)
                    .where(
                        EventMention.document_version_id == DocumentVersion.id,
                        EventMention.review_status == "confirmed",
                    )
                    .exists(),
                )
            )
        )
    )
    decisions = project_workflow.adoption_decisions(session, research_case_id)
    rows = [row for row in rows if project_workflow.adopted_document(session, decisions, row[1].id)]
    grouped_materials: dict[int, list[tuple[Any, DocumentVersion, Document, Source]]] = defaultdict(list)
    for row in rows:
        grouped_materials[row[2].id].append(row)
    selected_materials = [
        max(group, key=lambda row: (row[1].version_no, row[1].id)) for group in grouped_materials.values()
    ]
    selected_materials.sort(
        key=lambda row: (_iso(row[1].published_at or row[2].published_at) or "", row[1].id),
        reverse=True,
    )

    events = list(
        session.scalars(
            select(ResearchEvent)
            .join(ResearchCaseEvent, ResearchCaseEvent.event_id == ResearchEvent.id)
            .where(
                ResearchCaseEvent.research_case_id == research_case_id,
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(ResearchEvent.start_at.is_(None), ResearchEvent.start_at, ResearchEvent.id)
        )
    )
    event_ids = [event.id for event in events]
    mention_rows = []
    if event_ids:
        mention_rows = list(
            session.execute(
                select(EventMention, ResearchEvent, DocumentVersion, Document, Source)
                .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
                .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    EventMention.event_id.in_(event_ids),
                    EventMention.review_status == "confirmed",
                )
                .order_by(
                    EventMention.source_reported_start_at.is_(None),
                    EventMention.source_reported_start_at,
                    DocumentVersion.published_at.is_(None),
                    DocumentVersion.published_at,
                    Source.name,
                )
            )
        )
    mention_rows = [
        row
        for row in mention_rows
        if project_workflow.is_adopted(decisions, "event_mention", row[0].id)
        and project_workflow.adopted_document(session, decisions, row[2].id)
    ]
    supported_events = {row[1].id for row in mention_rows}
    events = [event for event in events if event.id in supported_events]
    mention_by_id = {mention.id: row for row in mention_rows for mention in (row[0],)}
    mentions_by_event: dict[int, list[tuple[Any, ResearchEvent, DocumentVersion, Document, Source]]] = (
        defaultdict(list)
    )
    for row in mention_rows:
        mentions_by_event[row[1].id].append(row)
    claims = []
    if mention_by_id:
        claims = list(
            session.scalars(
                select(EvidenceClaim)
                .where(
                    EvidenceClaim.event_mention_id.in_(mention_by_id),
                    EvidenceClaim.review_status == "confirmed",
                )
                .order_by(EvidenceClaim.comparison_key, EvidenceClaim.id)
            )
        )
    claims_by_comparison: dict[str, list[EvidenceClaim]] = defaultdict(list)
    for claim in claims:
        claims_by_comparison[claim.comparison_key].append(claim)

    evidence: dict[str, EvidenceItem] = {}
    facts: list[FactItem] = []
    materials = []
    unknown_material_dates = 0
    for _, version, document, source in selected_materials:
        published_at = version.published_at or document.published_at
        evidence_id = f"document-version:{version.id}"
        evidence[evidence_id] = EvidenceItem(
            evidence_id=evidence_id,
            kind="document",
            title=version.title,
            source_name=source.name,
            source_url=document.canonical_url or document.discovery_url,
            published_at=_iso(published_at),
            observed_at=_iso(document.last_seen_at),
            published_at_precision=_published_precision(version),
            evidence_locator={"document_id": document.id, "document_version_id": version.id},
            payload={"document_id": document.id, "document_version_id": version.id},
        )
        in_period = bool(published_at and period_start <= _as_utc(published_at) <= period_end)
        if published_at is None:
            unknown_material_dates += 1
        material = {
            "document_id": document.id,
            "document_version_id": version.id,
            "linked_version_ids": sorted({row[1].id for row in grouped_materials[document.id]}),
            "title": version.title,
            "source": source.name,
            "published_at": _iso(published_at),
            "published_at_precision": _published_precision(version),
            "source_url": document.canonical_url or document.discovery_url,
            "evidence_id": evidence_id,
            "new_in_period": in_period,
        }
        materials.append(material)
        if in_period or research_progress:
            facts.append(
                FactItem(
                    fact_id=f"fact:material:{version.id}",
                    text=(
                        f"{'当前项目已采用材料' if research_progress else '本期新增材料'}"
                        f"《{version.title}》，来源 {source.name}，"
                        f"来源发布时间为 {_display_date(published_at, _published_precision(version))}。"
                    ),
                    evidence_ids=[evidence_id],
                )
            )

    event_timeline = []
    events_without_evidence = 0
    for event in events:
        event_mentions = mentions_by_event[event.id]
        event_evidence_ids = []
        for mention, _, version, document, source in event_mentions:
            evidence_id = f"event-mention:{mention.id}"
            event_evidence_ids.append(evidence_id)
            evidence[evidence_id] = EvidenceItem(
                evidence_id=evidence_id,
                kind="event_mention",
                title=event.title,
                source_name=source.name,
                source_url=document.canonical_url or document.discovery_url,
                published_at=_iso(version.published_at or document.published_at),
                observed_at=_iso(document.last_seen_at),
                published_at_precision=_published_precision(version),
                evidence_locator=mention.evidence_locator,
                payload={
                    "event_id": event.id,
                    "document_version_id": version.id,
                    "mention_summary": mention.mention_summary,
                },
            )
        if event_evidence_ids:
            facts.append(
                FactItem(
                    fact_id=f"fact:event:{event.id}",
                    text=(
                        f"{_display_event_date(event.start_at, event.date_precision)}，"
                        f"专题关联事件《{event.title}》：{event.summary}"
                    ),
                    evidence_ids=event_evidence_ids,
                )
            )
        else:
            events_without_evidence += 1
        event_timeline.append(
            {
                "event_id": event.id,
                "title": event.title,
                "start_at": _iso(event.start_at),
                "date_precision": event.date_precision,
                "summary": event.summary,
                "evidence_ids": event_evidence_ids,
            }
        )

    claim_differences = []
    for comparison_key, group in claims_by_comparison.items():
        distinct_values = {(claim.value_text, str(claim.numeric_value), claim.unit) for claim in group}
        if len(distinct_values) <= 1:
            continue
        group_items = []
        for claim in group:
            mention, event, _, _, source = mention_by_id[claim.event_mention_id]
            evidence_id = f"event-mention:{mention.id}"
            fact_id = f"fact:claim:{claim.id}"
            facts.append(
                FactItem(
                    fact_id=fact_id,
                    text=(f"在《{event.title}》中，来源 {source.name} 的已确认主张为：{claim.value_text}。"),
                    evidence_ids=[evidence_id],
                )
            )
            group_items.append(
                {
                    "claim_id": claim.id,
                    "fact_id": fact_id,
                    "evidence_id": evidence_id,
                    "source": source.name,
                    "value_text": claim.value_text,
                    "numeric_value": float(claim.numeric_value) if claim.numeric_value is not None else None,
                    "unit": claim.unit,
                    "position_summary": claim.position_summary,
                }
            )
        claim_differences.append(
            {"comparison_key": comparison_key, "has_difference": True, "claims": group_items}
        )

    gaps = []
    if not any(item["new_in_period"] for item in materials):
        gaps.append("报告周期内没有来源明确声明发布时间的新增材料。")
    if unknown_material_dates:
        gaps.append(f"{unknown_material_dates} 份关联材料缺少来源发布时间，未计入本期新增。")
    if events_without_evidence:
        gaps.append(f"{events_without_evidence} 个关联事件没有 confirmed 来源提及，未形成事实段落。")
    if not claim_differences:
        gaps.append("当前冻结事实包没有可并列展示的数字或立场差异组。")
    evidence_items = list(evidence.values())
    output = {
        "type": "topic_digest",
        "schema_version": "2.0",
        "frequency": frequency,
        "research_case": {
            "id": research_case.id,
            "title": research_case.title,
            "research_question": research_case.research_question,
        },
        "period": {"start": _iso(period_start), "end": _iso(period_end)},
        "materials": materials,
        "new_materials": [item for item in materials if item["new_in_period"]],
        "event_timeline": event_timeline,
        "claim_differences": claim_differences,
        "evidence_gaps": gaps,
        "method_limitations": [
            "仅使用运行开始时冻结的 reviewed 事件、confirmed 材料与 confirmed 主张。",
            "来源差异并列展示；系统不取平均、不判断真伪或权威性。",
            "来源未声明发布时间时，不以采集时间替代本期发布时间。",
        ],
        "ai_digest": None,
        "generation_status": "pending",
        "generated_from_stored_evidence_only": True,
    }
    snapshot = {
        "report_basis": "research_progress" if research_progress else "publication_updates",
        "report_period": output["period"],
        "material_version_ids": [item["document_version_id"] for item in materials],
        "linked_material_version_ids": sorted(
            {version_id for item in materials for version_id in item["linked_version_ids"]}
        ),
        "event_ids": [item["event_id"] for item in event_timeline],
        "evidence_ids": [item.evidence_id for item in evidence_items],
        "fact_ids": [item.fact_id for item in facts],
        "agent_runtime": get_settings().agent_runtime,
        "agent_model": get_settings().agent_model or None,
        "digest_template_version": config.get("template_version", "topic-digest-1.1"),
    }
    return {
        "research_progress": research_progress,
        "snapshot": snapshot,
        "output": output,
        "tool": RegisteredAgentTool(
            name="topic_digest_preview",
            description="读取专题冻结事实包并组织可追溯的本期周报。",
            result=ToolResult(
                capability="topic_digest_preview",
                status="succeeded" if facts else "insufficient_data",
                summary=f"冻结 {len(facts)} 条事实与 {len(evidence_items)} 条证据。",
                evidence=evidence_items,
                facts=facts,
                calculations=[],
                limitations=gaps,
            ),
        ),
    }


def _generate_topic_digest(
    session: Session,
    run: CapabilityRun,
    research_case: ResearchCase | None,
    bundle: dict[str, Any],
) -> None:
    settings = get_settings()
    output = bundle["output"]
    tool: RegisteredAgentTool = bundle["tool"]
    agent_run = AgentRun(
        country_iso3=str((research_case.scope if research_case else {}).get("country_iso3", "COD")),
        conversation_id=str(uuid.uuid4()),
        scope_type="topic",
        scope_key=str(research_case.id if research_case else run.research_case_id),
        scope_context={
            "research_case_id": run.research_case_id,
            "capability_run_id": run.id,
            "report_period": output["period"],
        },
        question=(
            (
                f"用户本轮写作要求：{bundle['writing_instruction']}\n"
                if bundle.get("writing_instruction")
                else ""
            )
            + f"生成专题《{research_case.title if research_case else ''}》本期周报。"
            + (
                "这是研究进展周报，使用当前项目已采用的全部材料。"
                "按研究目标整理可支持的认识和后续核验建议；"
                "区分文献发表日期与本周研究工作，不把历史文献写成本周新事件。"
                "只有书目信息时仅说明题名、来源、日期及可继续研究的问题，不推断论文结论。"
                if bundle.get("research_progress")
                else ""
            )
        ),
        workflow="evidence_synthesis" if settings.agent_runtime == "evidence_only" else "bounded_agent",
        runtime=settings.agent_runtime,
        model_name=settings.agent_model or None,
        status="running",
        plan={},
        tool_trace=[],
        evidence=[],
        artifact={},
        started_at=run.started_at,
    )
    session.add(agent_run)
    session.flush()
    output["agent_run_id"] = agent_run.id
    if not tool.result.facts:
        run.output = {**output, "generation_status": "insufficient_data"}
        run.status = "insufficient_data"
        run.error_code = "insufficient_data"
        run.error_message = "冻结事实包没有可用于生成周报的 reviewed 事实。"
        agent_run.status = "failed"
        agent_run.error_code = "insufficient_data"
        agent_run.error_message = run.error_message
        agent_run.finished_at = _utc_now()
        run.finished_at = agent_run.finished_at
        return
    if settings.agent_runtime == "evidence_only":
        run.output = {**output, "generation_status": "failed"}
        run.status = "failed"
        run.error_code = "agent_runtime_required"
        run.error_message = "当前未启用生成模型，未生成 AI 专题周报。"
        agent_run.status = "failed"
        agent_run.error_code = run.error_code
        agent_run.error_message = run.error_message
        agent_run.finished_at = _utc_now()
        run.finished_at = agent_run.finished_at
        return
    try:
        result = run_country_agent(
            settings=settings,
            request=AgentRequest(
                country_iso3=agent_run.country_iso3,
                country_name="刚果民主共和国（刚果（金））",
                question=agent_run.question,
                output_type="topic_weekly_digest",
                scope_type="topic",
                scope_key=agent_run.scope_key,
                scope_context=agent_run.scope_context,
            ),
            tools=[tool],
            fallback_related_questions=["本期还缺哪些来源发布时间？", "哪些来源主张存在差异？"],
        )
    except AgentRuntimeError as exc:
        run.output = {**output, "generation_status": "failed"}
        run.status = "failed"
        run.error_code = "agent_generation_failed"
        run.error_message = str(exc)
        agent_run.status = "failed"
        agent_run.error_code = run.error_code
        agent_run.error_message = run.error_message
        agent_run.finished_at = _utc_now()
        run.finished_at = agent_run.finished_at
        return
    artifact = result.artifact
    artifact.limitations = list(dict.fromkeys(artifact.limitations))
    run.output = {
        **output,
        "generation_status": "succeeded",
        "ai_digest": artifact.model_dump(mode="json"),
    }
    run.status = "succeeded"
    agent_run.workflow = result.plan.workflow
    agent_run.plan = result.plan.model_dump(mode="json")
    agent_run.tool_trace = artifact.tool_trace
    agent_run.evidence = [item.model_dump(mode="json") for item in artifact.citations]
    agent_run.artifact = artifact.model_dump(mode="json")
    agent_run.status = "succeeded"
    agent_run.finished_at = _utc_now()
    run.finished_at = agent_run.finished_at


def _digest_period(
    config: dict[str, Any],
    frequency: str,
    started_at: datetime,
) -> tuple[datetime, datetime]:
    end = _parse_datetime(config.get("date_to")) or _as_utc(started_at)
    start = _parse_datetime(config.get("date_from"))
    if start is None:
        start = end - timedelta(days=1 if frequency == "daily" else 7)
    if start > end:
        raise ResearchCapabilityError("topic-digest date_from must not be after date_to")
    return start, end


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchCapabilityError("topic-digest dates must be ISO 8601") from exc
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _published_precision(version: DocumentVersion) -> str:
    provenance = (version.source_metadata or {}).get("published_at_provenance")
    if isinstance(provenance, dict) and provenance.get("precision") in {
        "day",
        "month",
        "year",
        "unknown",
    }:
        return provenance["precision"]
    return "unknown"


def _display_date(value: datetime | None, precision: str) -> str:
    if value is None or precision == "unknown":
        return "来源未声明"
    if precision == "year":
        return f"{value.year} 年"
    if precision == "month":
        return value.strftime("%Y-%m")
    return value.strftime("%Y-%m-%d")


def _display_event_date(value: datetime | None, precision: str) -> str:
    if value is None or precision == "unknown":
        return "日期未知"
    return _display_date(value, precision)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _output_has_evidence(slug: str, output: dict[str, Any]) -> bool:
    if slug == "country-brief":
        return bool(output.get("structured_observation_count") or output.get("event_count"))
    if slug == "bilingual-policy-verification":
        return bool(output.get("segments"))
    if slug == "policy-dynamics":
        return bool(output.get("materials"))
    if slug == "event-timeline":
        return any(item.get("confirmed_source_mentions") for item in output.get("items") or [])
    if slug == "field-material-organizer":
        return bool(output.get("excerpts") or output.get("file_refs"))
    if slug == "interdisciplinary-evidence":
        return any(
            any(group.values()) for group in (item.get("evidence") for item in output.get("matrix") or [])
        )
    if slug == "material-relevance-ranking":
        return bool(output.get("ranked_materials"))
    if slug == "contradictory-evidence-context":
        return len(output.get("context_cards") or []) >= 2
    if slug == "country-comparison":
        return bool(output.get("comparable_rows"))
    if slug == "policy-impact":
        return bool(output.get("policy_events") and output.get("edges"))
    if slug == "topic-feasibility":
        summary = output.get("evidence_summary") or {}
        return bool(output.get("research_question") and any(summary.values()))
    if slug == "material-condition-assessment":
        summary = output.get("evidence_summary") or {}
        return bool(output.get("research_question") and any(summary.values()))
    if slug == "policy-impact-graph":
        return bool(output.get("policy_events") and output.get("edges"))
    if slug == "event-evidence-matrix":
        return any(item.get("distinct_sources", 0) for item in output.get("events") or [])
    if slug == "topic-digest":
        return bool(output.get("materials"))
    return False
