from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.collectors.manual_wits_tariff import (
    WITS_MANUAL_SUPPLEMENT_SCHEDULES,
    ManualWitsTariffError,
    merge_manual_wits_tariff_panel,
    parse_manual_wits_tariff_zip,
)
from app.collectors.wits_tariff import (
    WITS_AVAILABILITY_URL,
    WITS_COUNTRIES,
    WITS_PRODUCTS,
    WITS_YEARS,
    WitsTariffCell,
    WitsTariffPanel,
    WitsTariffShardResult,
    build_wits_tariff_url,
)
from app.models import (
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
)
from app.services.wits_tariff_store import (
    WITS_TARIFF_DATASET_KEY,
    WitsTariffQueryEvidence,
)


def build_manual_wits_import(
    session: Session,
    *,
    zip_paths: tuple[Path, ...],
    file_sha256: dict[Path, str],
    imported_at: datetime,
) -> tuple[WitsTariffPanel, tuple[WitsTariffQueryEvidence, ...]]:
    shards: dict[tuple[str, int], WitsTariffShardResult] = {}
    path_by_schedule: dict[tuple[str, int], Path] = {}
    for path in zip_paths:
        shard = parse_manual_wits_tariff_zip(path)
        key = (shard.source_reporter_code, shard.period)
        if key in shards:
            raise ManualWitsTariffError(
                "manual_wits_schedule_duplicate",
                f"multiple ZIP files contain WITS schedule {key[0]}-{key[1]}",
            )
        shards[key] = shard
        path_by_schedule[key] = path
    if set(shards) != WITS_MANUAL_SUPPLEMENT_SCHEDULES:
        raise ManualWitsTariffError(
            "manual_wits_schedule_set",
            "input directory does not contain the exact eight reviewed WITS schedules",
        )

    snapshot = _latest_snapshot(session)
    base_panel = _panel_from_snapshot(session, snapshot)
    panel = merge_manual_wits_tariff_panel(base_panel, shards)
    evidence = _merged_evidence(
        snapshot,
        path_by_schedule=path_by_schedule,
        file_sha256=file_sha256,
        imported_at=imported_at,
    )
    return panel, evidence


def _latest_snapshot(session: Session) -> StructuredSnapshot:
    dataset = session.scalar(
        select(StructuredDataset).where(StructuredDataset.dataset_key == WITS_TARIFF_DATASET_KEY)
    )
    if dataset is None:
        raise ManualWitsTariffError(
            "manual_wits_base_missing",
            "WITS dataset must have an existing reviewed snapshot before manual supplementation",
        )
    snapshot = session.scalar(
        select(StructuredSnapshot)
        .where(StructuredSnapshot.dataset_id == dataset.id)
        .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
        .limit(1)
    )
    if snapshot is None:
        raise ManualWitsTariffError(
            "manual_wits_base_missing",
            "WITS dataset has no existing snapshot",
        )
    return snapshot


