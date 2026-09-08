from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.services.agent_runtime import (
    AgentRequest,
    AgentRuntimeError,
    AnswerArtifact,
    AnswerClaim,
    AnswerDraft,
    AnswerSection,
    CapabilitySpec,
    CodexLocalRuntime,
    CozeTestRuntime,
    EvidenceItem,
    ExecutionPlan,
    FactItem,
    OpenAIResponsesRuntime,
    StructuredAgentRuntime,
    ToolResult,
)


@dataclass(frozen=True)
class RegisteredAgentTool:
    name: str
    description: str
    result: ToolResult


@dataclass(frozen=True)
class CountryAgentResult:
    plan: ExecutionPlan
    artifact: AnswerArtifact


AgentProgressCallback = Callable[[str, dict[str, Any]], None]


def run_country_agent(
    *,
    settings: Settings,
    request: AgentRequest,
    tools: list[RegisteredAgentTool],
    fallback_related_questions: list[str],
    progress: AgentProgressCallback | None = None,
) -> CountryAgentResult:
    if not tools:
        raise AgentRuntimeError("country agent requires at least one registered capability")
    registry = {tool.name: tool for tool in tools}
    runtime = _runtime(settings)
    plan_fallback_limitation: str | None = None
    if runtime is None:
        plan = _heuristic_plan(request, registry)
    else:
        specs = [CapabilitySpec(name=item.name, description=item.description) for item in tools]
        try:
            plan = runtime.create_plan(request, specs)
            plan = _validate_plan(plan, registry, settings.agent_max_steps)
        except AgentRuntimeError:
            if request.answer_mode != "research_chat":
                raise
            plan = _validate_plan(
                _heuristic_plan(request, registry),
                registry,
                settings.agent_max_steps,
            )
            plan_fallback_limitation = "模型计划未通过能力白名单校验；本轮已改用当前研究空间的确定性计划。"
    if request.scope_context.get("capability_config_ids") and "capability_readiness" in registry:
        selected = [name for name in plan.selected_capabilities if name != "capability_readiness"]
        plan.selected_capabilities = [
            "capability_readiness",
            *selected[: max(0, settings.agent_max_steps - 1)],
        ]
        plan.public_steps = [
            "读取并校验本轮所选能力配置",
            *[step for step in plan.public_steps if "能力配置" not in step],
        ][: settings.agent_max_steps]
    if "selected_context" in registry:
        plan.selected_capabilities = [
            "selected_context",
            *[name for name in plan.selected_capabilities if name != "selected_context"],
        ][: settings.agent_max_steps]
    _emit_progress(
        progress,
        "plan.ready",
        {
            "workflow": plan.workflow,
            "scope_summary": plan.scope_summary,
            "steps": plan.public_steps
            or [f"调用「{registry[name].description}」" for name in plan.selected_capabilities],
            "capabilities": [
                {"name": name, "description": registry[name].description}
                for name in plan.selected_capabilities
            ],
        },
    )
    results: list[ToolResult] = []
    for name in plan.selected_capabilities:
        _emit_progress(
            progress,
            "capability.started",
            {"capability": name, "description": registry[name].description},
        )
        result = registry[name].result
        results.append(result)
        _emit_progress(
            progress,
            "capability.completed",
            {
                "capability": name,
                "status": result.status,
                "evidence_count": len(result.evidence),
                "fact_count": len(result.facts),
            },
        )
    evidence = _unique_evidence(results)
    facts = _unique_facts(results)
    _validate_facts(facts, evidence)
    _emit_progress(
        progress,
        "evidence.validated",
        {
            "stage": "controlled_evidence",
            "evidence_count": len(evidence),
            "fact_count": len(facts),
            "next_stage": "compose_answer",
        },
    )
    if runtime is None:
        draft = _deterministic_draft(
            results,
            facts,
            fallback_related_questions,
            extra_limitations=["当前为受控证据整理模式，未调用生成模型。"],
        )
        runtime_name = "evidence_only"
        status = "evidence_only"
    else:
        draft = runtime.synthesize(request, plan, results)
        if request.answer_mode == "research_chat":
            _repair_research_chat_fact_ids(draft, facts)
        try:
            _validate_draft(draft, evidence, facts, answer_mode=request.answer_mode)
        except AgentRuntimeError:
            if request.answer_mode != "research_chat":
                raise
            draft = _deterministic_draft(
                results,
                facts,
                fallback_related_questions,
                extra_limitations=[
                    "模型综合结果未通过事实—证据校验；本轮已退回受控证据原文，未采用未验证表达。"
                ],
            )
            status = "guardrail_fallback"
        else:
            status = "succeeded"
        runtime_name = runtime.runtime_name
    cited_ids = {
        evidence_id
        for section in draft.sections
        for claim in section.claims
        for evidence_id in claim.evidence_ids
    }
    citations = [item for item in evidence if item.evidence_id in cited_ids]
    artifact = AnswerArtifact(
        status=status,
        runtime=runtime_name,
        workflow=plan.workflow,
        sections=draft.sections,
        citations=citations,
        calculations=[calculation for result in results for calculation in result.calculations],
        tool_trace=[
            {
                "capability": result.capability,
                "status": result.status,
                "evidence_count": len(result.evidence),
                "fact_count": len(result.facts),
            }
            for result in results
        ],
        limitations=[
            *[limitation for result in results for limitation in result.limitations],
            *draft.limitations,
            *([plan_fallback_limitation] if plan_fallback_limitation else []),
        ],
        related_questions=draft.related_questions,
    )
    return CountryAgentResult(plan=plan, artifact=artifact)


