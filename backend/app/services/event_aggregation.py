from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

AGGREGATION_VERSION = "rules-v1"
WEIGHTS = {
    "entities": 0.30,
    "location": 0.25,
    "time": 0.20,
    "action": 0.15,
    "title": 0.10,
}
TIME_WINDOWS_DAYS = {
    "conflict": 3,
    "accident": 3,
    "policy": 14,
    "market": 14,
    "other": 7,
}


@dataclass(frozen=True)
class EventCandidateInput:
    country_key: str
    event_type: str
    title: str
    start_at: datetime | None
    entity_keys: frozenset[str]
    location_key: str | None = None
    action: str | None = None


@dataclass(frozen=True)
class EventMatchScore:
    is_candidate: bool
    score: float
    review_band: str
    reasons: dict[str, float | int | str | bool]
    aggregation_version: str = AGGREGATION_VERSION


def normalize_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _text_similarity(left: str | None, right: str | None) -> float:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _location_similarity(left: str | None, right: str | None) -> float:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    similarity = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    return similarity if similarity >= 0.8 else 0.0


def _compatible_types(left: str, right: str) -> bool:
    return left == right or "other" in {left, right}


def _time_window_days(left: str, right: str) -> int:
    return max(TIME_WINDOWS_DAYS.get(left, 7), TIME_WINDOWS_DAYS.get(right, 7))


def score_event_candidate(
    left: EventCandidateInput,
    right: EventCandidateInput,
) -> EventMatchScore:
    country_match = normalize_text(left.country_key) == normalize_text(right.country_key)
    types_compatible = _compatible_types(left.event_type, right.event_type)
    shared_entities = {normalize_text(value) for value in left.entity_keys if normalize_text(value)} & {
        normalize_text(value) for value in right.entity_keys if normalize_text(value)
    }
    window_days = _time_window_days(left.event_type, right.event_type)

    if left.start_at is None or right.start_at is None:
        day_distance = None
        within_window = False
    else:
        day_distance = abs((left.start_at.date() - right.start_at.date()).days)
        within_window = day_distance <= window_days

    gated = country_match and types_compatible and within_window and bool(shared_entities)
    if not gated:
        return EventMatchScore(
            is_candidate=False,
            score=0.0,
            review_band="separate",
            reasons={
                "country_match": country_match,
                "types_compatible": types_compatible,
                "shared_entity_count": len(shared_entities),
                "day_distance": day_distance if day_distance is not None else "unknown",
                "time_window_days": window_days,
                "within_window": within_window,
            },
        )

    left_entities = frozenset(normalize_text(value) for value in left.entity_keys if normalize_text(value))
    right_entities = frozenset(normalize_text(value) for value in right.entity_keys if normalize_text(value))
    entity_score = _jaccard(left_entities, right_entities)
    location_score = _location_similarity(left.location_key, right.location_key)
    time_score = max(0.0, 1.0 - (day_distance or 0) / window_days)
    type_score = 1.0 if left.event_type == right.event_type else 0.5
    action_similarity = _text_similarity(left.action, right.action)
    action_score = 0.7 * type_score + 0.3 * action_similarity
    title_score = _text_similarity(left.title, right.title)
    components = {
        "entities": entity_score,
        "location": location_score,
        "time": time_score,
        "action": action_score,
        "title": title_score,
    }
    score = round(sum(components[key] * WEIGHTS[key] for key in WEIGHTS), 4)
    if score >= 0.80:
        review_band = "high_priority"
    elif score >= 0.60:
        review_band = "ambiguous"
    else:
        review_band = "separate"
    return EventMatchScore(
        is_candidate=True,
        score=score,
        review_band=review_band,
        reasons={
            **{f"{key}_score": round(value, 4) for key, value in components.items()},
            "shared_entity_count": len(shared_entities),
            "day_distance": day_distance or 0,
            "time_window_days": window_days,
            "source_authority_used": False,
        },
    )
