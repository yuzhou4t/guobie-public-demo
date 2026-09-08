"""Rehydrate selected material references; never trust client or cached source text."""

import json

from sqlalchemy import select

from app.models import (
    Document,
    DocumentEntity,
    DocumentVersion,
    EventEntity,
    EventMention,
    ResearchCase,
    ResearchCaseDocument,
    ResearchEntity,
    ResearchEvent,
    Source,
)
from app.services import project_research as workflow
from app.services.project_candidates import adopted_observations
from app.services.research_materials import country_profiles, digest


def enrich_evidence(db, case_id, items, actor=None):
    case = db.get(ResearchCase, case_id)
    scope = case.scope or {}
    countries = scope.get("country_iso3s") or [scope.get("country_iso3", "COD")]
    decisions = workflow.adoption_decisions(db, case_id)
    result = []
    seen = {(i["object_type"], i["object_id"]) for i in items}
    for oid in workflow.adopted_mention_ids(db, case_id):
        if ("event_mention", oid) not in seen:
            mention = db.get(EventMention, oid)
            event = db.get(ResearchEvent, mention.event_id)
            if event and event.review_status == "reviewed":
                items.append(
                    {
                        "object_type": "event_mention",
                        "object_id": oid,
                        "snapshot": {
                            "locator": {"event_mention_id": oid, "mention_locator": mention.evidence_locator}
                        },
                    }
                )
    observations, locators = adopted_observations(db, case_id, countries)
    observed = {version.id: (obs, version, dataset) for obs, version, dataset in observations}
    for item in items:
        kind, oid = item["object_type"], item["object_id"]
        snap = dict(item["snapshot"])
        snap["note"] = item.get("note", "")
        if snap["note"] == "reviewed CSV import":
            snap["note"] = "导入时已复核（来源记录）"
        version = None
        if kind == "document_version":
            version = db.get(DocumentVersion, oid)
            if not version or not workflow.adopted_document(db, decisions, oid):
                continue
        elif kind == "event_mention":
            mention = db.get(EventMention, oid)
            event = db.get(ResearchEvent, mention.event_id) if mention else None
            if not event or event.review_status != "reviewed":
                continue
            # An explicit country tag must stay inside the project scope.
            country_entity = db.get(ResearchEntity, event.country_entity_id)
            event_country = country_entity.canonical_key if country_entity else None
            if event_country and event_country not in countries:
                continue
            version = db.get(DocumentVersion, mention.document_version_id)
            snap.update(
                event_title=event.title,
                occurred_at=str(event.start_at or ""),
                date_precision=event.date_precision,
                text=mention.mention_summary,
                place=(event.details or {}).get("place"),
            )
            actors = db.scalars(
                select(ResearchEntity.canonical_name)
                .join(EventEntity, EventEntity.entity_id == ResearchEntity.id)
                .where(EventEntity.event_id == event.id, EventEntity.role == "actor")
            )
            snap["actors"] = list(actors)
        elif kind == "structured_observation_version":
            if oid not in observed:
                continue
            obs, value, dataset = observed[oid]
            dimensions = {
                key: getattr(obs, key)
                for key in (
                    "frequency",
                    "metric_code",
                    "partner_iso3",
                    "commodity_code",
                    "trade_flow",
                    "commodity_classification",
                    "partner2_code",
                    "customs_code",
                    "mot_code",
                )
            }
            snap.update(
                locator=locators[oid],
                country_iso3=obs.country_iso3,
                indicator_code=obs.indicator_code or obs.metric_code,
                dataset_id=dataset.id,
                period=obs.period,
                value=str(value.value) if value.value is not None else None,
                missing_reason=value.missing_reason,
                unit=value.unit,
                source_url=value.source_url,
                source_name=dataset.name,
                dimensions=json.dumps(dimensions, ensure_ascii=False, sort_keys=True),
            )
        else:
            continue
        if version:
            doc = db.get(Document, version.document_id)
            linked_countries = set(
                db.scalars(
                    select(ResearchEntity.canonical_key)
                    .join(DocumentEntity, DocumentEntity.entity_id == ResearchEntity.id)
                    .where(
                        DocumentEntity.document_version_id == version.id,
                        ResearchEntity.entity_type == "country",
                    )
                )
            )
            if linked_countries and not linked_countries.intersection(countries):
                continue
            if linked_countries:
                snap["country_iso3"] = ", ".join(sorted(linked_countries.intersection(countries)))
            source = db.get(Source, doc.source_id)
            metadata = version.source_metadata or {}
            tagged = metadata.get("country_iso3")
            if tagged and tagged not in countries:
                continue
            snap.update(
                title=version.title or doc.title,
                source_name=source.name,
                source_url=doc.canonical_url,
                published_at=str(version.published_at or doc.published_at or ""),
                language=version.language,
                topics=metadata.get("topics", []),
            )
            if kind == "document_version":
                snap["text"] = version.abstract or ""
                usage = db.scalar(
                    select(ResearchCaseDocument.usage_type).where(
                        ResearchCaseDocument.research_case_id == case_id,
                        ResearchCaseDocument.document_version_id == version.id,
                    )
                )
                snap["purpose"] = {
                    "support": "支持证据",
                    "background": "背景资料",
                    "refute": "反证",
                    "to_verify": "待核实",
                }.get(usage, "未填写")
        snap["evidence_hash"] = digest(snap)
        result.append({**item, "snapshot": snap, "decision": "accepted"})
    for oid in workflow.adopted_field_material_ids(db, case_id):
        from app.services.field_access import FieldAccessDenied, resolve_field_material_access

        try:
            material = resolve_field_material_access(db, actor, oid, case_id, "cite").material
        except FieldAccessDenied:
            continue
        if material.country_iso3 and material.country_iso3 not in countries:
            continue
        snap = {
            "title": material.title,
            "source_name": "用户提供·未复核",
            "privacy": material.privacy_level,
            "published_at": str(material.captured_on or ""),
            "country_iso3": material.country_iso3,
            "locator": {
                "field_material_id": oid,
                "sha256": material.sha256,
                "privacy": material.privacy_level,
            },
            "text": "",
        }
        snap["evidence_hash"] = digest(snap)
        result.append(
            {"object_type": "field_material", "object_id": oid, "decision": "accepted", "snapshot": snap}
        )
    profiles = country_profiles()
    for entity in db.scalars(select(ResearchEntity).where(ResearchEntity.entity_type == "country")):
        iso3 = (entity.details or {}).get("iso3") or entity.canonical_key.removeprefix("country:")
        if iso3 not in countries or iso3 not in profiles["countries"]:
            continue
        profile = profiles["countries"][iso3]
        facts = {
            label: profile.get(key)
            for key, label in (
                ("capital", "首都"),
                ("currencies", "货币"),
                ("official_languages", "官方语言"),
                ("land_area_km2", "国土面积（平方千米）"),
            )
        }
        snap = {
            "title": f"{entity.canonical_name}基础国情",
            "country_iso3": iso3,
            "facts": facts,
            "source_name": profiles["source"]["name"],
            "source_url": profiles["source"]["snapshot_url"],
            "locator": {
                "country_iso3": iso3,
                "revision": profiles["source"]["revision"],
                "profile_hash": digest(profile),
            },
            "license": profiles["source"]["license"],
            "field_overrides": profiles.get("field_overrides", {}).get(iso3, {}),
        }
        snap["evidence_hash"] = digest(snap)
        result.append(
            {
                "object_type": "country_profile",
                "object_id": entity.id,
                "decision": "accepted",
                "snapshot": snap,
                "basis": "项目国家的已登记基础档案；勾选后引用",
            }
        )
    return result


def evidence_fingerprint(snapshot):
    # Editorial notes and display formatting are not changes to the cited evidence.
    keys = (
        "title",
        "text",
        "locator",
        "source_url",
        "published_at",
        "language",
        "country_iso3",
        "facts",
        "field_overrides",
        "privacy",
        "value",
        "unit",
        "period",
        "dimensions",
        "license",
        "occurred_at",
        "event_title",
        "date_precision",
        "actors",
        "place",
        "missing_reason",
    )
    return digest({key: snapshot.get(key) for key in keys})