def _emit_progress(
    callback: AgentProgressCallback | None,
    event: str,
    payload: dict[str, Any],
) -> None:
    if callback is not None:
        callback(event, payload)


def artifact_markdown(artifact: AnswerArtifact) -> str:
    blocks: list[str] = []
    for section in artifact.sections:
        claims = "\n".join(f"- {claim.text}" for claim in section.claims)
        blocks.append(f"### {section.title}\n\n{claims}")
    if not blocks:
        blocks.append("### 证据不足\n\n当前问题没有可用的已复核事实。")
    return "\n\n".join(blocks)


def _runtime(settings: Settings) -> StructuredAgentRuntime | None:
    if settings.agent_runtime == "user_api":
        from app.services.public_demo_runtime import current_runtime

        return current_runtime(settings)
    if settings.agent_runtime == "evidence_only":
        return None
    if settings.agent_runtime == "codex_local":
        if settings.environment == "production":
            raise AgentRuntimeError("local Codex runtime is disabled in production")
        return CodexLocalRuntime(
            binary=settings.agent_codex_binary,
            model=settings.agent_model,
            timeout_seconds=settings.agent_timeout_seconds,
        )
    if settings.agent_runtime == "coze_test":
        if settings.environment == "production":
            raise AgentRuntimeError("Coze test runtime is disabled in production")
        if settings.agent_coze_token is None:
            raise AgentRuntimeError("GUOBIE_AGENT_COZE_TOKEN is required")
        return CozeTestRuntime(
            token=settings.agent_coze_token,
            bot_id=settings.agent_coze_bot_id,
            timeout_seconds=settings.agent_timeout_seconds,
        )
    if settings.agent_runtime == "openai_responses":
        if settings.agent_openai_api_key is None:
            raise AgentRuntimeError("GUOBIE_AGENT_OPENAI_API_KEY is required")
        return OpenAIResponsesRuntime(
            api_key=settings.agent_openai_api_key,
            model=settings.agent_model,
            timeout_seconds=settings.agent_timeout_seconds,
        )
    raise AgentRuntimeError("unsupported agent runtime")


def get_structured_agent_runtime(settings: Settings) -> StructuredAgentRuntime | None:
    """Return the configured structured runtime for evidence-bounded specialist workflows."""

    return _runtime(settings)


