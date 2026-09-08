"""Read registered paper-to-event evidence links without inferring topic relations."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EventMention, ResearchEntity, ResearchEvent


def frontier_relations(db: Session, papers: list[dict], country_iso3: str | None) -> dict:
    version_ids = {p["document_version_id"] for p in papers}
    statement = (
        select(EventMention, ResearchEvent, ResearchEntity)
        .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
        .join(ResearchEntity, ResearchEntity.id == ResearchEvent.country_entity_id)
        .where(
            EventMention.document_version_id.in_(version_ids),
            EventMention.review_status == "confirmed",
            ResearchEvent.review_status == "reviewed",
        )
        .order_by(ResearchEvent.start_at.desc(), ResearchEvent.id, EventMention.id)
    )
    if country_iso3:
        statement = statement.where(ResearchEntity.canonical_key == country_iso3)
    related = {"policies": [], "events": [], "data": []}
    by_event = {}
    for mention, event, country in db.execute(statement):
        if event.id not in by_event:
            entry = {
                "id": event.id,
                "title": event.title,
                "country_iso3": country.canonical_key,
                "url": f"#/countries/{country.canonical_key}/events/{event.id}/evidence",
                "date": event.start_at,
                "date_precision": event.date_precision,
                "basis": "该主题论文的已确认文献提及连接到此已复核事件",
                "evidence_links": [],
            }
            by_event[event.id] = entry
            related["policies" if event.event_type == "policy" else "events"].append(entry)
        by_event[event.id]["evidence_links"].append(
            {
                "event_mention_id": mention.id,
                "document_version_id": mention.document_version_id,
                "locator": mention.evidence_locator,
            }
        )
    return {
        "related": related,
        "relation_note": "只展示已确认文献提及与已复核事件的登记关系；不通过关键词推断关联。",
        "relation_states": {
            "policies": "registered" if related["policies"] else "no_registered_link",
            "events": "registered" if related["events"] else "no_registered_link",
            "data": "no_relation_registry",
        },
        "data_relation_note": "尚未登记论文与结构化数据的直接关系；同国数据不等于该论文的引用数据。",
    }
