"""Bounded S03 discovery with immutable run observations and append-only decisions."""

import hashlib
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher

from sqlalchemy import select

from app.collectors import get_collector
from app.models import Document, DocumentVersion, Source, SourceChannel, User
from app.models.skill_workflows import FieldMaterialProfile, TrackingItem
from app.models.tracking_schedule import TrackingObservation
from app.services.field_access import resolve_field_material_access
from app.services.field_workflow import extract_units, material_units
from app.services.robots_policy import check_robots
from app.services.source_probe import ProbeError, SafeHttpClient, normalize_http_url


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def article_units(units, article):
    if not article:
        if sum(len(x["text"]) for x in units) > 30_000:
            raise ValueError("比较内容超过 3 万字，请指定条款编号")
        return units
    text = "\n".join(x["text"] for x in units)
    matches = list(re.finditer(r"\bArticle\s+" + re.escape(str(article)) + r"\s*:", text, re.I))
    if not matches:
        raise ValueError(f"未找到 Article {article}，请补充可比较原文")
    start = matches[-1].start()
    end_match = re.search(r"\bArticle\s+\d+", text[matches[-1].end() :], re.I)
    end = matches[-1].end() + end_match.start() if end_match else len(text)
    result, offset = [], 0
    for unit in units:
        length = len(unit["text"])
        a, b = max(0, start - offset), min(length, end - offset)
        if a < b:
            result.append({"locator": unit["locator"], "text": unit["text"][a:b], "start_offset": a})
        offset += length + 1
    return result


def base_snapshot(**values):
    return {
        "title": "待识别材料",
        "source_name": "来源待补",
        "source_url": "",
        "published_at": None,
        "observed_at": datetime.now(UTC).isoformat(),
        "language": "unknown",
        "source_type": "用户指定来源",
        "access_status": "accessible",
        "extraction_status": "metadata_only",
        "units": [],
        "issues": [],
        **values,
    }


def fetch_snapshot(spec, client):
    url = normalize_http_url(spec["url"])
    snapshot = base_snapshot(
        title=spec.get("title") or url,
        source_url=url,
        source_name=spec.get("source_name") or "用户指定来源",
        language=spec.get("language") or "unknown",
        published_at=spec.get("published_at"),
        metadata_basis="用户配置，待复核",
    )
    robots = check_robots(client, url)
    if not robots.allowed:
        snapshot.update(access_status="access_failed", issues=[robots.code])
        return snapshot
    try:
        resource = client.fetch(url)
        snapshot.update(
            observed_at=resource.fetched_at.isoformat(), final_url=resource.final_url, sha256=resource.sha256
        )
        if spec.get("compare_content"):
            # Only explicitly selected documents are eligible; never enable body storage for a source.
            kind = resource.content_type.split(";")[0]
            if resource.body.startswith(b"%PDF-"):
                kind = "application/pdf"
            units = extract_units(resource.body, kind, selected_article=spec.get("article"))
            if not spec.get("article"):
                units = article_units(units, None)
            snapshot.update(
                units=units,
                comparison_scope=f"Article {spec['article']}" if spec.get("article") else "用户选定文件",
                extraction_status="extracted"
                if any(x["text"].strip() for x in units)
                else "extraction_failed",
            )
        else:
            snapshot["extraction_status"] = "metadata_only"
    except ProbeError as exc:
        snapshot.update(access_status="access_failed", issues=[exc.code])
    except (ValueError, OSError) as exc:
        snapshot.update(extraction_status="extraction_failed", issues=[str(exc)])
    return snapshot