def _heuristic_plan(
    request: AgentRequest,
    registry: dict[str, RegisteredAgentTool],
) -> ExecutionPlan:
    lowered = request.question.lower()
    selected: list[str] = []
    if request.scope_type == "event":
        if any(word in lowered for word in ("缺口", "缺少", "还需", "还缺")):
            selected.append("event_evidence_gap")
        elif any(word in lowered for word in ("关联", "相关事件")):
            selected.append("related_events")
        elif any(word in lowered for word in ("差异", "立场", "数字", "主张")):
            selected.extend(("event_evidence_compare", "claim_difference"))
        else:
            selected.extend(("event_timeline", "event_evidence_compare"))
    elif request.scope_type == "topic":
        if any(word in lowered for word in ("缺口", "不足", "还需", "还缺")):
            selected.append("topic_gap_analysis")
        elif any(word in lowered for word in ("周报", "摘要", "预览")):
            selected.append("topic_digest_preview")
        elif any(word in lowered for word in ("事件", "时间线")):
            selected.append("topic_event_overview")
        elif any(word in lowered for word in ("搜索", "查找", "材料")):
            selected.append("topic_material_search")
        else:
            selected.append("topic_evidence_summary")
    elif request.scope_type == "capability":
        if any(word in lowered for word in ("解释", "为什么", "失败")):
            selected.append("capability_run_explain")
        elif any(word in lowered for word in ("预览", "产物")):
            selected.append("capability_run_preview")
        else:
            selected.append("capability_readiness")
    elif request.scope_type == "resource":
        selected.append(
            "source_coverage_check"
            if any(word in lowered for word in ("覆盖", "缺口"))
            else "source_catalog_search"
        )
    elif request.scope_type == "system":
        selected.append("readiness_explain")
    elif any(word in lowered for word in ("首都", "货币", "官方语言", "国土面积", "面积", "basic fact")):
        selected.append("country_basic_facts")
    elif any(word in lowered for word in ("事件", "冲突", "安全", "武装", "战争", "主张", "差异", "m23")):
        selected.append("event_evidence_compare")
    elif any(word in lowered for word in ("政策", "法规", "税", "准入", "矿业法")):
        selected.append("policy_timeline")
    elif any(word in lowered for word in ("矿产", "资源", "贸易", "投资", "中国", "供应链")):
        selected.extend(("trade_trend", "policy_timeline", "freshness_check"))
    elif any(word in lowered for word in ("趋势", "增长", "变化", "同比", "十年")):
        selected.append("indicator_trend")
    elif any(word in lowered for word in ("新鲜", "时效", "更新", "最近", "过期")):
        selected.append("freshness_check")
    else:
        selected.append("country_snapshot")
    selected = [name for name in dict.fromkeys(selected) if name in registry]
    return ExecutionPlan(
        workflow="evidence_synthesis",
        selected_capabilities=selected,
        scope_summary=f"{request.scope_type}:{request.scope_key or request.country_iso3} 受控证据",
        public_steps=[f"读取并核对「{registry[name].description}」" for name in selected],
    )


def _validate_plan(
    plan: ExecutionPlan,
    registry: dict[str, RegisteredAgentTool],
    max_steps: int,
) -> ExecutionPlan:
    if plan.workflow != "bounded_agent":
        plan.workflow = "bounded_agent"
    selected = list(dict.fromkeys(plan.selected_capabilities))
    if not selected:
        raise AgentRuntimeError("agent plan did not select a capability")
    unknown = [name for name in selected if name not in registry]
    if unknown:
        raise AgentRuntimeError("agent plan selected an unregistered capability")
    if len(selected) > max_steps:
        raise AgentRuntimeError("agent plan exceeded the configured step limit")
    plan.selected_capabilities = selected
    return plan


def _unique_evidence(results: list[ToolResult]) -> list[EvidenceItem]:
    by_id: dict[str, EvidenceItem] = {}
    for result in results:
        for item in result.evidence:
            by_id.setdefault(item.evidence_id, item)
    return list(by_id.values())


def _unique_facts(results: list[ToolResult]) -> list[FactItem]:
    by_id: dict[str, FactItem] = {}
    for result in results:
        for item in result.facts:
            by_id.setdefault(item.fact_id, item)
    return list(by_id.values())


def _validate_facts(facts: list[FactItem], evidence: list[EvidenceItem]) -> None:
    valid_ids = {item.evidence_id for item in evidence}
    for fact in facts:
        if fact.review_status != "confirmed":
            raise AgentRuntimeError("controlled fact is not confirmed")
        if not fact.evidence_ids or set(fact.evidence_ids) - valid_ids:
            raise AgentRuntimeError("controlled fact references invalid evidence")


