"""Country catalog backed by exact latest-snapshot membership and complete dimensions."""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Source,
    SourceChannel,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)

CATEGORIES = ("基础国情", "政治治理", "经济发展", "人口社会", "资源环境", "国际关系")
DIMENSIONS = (
    "country_iso3",
    "partner_iso3",
    "indicator_code",
    "metric_code",
    "commodity_classification",
    "source_dataset_code",
    "commodity_code",
    "trade_flow",
    "partner2_code",
    "customs_code",
    "mot_code",
    "frequency",
)


# Field names verified against the existing parsers, not inferred from display labels.
SOURCE_FIELDS = {
    "world-bank": {
        "value": "value",
        "period": "date",
        "country": "countryiso3code",
        "indicator": "indicator.id",
        "unit": "unit",
        "release": "lastupdated",
    },
    "comtrade": {
        "value": "primaryValue",
        "period": "period",
        "country": "reporterCode",
        "partner": "partnerCode",
        "commodity": "cmdCode",
        "flow": "flowCode",
    },
    "unctad-fdi": {
        "country": "Economy",
        "period": "Year",
        "value": "Millions of US$ at current prices",
        "flow": "Flow",
        "direction": "Direction",
    },
    "oecd": {
        "country": "RECIPIENT",
        "period": "TIME_PERIOD",
        "unit": "UNIT_MEASURE",
        "measure": "MEASURE",
        "donor": "DONOR",
        "price_basis": "PRICE_BASE",
        "value": "observations[key][0]",
    },
    "wits": {
        "country": "REPORTER",
        "partner": "PARTNER",
        "period": "TIME_PERIOD",
        "value": "OBS_VALUE",
        "commodity": "PRODUCTCODE",
        "measure": "OBS_VALUE_MEASURE",
    },
}


def original_source_fields(dataset_key: str, metadata: dict) -> dict:
    if metadata.get("original_fields"):
        return metadata["original_fields"]
    return next((fields for prefix, fields in SOURCE_FIELDS.items() if prefix in dataset_key), {})


SERIES_DIMENSIONS = tuple(field for field in DIMENSIONS if field != "source_dataset_code")


def data_permissions(metadata: dict) -> dict:
    license_name = metadata.get("license") or metadata.get("official_dataset", {}).get("license_name")
    license_url = metadata.get("license_url") or metadata.get("official_dataset", {}).get("license_url")
    public_license = str(license_name).upper() in {
        "CC BY-4.0",
        "CC BY 4.0",
        "CC-BY-4.0",
        "CC0",
        "CREATIVE COMMONS ATTRIBUTION 4.0",
    } and bool(license_url)
    readable = metadata.get("view_right", "allowed") == "allowed" and metadata.get("access_scope") not in {
        "restricted",
        "private",
        "anonymized_restricted",
    }
    known_license = bool(license_name and license_name not in {"待核验", "unknown"} and license_url)
    citation = metadata.get("citation_right")
    download = metadata.get("download_right")
    return {
        "view": {"allowed": readable, "reason": "内部可查看" if readable else "无此数据的访问权"},
        "citation": {
            "allowed": readable
            and known_license
            and (
                citation in {"allowed", "source_attribution_required"} or citation is None and public_license
            ),
            "reason": "引用需注明来源、统计期与版本" if known_license else "引用许可尚未核验",
        },
        "download": {
            "allowed": readable
            and known_license
            and (download == "allowed" or download is None and public_license),
            "reason": "下载需保留署名与许可" if known_license else "下载许可尚未核验",
        },
    }


def indicator_identity(observation: StructuredObservation) -> str:
    return json.dumps(
        [getattr(observation, key) for key in SERIES_DIMENSIONS], ensure_ascii=False, separators=(",", ":")
    )


