"""Project candidates reuse the country catalog's exact snapshot and rights decisions."""

from datetime import datetime

from fastapi.encoders import jsonable_encoder


def structured_candidates(catalog: dict, terms: list[str], start: datetime, end: datetime):
    count = 0
    for dataset in catalog["datasets"]:
        if not dataset["permissions"]["citation"]["allowed"]:
            continue
        for indicator in dataset["indicators"]:
            searchable = " ".join(
                str(value or "")
                for value in (
                    dataset["name"],
                    dataset["dataset_key"],
                    indicator["label"],
                    indicator["indicator_code"],
                    indicator["metric_code"],
                    indicator["definition"],
                )
            ).casefold()
            if not any(term.casefold() in searchable for term in terms):
                continue
            for record in indicator["records"]:
                period = record["period"]
                if record["frequency"] == "annual":
                    included = start.year <= period <= end.year
                elif record["frequency"] == "monthly":
                    included = start.year * 100 + start.month <= period <= end.year * 100 + end.month
                else:
                    continue
                if not included:
                    continue
                locator = {
                    key: record[key]
                    for key in ("snapshot_id", "observation_id", "observation_version_id", "version_no")
                }
                locator.update(dataset_id=dataset["id"], series_key=indicator["key"])
                snapshot = {
                    **record,
                    "value": record["value_text"],
                    "title": f"{indicator['label']} · {period}",
                    "definition": indicator["definition"],
                    "source_name": dataset["source"]["name"],
                    "dataset_key": dataset["dataset_key"],
                    "license": dataset["license"],
                    "license_url": dataset["license_url"],
                    "permissions": dataset["permissions"],
                    "locator": locator,
                    "source_space": "country",
                    "country_url": f"#/countries/{catalog['country_iso3']}/data",
                    "boundary": "完整年度或月份统计期；缺失值及口径按固定快照保留，不推断趋势。",
                }
                yield record["observation_version_id"], jsonable_encoder(snapshot)
                count += 1
                if count >= 150:
                    return


def adopted_observations(db, case_id: int, countries: list[str] | None):
    """Read the adopted membership, not the observation's mutable latest version."""
    from sqlalchemy import select

    from app.models import (
        ProjectEvidence,
        StructuredDataset,
        StructuredObservation,
        StructuredObservationVersion,
        StructuredSnapshot,
        StructuredSnapshotObservation,
    )
    from app.services.country_data_catalog import data_permissions
    from app.services.project_research import adoption_decisions, is_adopted

    decisions = adoption_decisions(db, case_id)
    evidence = list(
        db.scalars(
            select(ProjectEvidence).where(
                ProjectEvidence.research_case_id == case_id,
                ProjectEvidence.object_type == "structured_observation_version",
            )
        )
    )
    rows, locators = [], {}
    for candidate in evidence:
        if not is_adopted(decisions, candidate.object_type, candidate.object_id):
            continue
        snapshot_id = candidate.snapshot.get("locator", {}).get("snapshot_id")
        if not snapshot_id:
            continue
        record = db.execute(
            select(StructuredObservation, StructuredObservationVersion, StructuredDataset, StructuredSnapshot)
            .join(
                StructuredObservationVersion,
                StructuredObservationVersion.observation_id == StructuredObservation.id,
            )
            .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
            .join(
                StructuredSnapshotObservation,
                (StructuredSnapshotObservation.observation_id == StructuredObservation.id)
                & (StructuredSnapshotObservation.observation_version_id == StructuredObservationVersion.id),
            )
            .join(
                StructuredSnapshot,
                (StructuredSnapshot.id == StructuredSnapshotObservation.snapshot_id)
                & (StructuredSnapshot.dataset_id == StructuredDataset.id),
            )
            .where(
                StructuredObservationVersion.id == candidate.object_id,
                StructuredSnapshot.id == snapshot_id,
                StructuredObservation.country_iso3.in_(countries) if countries is not None else True,
            )
        ).one_or_none()
        if not record or not data_permissions(record[3].source_metadata)["citation"]["allowed"]:
            continue
        observation, version, dataset, snapshot = record
        rows.append((observation, version, dataset))
        locators[version.id] = {
            "snapshot_id": snapshot.id,
            "observation_id": observation.id,
            "observation_version_id": version.id,
            "version_no": version.version_no,
            "dataset_id": dataset.id,
        }
    return rows, locators