def _deterministic_draft(
    results: list[ToolResult],
    facts: list[FactItem],
    related_questions: list[str],
    *,
    extra_limitations: list[str],
) -> AnswerDraft:
    return AnswerDraft(
        sections=[
            AnswerSection(
                title=_capability_title(result.capability),
                claims=[
                    AnswerClaim(
                        text=fact.text,
                        fact_ids=[fact.fact_id],
                        evidence_ids=fact.evidence_ids,
                        origin=fact.origin,
                        verification_status=fact.verification_status,
                    )
                    for fact in result.facts
                ],
            )
            for result in results
            if result.facts
        ],
        limitations=[
            *([] if facts else ["当前问题没有可用的已复核事实，无法生成事实性回答。"]),
            *extra_limitations,
        ],
        related_questions=related_questions,
    )


def _validate_draft(
    draft: AnswerDraft,
    evidence: list[EvidenceItem],
    facts: list[FactItem],
    *,
    answer_mode: str,
) -> None:
    valid_evidence_ids = {item.evidence_id for item in evidence}
    fact_by_id = {item.fact_id: item for item in facts}
    used_fact_ids: set[str] = set()
    for section in draft.sections:
        for claim in section.claims:
            if not claim.fact_ids or any(fact_id not in fact_by_id for fact_id in claim.fact_ids):
                raise AgentRuntimeError("agent answer cited a fact outside the controlled bundle")
            cited_facts = [fact_by_id[fact_id] for fact_id in claim.fact_ids]
            if answer_mode == "formal_artifact" and (
                len(cited_facts) != 1 or claim.text != cited_facts[0].text
            ):
                raise AgentRuntimeError("agent answer changed controlled fact text")
            expected_evidence_ids = list(
                dict.fromkeys(evidence_id for fact in cited_facts for evidence_id in fact.evidence_ids)
            )
            if set(claim.evidence_ids) != set(expected_evidence_ids):
                raise AgentRuntimeError("agent answer changed the fact evidence mapping")
            if any(
                claim.origin != fact.origin or claim.verification_status != fact.verification_status
                for fact in cited_facts
            ):
                raise AgentRuntimeError("agent answer changed the fact provenance classification")
            if set(claim.evidence_ids) - valid_evidence_ids:
                raise AgentRuntimeError("agent answer cited evidence outside the controlled bundle")
            used_fact_ids.update(claim.fact_ids)
    if facts and not used_fact_ids:
        raise AgentRuntimeError("agent answer did not use the controlled facts")


def _repair_research_chat_fact_ids(draft: AnswerDraft, facts: list[FactItem]) -> None:
    valid_fact_ids = {fact.fact_id for fact in facts}
    for section in draft.sections:
        for claim in section.claims:
            if claim.fact_ids and set(claim.fact_ids) <= valid_fact_ids:
                continue
            cited_evidence = set(claim.evidence_ids)
            if not cited_evidence:
                continue
            candidates = [
                fact
                for fact in facts
                if fact.origin == claim.origin
                and fact.verification_status == claim.verification_status
                and set(fact.evidence_ids) <= cited_evidence
            ]
            covered_evidence = {evidence_id for fact in candidates for evidence_id in fact.evidence_ids}
            if candidates and covered_evidence == cited_evidence:
                claim.fact_ids = [fact.fact_id for fact in candidates]


def _capability_title(capability: str) -> str:
    return {
        "country_basic_facts": "基础国情档案",
        "country_snapshot": "国别宏观底账",
        "indicator_trend": "指标趋势",
        "trade_trend": "贸易趋势",
        "policy_timeline": "政策时间线",
        "event_evidence_compare": "事件证据对比",
        "freshness_check": "数据新鲜度",
        "event_timeline": "事件时间线",
        "claim_difference": "来源主张差异",
        "related_events": "关联事件",
        "event_evidence_gap": "事件证据缺口",
        "topic_material_search": "专题材料检索",
        "topic_evidence_summary": "专题证据摘要",
        "topic_event_overview": "专题事件概览",
        "topic_gap_analysis": "专题证据缺口",
        "topic_digest_preview": "专题周报预览",
        "capability_readiness": "能力就绪度",
        "capability_run_preview": "能力产物预览",
        "capability_run_explain": "能力运行解释",
        "source_catalog_search": "信源目录",
        "source_coverage_check": "信源覆盖检查",
        "readiness_explain": "系统就绪度",
    }.get(capability, "受控证据")