def country_data_catalog(db: Session, iso3: str, registry: dict) -> dict:
    datasets = []
    comparisons = defaultdict(list)
    owners = db.execute(
        select(StructuredDataset, Source)
        .join(SourceChannel, SourceChannel.id == StructuredDataset.channel_id)
        .join(Source, Source.id == SourceChannel.source_id)
    ).all()
    for dataset, source in owners:
        snapshot = db.scalar(
            select(StructuredSnapshot)
            .where(StructuredSnapshot.dataset_id == dataset.id)
            .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
            .limit(1)
        )
        if snapshot is None:
            continue
        permissions = data_permissions(snapshot.source_metadata)
        if not permissions["view"]["allowed"]:
            continue
        rows = db.execute(
            select(StructuredObservation, StructuredObservationVersion)
            .join(
                StructuredSnapshotObservation,
                StructuredSnapshotObservation.observation_id == StructuredObservation.id,
            )
            .join(
                StructuredObservationVersion,
                StructuredObservationVersion.id == StructuredSnapshotObservation.observation_version_id,
            )
            .where(
                StructuredSnapshotObservation.snapshot_id == snapshot.id,
                StructuredObservation.dataset_id == dataset.id,
                StructuredObservation.country_iso3 == iso3,
            )
            .order_by(StructuredObservation.period.desc(), StructuredObservation.id)
        ).all()
        if not rows:
            continue
        metadata = snapshot.source_metadata
        official = metadata.get("official_dataset", {})
        item = {
            "id": dataset.id,
            "dataset_key": dataset.dataset_key,
            "name": dataset.name,
            "frequency": dataset.frequency,
            "source": {
                "id": source.id,
                "name": source.name,
                "organization": source.organization_name,
                "type": source.source_type,
            },
            "updated_at": snapshot.retrieved_at,
            "source_published_at": metadata.get("source_release_date") or metadata.get("lastupdated"),
            "original_fields": original_source_fields(dataset.dataset_key, metadata),
            "snapshot_id": snapshot.id,
            "provider_version": snapshot.provider_version,
            "license": metadata.get("license") or official.get("license_name") or "待核验",
            "license_url": metadata.get("license_url") or official.get("license_url"),
            "citation_right": "source_attribution_required"
            if permissions["citation"]["allowed"]
            else "review_required",
            "download_right": "allowed" if permissions["download"]["allowed"] else "review_required",
            "permissions": permissions,
            "indicators": [],
        }
        indicators = {}
        for observation, version in rows:
            code = observation.indicator_code or observation.metric_code
            definition = registry.get(code, {})
            key = indicator_identity(observation)
            label = definition.get("label", code)
            if observation.commodity_code:
                commodity = {"260300": "铜矿砂及其精矿", "260500": "钴矿砂及其精矿", "283691": "碳酸锂"}.get(
                    observation.commodity_code, observation.commodity_code
                )
                flow = {"M": "进口", "X": "出口"}.get(observation.trade_flow, "")
                partner = {"WLD": "世界", "CHN": "中国"}.get(
                    observation.partner_iso3, observation.partner_iso3 or ""
                )
                classification = observation.commodity_classification or "分类版本未注明"
                label = f"{commodity} · {flow}{label} · {partner} · {classification}"
            indicator = indicators.setdefault(
                key,
                {
                    "key": key,
                    "indicator_code": observation.indicator_code,
                    "metric_code": observation.metric_code,
                    "label": label,
                    "theme": definition.get("theme", "经济发展"),
                    "definition": definition.get("definition", "定义未登记，具体以来源库为准。"),
                    "definition_url": definition.get("definition_url"),
                    "geography_level": definition.get("geography_level", "country"),
                    "original_fields": next(
                        (
                            fields
                            for prefix, fields in definition.get("original_fields_by_source", {}).items()
                            if prefix in dataset.dataset_key
                        ),
                        original_source_fields(dataset.dataset_key, metadata),
                    ),
                    "source_field": {field: getattr(observation, field) for field in SERIES_DIMENSIONS},
                    "source_dataset_codes": [],
                    "units": [],
                    "period_from": observation.period,
                    "period_to": observation.period,
                    "records": [],
                },
            )
            if observation.source_dataset_code not in indicator["source_dataset_codes"]:
                indicator["source_dataset_codes"].append(observation.source_dataset_code)
            if version.unit and version.unit not in indicator["units"]:
                indicator["units"].append(version.unit)
            indicator["period_from"] = min(indicator["period_from"], observation.period)
            indicator["records"].append(
                {
                    "observation_id": observation.id,
                    "observation_version_id": version.id,
                    "version_no": version.version_no,
                    "period": observation.period,
                    "frequency": observation.frequency,
                    "value": float(version.value) if version.value is not None else None,
                    "value_text": str(version.value) if version.value is not None else None,
                    "unit": version.unit,
                    "currency": version.currency,
                    "price_basis": version.price_basis,
                    "missing_reason": version.missing_reason,
                    "source_status": version.source_status,
                    "source_url": version.source_url,
                    "source_release_date": version.source_release_date,
                    "snapshot_id": snapshot.id,
                    "snapshot_retrieved_at": snapshot.retrieved_at,
                    "quality_flags": version.quality_flags,
                    **{field: getattr(observation, field) for field in DIMENSIONS},
                }
            )
        item["indicators"] = list(indicators.values())
        datasets.append(item)
        for indicator in item["indicators"]:
            registered = registry.get(indicator["indicator_code"] or indicator["metric_code"], {})
            if registered.get("comparison_key"):
                comparisons[registered["comparison_key"]].append(
                    {
                        "dataset_id": dataset.id,
                        "indicator_key": indicator["key"],
                        "source_name": source.name,
                        "definition": indicator["definition"],
                        "units": indicator["units"],
                        "period_from": indicator["period_from"],
                        "period_to": indicator["period_to"],
                        "records": indicator["records"],
                    }
                )
    all_indicators = [i for d in datasets for i in d["indicators"]]
    return {
        "country_iso3": iso3,
        "datasets": datasets,
        "categories": [
            {"name": name, "indicator_count": sum(i["theme"] == name for i in all_indicators)}
            for name in CATEGORIES
        ],
        "source_comparisons": [
            {
                "comparison_key": key,
                "sources": sources,
                "comparable": False,
                "difference_reason": "差异原因尚未明确；请逐项核对定义、统计期和口径。",
            }
            for key, sources in comparisons.items()
            if len(sources) > 1
        ],
        "comparison_policy": {
            "merge_sources": False,
            "average_sources": False,
            "automatic_conversion": False,
            "ranking": False,
        },
        "record_definition": "normalized_observation_not_raw_response",
        "method_note": "记录绑定数据集最新快照的实际成员；规范化观测不是原始 HTTP 响应。",
    }