def _source_candidates(db, source_id, client):
    source = db.get(Source, source_id)
    if not source:
        return [
            base_snapshot(
                source_name=f"来源 #{source_id}", access_status="source_missing", issues=["source_missing"]
            )
        ]
    snapshots = []
    channels = list(
        db.scalars(
            select(SourceChannel)
            .where(SourceChannel.source_id == source_id, SourceChannel.status.in_(["shadow", "active"]))
            .limit(2)
        )
    )
    for channel in channels:
        snapshot = base_snapshot(
            title=channel.name, source_name=source.name, source_url=channel.entry_url, source_id=source.id
        )
        if source.status != "active" or (
            channel.policy
            and (
                channel.policy.robots_state == "disallowed"
                or channel.policy.terms_state in {"disallowed", "blocked", "denied", "forbidden"}
            )
        ):
            snapshot.update(access_status="access_failed", issues=["来源或条款未允许获取"])
            snapshots.append(snapshot)
            continue
        try:
            robots = check_robots(client, channel.entry_url)
            if not robots.allowed:
                snapshot.update(access_status="access_failed", issues=[robots.code])
                snapshots.append(snapshot)
                continue
            resource = client.fetch(channel.entry_url)
            parsed = get_collector(channel.collector_type).parse(
                resource, channel.collector_config, link_role=channel.link_role
            )
            for candidate in parsed.candidates[:50]:
                snapshots.append(
                    base_snapshot(
                        title=candidate.title or "标题待补",
                        source_name=source.name,
                        source_url=candidate.canonical_url or candidate.discovery_url,
                        source_id=source.id,
                        source_type="平台配置来源",
                        language=candidate.language or "unknown",
                        published_at=candidate.published_at.isoformat() if candidate.published_at else None,
                        observed_at=resource.fetched_at.isoformat(),
                        discovery_method="本轮安全获取与适配器解析",
                    )
                )
            if not parsed.candidates:
                snapshot["issues"].append("no_candidates")
                snapshots.append(snapshot)
        except (ProbeError, ValueError, KeyError) as exc:
            snapshot.update(access_status="access_failed", issues=[getattr(exc, "code", type(exc).__name__)])
            snapshots.append(snapshot)
    # Previously indexed records stay explicitly distinct from this execution's discoveries.
    for document, version in db.execute(
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(Document.source_id == source_id)
        .order_by(DocumentVersion.id.desc())
        .limit(100)
    ):
        snapshots.append(
            base_snapshot(
                title=version.title or document.title,
                source_name=source.name,
                source_url=document.canonical_url or document.discovery_url,
                source_id=source_id,
                source_type="已入库元数据",
                document_version_id=version.id,
                language=getattr(document, "language", None) or "unknown",
                published_at=version.published_at.isoformat() if version.published_at else None,
                discovery_method="已有库内记录，非本轮新抓取",
                record_content_hash=version.content_sha256,
            )
        )
    if not channels and not snapshots:
        snapshots.append(
            base_snapshot(
                source_name=source.name, access_status="source_missing", issues=["没有可用栏目或已入库材料"]
            )
        )
    return snapshots


def execute(db, run, config, client=None):
    client = client or SafeHttpClient()
    if (
        not config.get("keywords")
        or not config.get("topic_name")
        or not (config.get("country_iso3") or config.get("regions"))
    ):
        raise ValueError("请填写国家或区域、主题和至少一个关键词")
    if not any(config.get(key) for key in ("sources", "source_ids", "material_ids")):
        raise ValueError("请选择来源、填写链接或选择公开文件")
    snapshots = []
    for spec in config.get("sources", [])[:8]:
        snapshots.append(fetch_snapshot(spec, client))
    for source_id in config.get("source_ids", [])[:3]:
        snapshots.extend(_source_candidates(db, source_id, client))
    for material_id in config.get("material_ids", [])[:15]:
        material = resolve_field_material_access(
            db, db.get(User, run.requested_by), material_id, run.research_case_id, "read"
        ).material
        profile = db.get(FieldMaterialProfile, material_id)
        context = profile.context if profile else {}
        snapshot = base_snapshot(
            title=material.title,
            source_name=context.get("source_name") or "用户导入文件",
            source_url=context.get("source_url") or "",
            material_id=material_id,
            sha256=material.sha256,
            published_at=context.get("published_at"),
            language=context.get("language") or "unknown",
            source_type="用户导入公开文件",
            original_filename=material.original_filename,
        )
        try:
            units = article_units(material_units(material), context.get("article"))
            snapshot.update(
                units=units,
                extraction_status="extracted"
                if any(x["text"].strip() for x in units)
                else "extraction_failed",
            )
        except (ValueError, OSError) as exc:
            snapshot.update(extraction_status="extraction_failed", issues=[str(exc)])
        snapshots.append(snapshot)
    seen, observed, counts = {}, [], {"new": 0, "updated": 0, "unchanged": 0, "excluded": 0}
    existing_hashes = {
        x.snapshot.get("sha256"): x.id
        for x in db.scalars(
            select(TrackingItem)
            .where(TrackingItem.config_id == run.config_id)
            .order_by(TrackingItem.id.desc())
        )
        if x.snapshot.get("sha256")
    }
    for snapshot in snapshots:
        identity = digest(
            f"file:{snapshot['material_id']}"
            if snapshot.get("material_id")
            else snapshot.get("source_url") or snapshot["source_name"]
        )
        if identity in seen:
            continue
        seen[identity] = True
        haystack = (snapshot["title"] + " " + " ".join(x["text"] for x in snapshot["units"])).casefold()
        reasons = []
        if not any(word.casefold() in haystack for word in config["keywords"]):
            reasons.append("关键词不匹配")
        published = (snapshot.get("published_at") or "")[:10]
        if not published:
            snapshot["issues"].append("publication_date_unknown")
        if published and (
            (config.get("date_from") and published < config["date_from"])
            or (config.get("date_to") and published > config["date_to"])
        ):
            reasons.append("超出时间范围")
        snapshot["match_status"] = "excluded" if reasons else "matched"
        snapshot["match_reasons"] = reasons
        if reasons:
            counts["excluded"] += 1
        if snapshot["extraction_status"] == "extraction_failed":
            snapshot["issues"].append("extraction_failed")
        item = db.scalar(
            select(TrackingItem).where(
                TrackingItem.config_id == run.config_id, TrackingItem.identity == identity
            )
        )
        key = snapshot.get("sha256")
        duplicate_id = existing_hashes.get(key) if key else None
        snapshot["relation"] = (
            "duplicate" if duplicate_id and (not item or duplicate_id != item.id) else "new"
        )
        if snapshot["relation"] == "duplicate":
            snapshot["duplicate_of"] = duplicate_id
            snapshot["issues"].append("duplicate")
        comparable = {
            k: v
            for k, v in snapshot.items()
            if k not in {"observed_at", "relation", "issues", "duplicate_of"}
        }
        fingerprint = digest(str(sorted(comparable.items())))
        if item is None:
            item = TrackingItem(
                config_id=run.config_id,
                identity=identity,
                material_id=snapshot.get("material_id"),
                snapshot={},
            )
            db.add(item)
            db.flush()
            counts["new"] += 1
            observation = "new"
        else:
            observation = "unchanged" if item.snapshot.get("fingerprint") == fingerprint else "updated"
            counts[observation] += 1
        snapshot["fingerprint"] = fingerprint
        item.snapshot = snapshot
        if key and not duplicate_id:
            existing_hashes.setdefault(key, item.id)
        immutable = TrackingObservation(
            run_id=run.id,
            item_id=item.id,
            fingerprint=fingerprint,
            observation=observation,
            snapshot=snapshot,
            observed_at=datetime.now(UTC),
        )
        db.add(immutable)
        db.flush()
        observed.append(
            {
                "item_id": item.id,
                "observation_id": immutable.id,
                "observation": observation,
                "snapshot": snapshot,
            }
        )
    failures = sum(o["snapshot"]["access_status"] in {"access_failed", "source_missing"} for o in observed)
    changes = sum(
        o["observation"] in {"new", "updated"}
        and o["snapshot"]["match_status"] == "matched"
        and o["snapshot"]["access_status"] == "accessible"
        for o in observed
    )
    return {
        "source_failures": failures,
        "meaningful_changes": changes,
        "type": "policy_tracking_workflow",
        "observations": observed,
        "counts": counts,
        "result_status": "candidate",
        "no_updates": changes == 0 and failures == 0,
        "method_note": "历史发布日期与本轮获取时间分列；候选进入追踪列表，人工保存后关联项目。",
    }