def _panel_from_snapshot(session: Session, snapshot: StructuredSnapshot) -> WitsTariffPanel:
    rows = session.execute(
        select(StructuredObservation, StructuredObservationVersion)
        .join(
            StructuredSnapshotObservation,
            StructuredSnapshotObservation.observation_id == StructuredObservation.id,
        )
        .join(
            StructuredObservationVersion,
            and_(
                StructuredObservationVersion.id == StructuredSnapshotObservation.observation_version_id,
                StructuredObservationVersion.observation_id == StructuredObservation.id,
            ),
        )
        .where(StructuredSnapshotObservation.snapshot_id == snapshot.id)
    ).all()
    if len(rows) != 84:
        raise ManualWitsTariffError(
            "manual_wits_base_incomplete",
            "latest WITS snapshot does not have 84 exact members",
        )

    by_key = {
        (observation.country_iso3, observation.period, observation.commodity_code): (
            observation,
            version,
        )
        for observation, version in rows
    }
    cells: list[WitsTariffCell] = []
    for country_iso3, reporter, source_iso3 in WITS_COUNTRIES:
        for period in WITS_YEARS:
            for product in WITS_PRODUCTS:
                stored = by_key.get((country_iso3, period, product))
                if stored is None:
                    raise ManualWitsTariffError(
                        "manual_wits_base_incomplete",
                        "latest WITS snapshot is missing a fixed-scope member",
                    )
                observation, version = stored
                flags = version.quality_flags
                if flags.get("source_reporter_code") != reporter or flags.get("source_iso3") != source_iso3:
                    raise ManualWitsTariffError(
                        "manual_wits_base_identity",
                        "latest WITS snapshot has inconsistent source identities",
                    )
                cells.append(
                    WitsTariffCell(
                        country_iso3=country_iso3,
                        source_reporter_code=reporter,
                        source_iso3=source_iso3,
                        period=period,
                        commodity_code=product,
                        commodity_classification=observation.commodity_classification,
                        value=_normalized_decimal_or_none(version.value),
                        missing_reason=version.missing_reason,
                        source_url=version.source_url,
                        total_lines=_int_or_none(flags.get("total_lines")),
                        preferential_lines=_int_or_none(flags.get("preferential_lines")),
                        mfn_lines=_int_or_none(flags.get("mfn_lines")),
                        non_ad_valorem_lines=_int_or_none(flags.get("non_ad_valorem_lines")),
                        sum_of_rates=_decimal_or_none(flags.get("sum_of_rates")),
                        min_rate=_decimal_or_none(flags.get("min_rate")),
                        max_rate=_decimal_or_none(flags.get("max_rate")),
                    )
                )
    return WitsTariffPanel(
        cells=tuple(cells),
        available_schedule_count=int(snapshot.source_metadata.get("available_schedule_count", 0)),
        candidate_cell_count=int(snapshot.source_metadata.get("candidate_cell_count", 0)),
        returned_count=snapshot.returned_count,
        valued_count=snapshot.valued_count,
        source_null_count=snapshot.source_null_count,
        not_returned_count=snapshot.not_returned_count,
    )


def _merged_evidence(
    snapshot: StructuredSnapshot,
    *,
    path_by_schedule: dict[tuple[str, int], Path],
    file_sha256: dict[Path, str],
    imported_at: datetime,
) -> tuple[WitsTariffQueryEvidence, ...]:
    manual_urls = {
        build_wits_tariff_url(reporter, period) for reporter, period in WITS_MANUAL_SUPPLEMENT_SCHEDULES
    }
    evidence_by_url: dict[str, WitsTariffQueryEvidence] = {}
    queries = snapshot.query_manifest.get("queries")
    if not isinstance(queries, list):
        raise ManualWitsTariffError(
            "manual_wits_base_evidence",
            "latest WITS snapshot has no readable query evidence",
        )
    for query in queries:
        if not isinstance(query, dict) or query.get("request_url") in manual_urls:
            continue
        try:
            evidence = WitsTariffQueryEvidence(
                kind=str(query["kind"]),
                request_url=str(query["request_url"]),
                resource_sha256=str(query["resource_sha256"]),
                fetched_at=datetime.fromisoformat(str(query["fetched_at"])),
                source_file_name=(
                    str(query["source_file_name"]) if query.get("source_file_name") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManualWitsTariffError(
                "manual_wits_base_evidence",
                "latest WITS snapshot query evidence is malformed",
            ) from exc
        evidence_by_url[evidence.request_url] = evidence

    for schedule, path in path_by_schedule.items():
        url = build_wits_tariff_url(*schedule)
        evidence_by_url[url] = WitsTariffQueryEvidence(
            kind="manual_tariff_zip",
            request_url=url,
            resource_sha256=file_sha256[path],
            fetched_at=imported_at,
            source_file_name=path.name,
        )
    return tuple(
        evidence_by_url[url]
        for url in sorted(
            evidence_by_url,
            key=lambda item: (item != WITS_AVAILABILITY_URL, item),
        )
    )


def _decimal_or_none(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _normalized_decimal_or_none(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value)).normalize()


def _int_or_none(value: object) -> int | None:
    return None if value is None else int(value)
