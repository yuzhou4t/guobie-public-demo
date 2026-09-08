from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.api.research import CountryQuestionPayload, ask_country_assistant
from app.db.session import get_session_factory
from app.models import AgentRun
from app.services.research_capabilities import prepare_mvp_demo

DEMO_QUESTIONS = (
    "刚果（金）当前已入库的六项宏观指标是什么？",
    "当前有哪些已复核政策材料及其发布时间？",
    "当前已复核事件的不同来源主张有哪些差异？",
)
EXPECTED_DEMO_CAPABILITIES = {
    DEMO_QUESTIONS[0]: "country_snapshot",
    DEMO_QUESTIONS[1]: "policy_timeline",
    DEMO_QUESTIONS[2]: "event_evidence_compare",
}


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    with get_session_factory()() as session:
        payload = prepare_mvp_demo(
            session,
            root / "data" / "research_capability_templates.json",
        )
        agent_run_ids = []
        for question in DEMO_QUESTIONS:
            existing = session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.country_iso3 == "COD",
                    AgentRun.question == question,
                    AgentRun.status == "succeeded",
                )
                .order_by(AgentRun.id.desc())
                .limit(1)
            )
            if existing is None or not _positive_run_ready(existing):
                answer = ask_country_assistant(
                    "COD",
                    CountryQuestionPayload(question=question),
                    session,
                )
                agent_run_ids.append(answer["run_id"])
            else:
                agent_run_ids.append(existing.id)
        payload["agent_run_ids"] = agent_run_ids
    print(json.dumps({"status": "ok", **payload}, ensure_ascii=False))
    return 0


def _positive_run_ready(run: AgentRun) -> bool:
    expected = EXPECTED_DEMO_CAPABILITIES.get(run.question)
    matching_trace = next(
        (
            item
            for item in run.tool_trace or []
            if item.get("capability") == expected and item.get("status") == "succeeded"
        ),
        None,
    )
    citations = (run.artifact or {}).get("citations") or []
    return bool(
        expected and matching_trace and matching_trace.get("evidence_count", 0) >= 2 and len(citations) >= 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