def source_positions(units, start, end):
    result, offset = [], 0
    for unit in units:
        length = len(unit["text"])
        if (start < offset + length and end > offset) or (
            start == end and offset <= start <= offset + length
        ):
            base = unit.get("start_offset", 0)
            result.append(
                {
                    "locator": unit["locator"],
                    "start": base + max(0, start - offset),
                    "end": base + min(length, max(0, end - offset)),
                }
            )
        offset += length + 1
    return result


def compare(before, after):
    left, right = before.get("units") or [], after.get("units") or []
    if not left or not right or not any(x["text"].strip() for x in left + right):
        raise ValueError("新旧材料必须都有可比较文本；请补充原文")
    a, b = "\n".join(x["text"] for x in left), "\n".join(x["text"] for x in right)
    if not a.strip() or not b.strip():
        raise ValueError("新旧材料必须都有可比较文本；请补充原文")
    # Whitespace layout differences are ignored, but every returned quote keeps original bytes of text.
    at, bt = list(re.finditer(r"\S+", a)), list(re.finditer(r"\S+", b))

    def span(tokens, start, end, text):
        begin = tokens[start].start() if start < len(tokens) else len(text)
        return begin, tokens[end - 1].end() if end > start else begin

    result = []
    for op, i, j, k, end in SequenceMatcher(
        None, [x.group() for x in at], [x.group() for x in bt], autojunk=False
    ).get_opcodes():
        if op == "equal":
            continue
        x, y = span(at, i, j, a)
        u, v = span(bt, k, end, b)
        result.append(
            {
                "kind": op,
                "before": a[x:y],
                "after": b[u:v],
                "before_range": [x, y],
                "after_range": [u, v],
                "before_positions": source_positions(left, x, y),
                "after_positions": source_positions(right, u, v),
                "before_locators": [z["locator"] for z in source_positions(left, x, y)],
                "after_locators": [z["locator"] for z in source_positions(right, u, v)],
            }
        )
    return result
