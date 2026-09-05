# -*- coding: utf-8 -*-
"""
===================================
Agent 分析流（从 StockAnalysisPipeline 迁出）
===================================

职责：
1. agent 模式分析与上下文装载（analyze_with_agent 等，原 _analyze_with_agent）
2. agent 结果 -> AnalysisResult 转换（agent_result_to_analysis_result）
3. 决策仪表盘字段回填与趋势降级（backfill_agent_dashboard_fields / apply_trend_fallback 等）

与 orchestrator 的关系：
- 实例方法首参 ``pipeline`` 即原 ``self``，仍访问实例状态；
- 部分模块级名字经由调用时 ``import src.core.pipeline as _pipeline`` 读取——
  保持既有测试对 ``src.core.pipeline.X`` 的 patch 语义（调用时解析）；
- 其余依赖为普通导入。
"""

from typing import Any, Dict, Optional

from data_provider.realtime_types import ChipDistribution
from src.analyzer import AnalysisResult
from src.services.stock_list_parser import AnalysisTarget, ParseStatus
from src.enums import ReportType
from src.services.daily_market_context import DailyMarketContext
from src.stock_analyzer import TrendAnalysisResult


def analyze_with_agent(
    pipeline, 
    code: str, 
    report_type: ReportType, 
    query_id: str,
    stock_name: str,
    realtime_quote: Any,
    chip_data: Optional[ChipDistribution],
    fundamental_context: Optional[Dict[str, Any]] = None,
    trend_result: Optional[TrendAnalysisResult] = None,
    *,
    market_phase_context: Optional[Dict[str, Any]] = None,
    market_phase_summary: Optional[Dict[str, Any]] = None,
    daily_market_context: Optional[DailyMarketContext] = None,
    portfolio_context: Optional[Dict[str, Any]] = None,
    market_structure_context: Optional[Dict[str, Any]] = None,
    analysis_target: Optional[AnalysisTarget] = None,
) -> Optional[AnalysisResult]:
    """
    使用 Agent 模式分析单只股票。
    """

    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    PipelineActionAdjustment = _pipeline.PipelineActionAdjustment
    activate_news_evidence_scope = _pipeline.activate_news_evidence_scope
    apply_daily_market_context_guardrail = _pipeline.apply_daily_market_context_guardrail
    apply_phase_decision_guardrails = _pipeline.apply_phase_decision_guardrails
    build_pipeline_final_explanation = _pipeline.build_pipeline_final_explanation
    capture_pipeline_action_adjustment = _pipeline.capture_pipeline_action_adjustment
    current_diagnostic_snapshot = _pipeline.current_diagnostic_snapshot
    fill_price_position_if_needed = _pipeline.fill_price_position_if_needed
    get_current_news_evidence = _pipeline.get_current_news_evidence
    get_market_for_stock = _pipeline.get_market_for_stock
    is_us_stock_code = _pipeline.is_us_stock_code
    logger = _pipeline.logger
    news_evidence_present = _pipeline.news_evidence_present
    normalize_chip_structure_availability = _pipeline.normalize_chip_structure_availability
    normalize_decision_action = _pipeline.normalize_decision_action
    normalize_report_language = _pipeline.normalize_report_language
    normalize_stock_code = _pipeline.normalize_stock_code
    record_history_run = _pipeline.record_history_run
    record_llm_run = _pipeline.record_llm_run
    record_llm_run_started = _pipeline.record_llm_run_started
    reset_news_evidence_scope = _pipeline.reset_news_evidence_scope
    stabilize_decision_with_structure = _pipeline.stabilize_decision_with_structure
    time = _pipeline.time

    try:
        from src.agent.factory import build_agent_executor
        report_language = normalize_report_language(getattr(pipeline.config, "report_language", "zh"))

        is_index = (
            analysis_target is not None
            and analysis_target.asset_type == ParseStatus.INDEX
        )

        requested_skills = (
            pipeline.analysis_skills
            if pipeline.analysis_skills is not None
            else (getattr(pipeline.config, 'agent_skills', None) or None)
        )
        # Build executor from shared factory (ToolRegistry and SkillManager prototype are cached)
        executor = build_agent_executor(pipeline.config, requested_skills)

        # 指数目标：从 Agent 工具面剔除与 INDEX_SKIP_MODULES 对应的底层 provider
        # 工具（筹码/基本面/资金流），确保 Agent 分支同样零调用（Story 1.5 V6）。
        if is_index:
            executor = pipeline._filter_agent_tools_for_index(executor)

        # Build initial context to avoid redundant tool calls
        initial_context = {
            "stock_code": code,
            "stock_name": stock_name,
            "report_type": report_type.value,
            "report_language": report_language,
            "fundamental_context": fundamental_context,
        }
        if isinstance(portfolio_context, dict):
            initial_context["portfolio_context"] = dict(portfolio_context)
        if pipeline.analysis_skills is not None:
            initial_context["skills"] = pipeline.analysis_skills
        if market_phase_context is not None:
            initial_context["market_phase_context"] = market_phase_context
        if isinstance(market_structure_context, dict):
            initial_context["market_structure_context"] = market_structure_context
        pipeline._attach_daily_market_context(
            initial_context,
            daily_market_context,
            report_language=report_language,
        )
        
        if realtime_quote:
            initial_context["realtime_quote"] = pipeline._safe_to_dict(realtime_quote)
        if chip_data:
            initial_context["chip_distribution"] = pipeline._safe_to_dict(chip_data)
        if trend_result:
            initial_context["trend_result"] = pipeline._safe_to_dict(trend_result)

        # Agent path: inject social sentiment as news_context so both
        # executor (_build_user_message) and orchestrator (ctx.set_data)
        # can consume it through the existing news_context channel
        social_evidence_context: Optional[str] = None
        if pipeline.social_sentiment_service is not None and pipeline.social_sentiment_service.is_available and is_us_stock_code(code):
            try:
                social_context = pipeline.social_sentiment_service.get_social_context(code)
                if social_context:
                    social_evidence_context = social_context
                    existing = initial_context.get("news_context")
                    if existing:
                        initial_context["news_context"] = existing + "\n\n" + social_context
                    else:
                        initial_context["news_context"] = social_context
                    logger.info(f"[{code}] Agent mode: social sentiment data injected into news_context")
            except Exception as e:
                logger.warning(f"[{code}] Agent mode: social sentiment fetch failed: {e}")

        persisted_intelligence_context = pipeline._load_persisted_intelligence_context(
            code=code,
            stock_name=stock_name,
            market=("cn" if is_index else get_market_for_stock(normalize_stock_code(code)) or "cn"),
        )
        if persisted_intelligence_context:
            existing = initial_context.get("news_context")
            initial_context["news_context"] = (
                f"{existing}\n\n{persisted_intelligence_context}"
                if existing
                else persisted_intelligence_context
            )
            logger.info(f"[{code}] Agent mode: local intelligence evidence injected into news_context")

        # Issue #1066: ensure deep history is in DB before agent tools run
        if analysis_target is None:
            pipeline._ensure_agent_history(code)
        else:
            pipeline._ensure_agent_history(code, analysis_target=analysis_target)

        analysis_context = pipeline._load_agent_analysis_context(
            code, stock_name, analysis_target=analysis_target
        )
        market = "cn" if is_index else get_market_for_stock(normalize_stock_code(code))
        (
            analysis_context_pack_summary,
            analysis_context_pack_overview,
        ) = pipeline._build_analysis_context_pack_outputs(
            pipeline._build_agent_analysis_artifacts(
                code=code,
                stock_name=stock_name,
                market=market,
                phase=market_phase_context,
                initial_context=initial_context,
                fundamental_context=fundamental_context,
                query_id=query_id,
                base_context=analysis_context,
                portfolio_context=portfolio_context,
            ),
            report_language=report_language,
            code=code,
            query_id=query_id,
        )
        if analysis_context_pack_summary:
            initial_context["analysis_context_pack_summary"] = analysis_context_pack_summary

        # 运行 Agent
        if report_language in ("en", "ko"):
            message = f"Analyze stock {code} ({stock_name}) and return the full decision dashboard JSON."
        else:
            message = f"请分析股票 {code} ({stock_name})，并生成决策仪表盘报告。"
        llm_started_at = time.monotonic()
        # Agent 自己调用搜索工具取证，所以披露计数只能来自这些工具的真实返回；
        # 分析结束后补打的 search_stock_news() 与 Agent 消费的证据无关。
        # 累加器对象在这里持有引用，reset 之后仍可安全读取。
        news_evidence_token = activate_news_evidence_scope()
        news_evidence = get_current_news_evidence()
        try:
            record_llm_run_started(
                model=getattr(pipeline.config, "agent_litellm_model", None),
                call_type="agent_analysis",
            )
            agent_result = executor.run(message, context=initial_context)
        except Exception as exc:
            record_llm_run(
                success=False,
                model=getattr(pipeline.config, "agent_litellm_model", None),
                call_type="agent_analysis",
                duration_ms=int((time.monotonic() - llm_started_at) * 1000),
                error_type=type(exc).__name__,
                error_message=exc,
            )
            raise
        finally:
            reset_news_evidence_scope(news_evidence_token)

        # 转换为 AnalysisResult
        result = pipeline._agent_result_to_analysis_result(
            agent_result,
            code,
            stock_name,
            report_type,
            query_id,
            trend_result=trend_result,
        )

        # 三态计数取自 Agent 实际消费的搜索工具结果：渠道不可用为 None（未执行
        # 检索），渠道可用则从 0 起步、拿到多少算多少。
        if result is not None and news_evidence is not None:
            result.news_result_count = news_evidence.resolve(
                search_available=bool(
                    pipeline.search_service is not None
                    and pipeline.search_service.is_available
                ),
            )
            # 与普通路径同样按来源逐个登记：Agent 运行期自己搜到的条数、注入的
            # 社交情绪、注入的本地资讯池。这条路径不经过 format_intel_report()，
            # 但仍不传拼好的整段，避免以后有人往里加会造占位文本的来源。
            result.news_evidence_present = news_evidence_present(
                result.news_result_count,
                social_evidence_context,
                persisted_intelligence_context,
            )
        record_llm_run(
            success=bool(result and getattr(result, "success", True)),
            model=getattr(result, "model_used", None) if result else getattr(agent_result, "model", None),
            call_type="agent_analysis",
            duration_ms=int((time.monotonic() - llm_started_at) * 1000),
            error_type=(
                None
                if result and getattr(result, "success", True)
                else "AgentResultError"
            ),
            error_message=(
                getattr(result, "error_message", None)
                if result and not getattr(result, "success", True)
                else ("Agent returned empty result" if result is None else None)
            ),
        )
        if result:
            result.query_id = query_id
        # Agent weak integrity: placeholder fill only, no LLM retry
        if result and getattr(pipeline.config, "report_integrity_enabled", False):
            from src.analyzer import check_content_integrity, apply_placeholder_fill

            pass_integrity, missing = check_content_integrity(
                result,
                require_phase_decision=isinstance(market_phase_summary, dict),
            )
            if not pass_integrity:
                apply_placeholder_fill(result, missing)
                logger.info(
                    "[LLM完整性] integrity_mode=agent_weak 必填字段缺失 %s，已占位补全",
                    missing,
                )
        # chip_structure fallback (Issue #589), before save_analysis_history
        if result and chip_data is not None:
            normalize_chip_structure_availability(result, chip_data)

        # price_position fallback (same as non-agent path Step 7.7)
        if result:
            pipeline_adjustments: list[PipelineActionAdjustment] = []
            runtime_facts = getattr(agent_result, "runtime_facts", None)
            pipeline_start_signal = getattr(result, "decision_type", "hold")
            risk_application = (
                getattr(runtime_facts, "risk_override_application", None)
                if runtime_facts is not None
                else None
            )
            if risk_application is not None:
                pipeline_start_signal = risk_application.post_risk_signal.value
            initial_action_advice = getattr(result, "operation_advice", None)
            pipeline._refresh_decision_action_for_final_result(
                result,
                report_type=report_type.value,
                previous_operation_advice=initial_action_advice,
            )
            pipeline_start_action = normalize_decision_action(
                getattr(result, "action", None)
            )
            action_chain_valid = pipeline_start_action is not None
            fill_price_position_if_needed(result, trend_result, realtime_quote)
            realtime_data = initial_context.get("realtime_quote", {})
            if isinstance(realtime_data, dict):
                result.current_price = realtime_data.get("price")
                result.change_pct = realtime_data.get("change_pct")
            action_before_guardrail = getattr(result, "action", None)
            advice_before_guardrail = getattr(result, "operation_advice", None)
            stabilize_decision_with_structure(result, trend_result, fundamental_context)
            pipeline._refresh_decision_action_for_final_result(
                result,
                report_type=report_type.value,
                previous_operation_advice=advice_before_guardrail,
            )
            action_after_guardrail = normalize_decision_action(
                getattr(result, "action", None)
            )
            if action_chain_valid and action_after_guardrail is not None:
                capture_pipeline_action_adjustment(
                    pipeline_adjustments,
                    source="structure_and_fundamentals",
                    before=action_before_guardrail,
                    after=action_after_guardrail,
                )
            else:
                action_chain_valid = False
            action_before_guardrail = getattr(result, "action", None)
            advice_before_guardrail = getattr(result, "operation_advice", None)
            adjustments = apply_phase_decision_guardrails(
                result,
                market_phase_summary=market_phase_summary,
                analysis_context_pack_overview=analysis_context_pack_overview,
                report_language=getattr(result, "report_language", None)
                or getattr(pipeline.config, "report_language", "zh"),
            )
            if adjustments:
                logger.info("[phase_decision_guardrail] Applied agent adjustments for %s: %s", code, adjustments)
            pipeline._refresh_decision_action_for_final_result(
                result,
                report_type=report_type.value,
                previous_operation_advice=advice_before_guardrail,
            )
            action_after_guardrail = normalize_decision_action(
                getattr(result, "action", None)
            )
            if action_chain_valid and action_after_guardrail is not None:
                capture_pipeline_action_adjustment(
                    pipeline_adjustments,
                    source="market_phase",
                    before=action_before_guardrail,
                    after=action_after_guardrail,
                )
            else:
                action_chain_valid = False
            action_before_guardrail = getattr(result, "action", None)
            advice_before_guardrail = getattr(result, "operation_advice", None)
            market_context_adjustments = apply_daily_market_context_guardrail(
                result,
                daily_market_context=initial_context.get("daily_market_context"),
                report_language=getattr(result, "report_language", None)
                or getattr(pipeline.config, "report_language", "zh"),
            )
            if market_context_adjustments:
                logger.info(
                    "[daily_market_context_guardrail] Applied agent adjustments for %s: %s",
                    code,
                    market_context_adjustments,
                )
            pipeline._refresh_decision_action_for_final_result(
                result,
                report_type=report_type.value,
                previous_operation_advice=advice_before_guardrail,
            )
            action_after_guardrail = normalize_decision_action(
                getattr(result, "action", None)
            )
            if action_chain_valid and action_after_guardrail is not None:
                capture_pipeline_action_adjustment(
                    pipeline_adjustments,
                    source="daily_market_context",
                    before=action_before_guardrail,
                    after=action_after_guardrail,
                )
            else:
                action_chain_valid = False
            if isinstance(fundamental_context, dict):
                result.fundamental_context = fundamental_context
            if isinstance(market_structure_context, dict):
                result.market_structure_context = market_structure_context
            result.market_phase_summary = market_phase_summary
            result.analysis_context_pack_overview = analysis_context_pack_overview
            final_action = normalize_decision_action(getattr(result, "action", None))
            if isinstance(result.dashboard, dict):
                result.dashboard.pop("agent_disagreement_explanation", None)
            if (
                runtime_facts is not None
                and action_chain_valid
                and pipeline_start_action is not None
                and final_action is not None
            ):
                if not isinstance(result.dashboard, dict):
                    result.dashboard = {}
                result.dashboard["agent_disagreement_explanation"] = (
                    build_pipeline_final_explanation(
                        runtime_facts=runtime_facts,
                        pipeline_start_signal=pipeline_start_signal,
                        pipeline_start_action=pipeline_start_action,
                        final_action=final_action,
                        pipeline_adjustments=pipeline_adjustments,
                        data_quality=(
                            analysis_context_pack_overview.get("data_quality")
                            if isinstance(analysis_context_pack_overview, dict)
                            else None
                        ),
                    )
                )

        if result:
            pipeline._append_daily_data_source(
                result,
                analysis_context,
                analysis_target,
            )

        resolved_stock_name = result.name if result and result.name else stock_name

        # 保存新闻情报到数据库（Agent 工具结果仅用于 LLM 上下文，未持久化，Fixes #396）
        # 使用 search_stock_news（与 Agent 工具调用逻辑一致），仅 1 次 API 调用，无额外延迟
        if pipeline.search_service is not None and pipeline.search_service.is_available:
            try:
                news_response = pipeline.search_service.search_stock_news(
                    stock_code=("" if is_index else code),
                    stock_name=resolved_stock_name,
                    max_results=5
                )
                # 这次补查只为持久化新闻情报（Fixes #396），刻意不写
                # result.news_result_count：它发生在分析结束之后，与 Agent 实际
                # 消费的证据无关，用它做披露判定会两个方向都失真。真正的计数在
                # executor.run() 的证据作用域里收集（见上文）。
                if news_response.success and news_response.results:
                    query_context = pipeline._build_query_context(query_id=query_id)
                    pipeline.db.save_news_intel(
                        code=code,
                        name=resolved_stock_name,
                        dimension="latest_news",
                        query=news_response.query,
                        response=news_response,
                        query_context=query_context
                    )
                    logger.info(f"[{code}] Agent 模式: 新闻情报已保存 {len(news_response.results)} 条")
            except Exception as e:
                logger.warning(f"[{code}] Agent 模式保存新闻情报失败: {e}")

        # 保存分析历史记录
        if result and result.success:
            try:
                agent_context_snapshot = pipeline._build_context_snapshot(
                    enhanced_context={
                        **pipeline._without_runtime_prompt_context(initial_context),
                        "stock_name": resolved_stock_name,
                    },
                    news_content=initial_context.get("news_context"),
                    realtime_quote=realtime_quote,
                    chip_data=chip_data,
                    analysis_context_pack_overview=analysis_context_pack_overview,
                    market_phase_summary=market_phase_summary,
                )
                result.diagnostic_context_snapshot = agent_context_snapshot
                agent_context_snapshot["stock_name"] = resolved_stock_name
                saved_history_id = pipeline.db.save_analysis_history(
                    result=result,
                    query_id=query_id,
                    report_type=report_type.value,
                    news_content=None,
                    context_snapshot=agent_context_snapshot,
                    save_snapshot=pipeline.save_context_snapshot,
                )
                valid_saved_history_id = (
                    isinstance(saved_history_id, int)
                    and not isinstance(saved_history_id, bool)
                    and saved_history_id > 0
                )
                record_history_run(
                    report_saved=bool(saved_history_id),
                    metadata_saved=bool(saved_history_id),
                    analysis_history_id=(
                        saved_history_id if valid_saved_history_id else None
                    ),
                )
                if valid_saved_history_id:
                    pipeline._persist_skill_opinion_samples_after_history_save(
                        runtime_facts=getattr(agent_result, "runtime_facts", None),
                        analysis_history_id=saved_history_id,
                        stock_code=code,
                        analysis_context_pack_overview=analysis_context_pack_overview,
                    )
                    pipeline._extract_decision_signal_after_history_save(
                        result=result,
                        query_id=query_id,
                        source_report_id=saved_history_id,
                        report_type=report_type.value,
                        context_snapshot=agent_context_snapshot,
                        portfolio_context=portfolio_context,
                        analysis_target=analysis_target,
                    )
                latest_diagnostic_snapshot = current_diagnostic_snapshot()
                if latest_diagnostic_snapshot is not None:
                    agent_context_snapshot["diagnostics"] = latest_diagnostic_snapshot
                    result.diagnostic_context_snapshot = agent_context_snapshot
            except Exception as e:
                record_history_run(
                    report_saved=False,
                    metadata_saved=False,
                    error_message=e,
                )
                logger.warning(f"[{code}] 保存 Agent 分析历史失败: {e}")

        return result

    except Exception as e:
        logger.error(f"[{code}] Agent 分析失败: {e}")
        logger.exception(f"[{code}] Agent 详细错误信息:")
        return None

def load_agent_analysis_context(
    pipeline, code: str, stock_name: str, analysis_target: Optional[AnalysisTarget] = None
) -> Dict[str, Any]:
    """Load daily-bar context for Agent pack summaries without blocking analysis."""

    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    logger = _pipeline.logger

    try:
        context = pipeline._get_analysis_context_with_market_fallback(
            code, analysis_target=analysis_target
        )
    except Exception as exc:
        logger.warning(
            "[%s] Agent analysis context load failed; daily_bars will be marked missing: %s",
            code,
            exc,
        )
        context = None

    if isinstance(context, dict) and context:
        enriched = dict(context)
        enriched.setdefault("code", code)
        if stock_name:
            enriched.setdefault("stock_name", stock_name)
        return enriched

    return {
        "code": code,
        "stock_name": stock_name,
        "data_missing": True,
        "today": {},
        "yesterday": {},
    }

def agent_result_to_analysis_result(
    pipeline,
    agent_result,
    code: str,
    stock_name: str,
    report_type: ReportType,
    query_id: str,
    trend_result: Optional[TrendAnalysisResult] = None,
) -> AnalysisResult:
    """
    将 AgentResult 转换为 AnalysisResult。
    """

    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    get_unknown_text = _pipeline.get_unknown_text
    infer_decision_type_from_advice = _pipeline.infer_decision_type_from_advice
    localize_confidence_level = _pipeline.localize_confidence_level
    localize_operation_advice = _pipeline.localize_operation_advice
    normalize_report_language = _pipeline.normalize_report_language
    populate_decision_action_fields = _pipeline.populate_decision_action_fields


    report_language = normalize_report_language(getattr(pipeline.config, "report_language", "zh"))
    dash = None
    result = AnalysisResult(
        code=code,
        name=stock_name,
        sentiment_score=50,
        trend_prediction=get_unknown_text(report_language),
        operation_advice=localize_operation_advice("观望", report_language),
        confidence_level=localize_confidence_level("medium", report_language),
        report_language=report_language,
        success=agent_result.success,
        error_message=agent_result.error or None,
        data_sources=f"agent:{agent_result.provider}",
        model_used=agent_result.model or None,
    )

    if agent_result.success and agent_result.dashboard:
        dash = agent_result.dashboard
        ai_stock_name = str(dash.get("stock_name", "")).strip()
        if ai_stock_name and pipeline._is_placeholder_stock_name(stock_name, code):
            result.name = ai_stock_name

        nested_dashboard = dash.get("dashboard") if isinstance(dash, dict) else None

        raw_score = pipeline._agent_dashboard_value(
            dash,
            nested_dashboard,
            "sentiment_score",
            scalar=True,
        )
        if pipeline._is_agent_field_missing(raw_score, scalar=True):
            fallback_score = pipeline._trend_score_fallback(trend_result)
            if fallback_score is not None:
                result.sentiment_score = fallback_score
                pipeline._mark_trend_fallback_source(result)
        else:
            result.sentiment_score = pipeline._safe_int(raw_score, 50)

        raw_trend = pipeline._agent_dashboard_value(
            dash,
            nested_dashboard,
            "trend_prediction",
            scalar=True,
            expect_text=True,
        )
        if pipeline._is_agent_field_missing(raw_trend, scalar=True, expect_text=True):
            trend_label = pipeline._trend_label_fallback(
                trend_result,
                report_language,
            )
            if trend_label:
                result.trend_prediction = trend_label
                pipeline._mark_trend_fallback_source(result)
        else:
            result.trend_prediction = str(raw_trend)

        raw_advice = pipeline._agent_dashboard_value(
            dash,
            nested_dashboard,
            "operation_advice",
            scalar=True,
            allow_dict=True,
            expect_text=True,
        )
        extracted_advice = ""
        if isinstance(raw_advice, dict):
            # LLM may return {"no_position": "...", "has_position": "..."}
            extracted_advice = pipeline._extract_advice_text_from_dict(raw_advice)
            if extracted_advice:
                result.operation_advice = localize_operation_advice(
                    extracted_advice,
                    report_language,
                )
            else:
                signal_label = pipeline._trend_signal_fallback(
                    trend_result,
                    report_language,
                )
                if signal_label:
                    result.operation_advice = signal_label
                    pipeline._mark_trend_fallback_source(result)
        elif not pipeline._is_agent_field_missing(
            raw_advice,
            scalar=True,
            allow_dict=True,
            expect_text=True,
        ):
            result.operation_advice = str(raw_advice) if raw_advice else (localize_operation_advice("观望", report_language))
        else:
            signal_label = pipeline._trend_signal_fallback(trend_result, report_language)
            if signal_label:
                result.operation_advice = signal_label
                pipeline._mark_trend_fallback_source(result)
        from src.agent.protocols import normalize_decision_signal

        raw_decision = pipeline._agent_dashboard_value(
            dash,
            nested_dashboard,
            "decision_type",
            scalar=True,
            expect_text=True,
        )
        if pipeline._is_agent_field_missing(raw_decision, scalar=True, expect_text=True):
            trend_decision = pipeline._trend_decision_fallback(trend_result)
            decision_from_advice = infer_decision_type_from_advice(
                result.operation_advice,
                default="",
            )
            if decision_from_advice:
                result.decision_type = decision_from_advice
                if (
                    pipeline._is_agent_field_missing(
                        raw_advice,
                        scalar=True,
                        allow_dict=True,
                        expect_text=True,
                    )
                    and not extracted_advice
                    and trend_decision
                ):
                    pipeline._mark_trend_fallback_source(result)
            else:
                result.decision_type = trend_decision or "hold"
                if trend_decision:
                    pipeline._mark_trend_fallback_source(result)
        else:
            result.decision_type = normalize_decision_signal(raw_decision)
        result.confidence_level = localize_confidence_level(
            pipeline._agent_dashboard_value(dash, nested_dashboard, "confidence_level")
            or result.confidence_level,
            report_language,
        )
        raw_summary = pipeline._agent_dashboard_value(
            dash,
            nested_dashboard,
            "analysis_summary",
            scalar=True,
            expect_text=True,
        )
        if not pipeline._is_agent_field_missing(raw_summary, scalar=True, expect_text=True):
            result.analysis_summary = str(raw_summary)
        else:
            result.analysis_summary = pipeline._summary_fallback_from_result(result, report_language)
        top_level_phase_decision = dash.get("phase_decision") if isinstance(dash, dict) else None
        if isinstance(nested_dashboard, dict) and isinstance(top_level_phase_decision, dict):
            nested_dashboard = dict(nested_dashboard)
            nested_dashboard.setdefault("phase_decision", top_level_phase_decision)

        # The AI returns a top-level dict that contains a nested 'dashboard' sub-key
        # with core_conclusion / battle_plan / intelligence.  AnalysisResult's helper
        # methods (get_sniper_points, get_core_conclusion, etc.) expect that inner
        # structure, so we unwrap it here.
        result.dashboard = nested_dashboard or dash
        pipeline._backfill_agent_dashboard_fields(result, trend_result, report_language)
    else:
        pipeline._apply_trend_fallback(result, trend_result, report_language)
        if trend_result is not None:
            result.analysis_summary = (
                result.analysis_summary
                or pipeline._summary_fallback_from_result(result, report_language)
            )
            pipeline._backfill_agent_dashboard_fields(result, trend_result, report_language)
        if not result.error_message:
            result.error_message = (
                "Agent failed to generate a valid decision dashboard" if report_language == "en"
                else "에이전트가 유효한 결정 대시보드를 생성하지 못했습니다" if report_language == "ko"
                else "Agent 未能生成有效的决策仪表盘"
            )

    explicit_action = dash.get("action") if isinstance(dash, dict) else None
    if explicit_action is None and isinstance(getattr(result, "dashboard", None), dict):
        explicit_action = result.dashboard.get("action")
    return populate_decision_action_fields(result, explicit_action=explicit_action)


def refresh_decision_action_for_final_result(
    result: AnalysisResult,
    *,
    report_type: Any,
    previous_operation_advice: Any,
) -> AnalysisResult:
    # A guardrail may rewrite the advice after the Agent action was parsed;
    # discard that stale action before using the same resolver as the
    # downstream DecisionSignal builder.


    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    resolve_decision_signal_action_fields = _pipeline.resolve_decision_signal_action_fields

    previous_advice = str(previous_operation_advice or "").strip()
    current_advice = str(getattr(result, "operation_advice", None) or "").strip()
    if previous_advice != current_advice:
        result.action = None
        result.action_label = None
    fields = resolve_decision_signal_action_fields(
        result,
        report_type=str(report_type or ""),
    )
    result.action = fields["action"]
    result.action_label = fields["action_label"]
    return result


def agent_dashboard_value(
    dash: Dict[str, Any],
    nested_dashboard: Any,
    key: str,
    *,
    scalar: bool = False,
    allow_dict: bool = False,
    expect_text: bool = False,
) -> Any:
    """Read a scalar from top-level agent payload, then nested dashboard fallback."""

    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    StockAnalysisPipeline = _pipeline.StockAnalysisPipeline


    value = dash.get(key) if isinstance(dash, dict) else None
    if isinstance(nested_dashboard, dict) and StockAnalysisPipeline._is_agent_field_missing(
        value,
        scalar=scalar,
        allow_dict=allow_dict,
        expect_text=expect_text,
    ):
        nested_value = nested_dashboard.get(key)
        if not StockAnalysisPipeline._is_agent_field_missing(
            nested_value,
            scalar=scalar,
            allow_dict=allow_dict,
            expect_text=expect_text,
        ):
            value = nested_value
    return value


def extract_advice_text_from_dict(raw_advice: dict) -> str:


    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    StockAnalysisPipeline = _pipeline.StockAnalysisPipeline

    for field in ("has_position", "no_position"):
        if isinstance(raw_advice.get(field), str):
            text = raw_advice[field].strip()
            if not StockAnalysisPipeline._is_agent_placeholder_text(text):
                return text

    for value in raw_advice.values():
        if isinstance(value, str):
            text = value.strip()
            if not StockAnalysisPipeline._is_agent_placeholder_text(text):
                return text

    return ""


def is_agent_placeholder_text(text: str) -> bool:
    if not text:
        return True
    return text.lower() in {"n/a", "na", "none", "null", "unknown", "tbd"} or text in {
        "未知",
        "待补充",
        "数据缺失",
        "无",
    }


def is_agent_field_missing(
    value: Any,
    *,
    scalar: bool = False,
    allow_dict: bool = False,
    expect_text: bool = False,
) -> bool:


    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    StockAnalysisPipeline = _pipeline.StockAnalysisPipeline

    if scalar and isinstance(value, dict):
        if not allow_dict or not value:
            return True
        return not StockAnalysisPipeline._extract_advice_text_from_dict(value)
    if value is None:
        return True
    if expect_text and scalar:
        if not isinstance(value, str):
            return True
    if isinstance(value, str):
        text = value.strip()
        return StockAnalysisPipeline._is_agent_placeholder_text(text)
    if isinstance(value, dict):
        if scalar:
            return not allow_dict
        return not value
    if scalar and isinstance(value, (list, tuple, set)):
        return True
    return False


def trend_score_fallback(trend_result: Optional[TrendAnalysisResult]) -> Optional[int]:
    if trend_result is None:
        return None
    try:
        score = int(getattr(trend_result, "signal_score", 0))
    except (TypeError, ValueError):
        return None
    return score if score > 0 else None


def trend_label_fallback(
    trend_result: Optional[TrendAnalysisResult],
    report_language: str = "zh",
) -> str:


    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    localize_trend_prediction = _pipeline.localize_trend_prediction

    if trend_result is None:
        return ""
    trend_status = getattr(trend_result, "trend_status", None)
    value = getattr(trend_status, "value", None) or str(trend_status or "").strip()
    if report_language != "en":
        return value
    return localize_trend_prediction(value, report_language)


def trend_signal_fallback(
    trend_result: Optional[TrendAnalysisResult],
    report_language: str = "zh",
) -> str:


    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    localize_operation_advice = _pipeline.localize_operation_advice

    if trend_result is None:
        return ""
    buy_signal = getattr(trend_result, "buy_signal", None)
    value = getattr(buy_signal, "value", None) or str(buy_signal or "").strip()
    return localize_operation_advice(value, report_language)


def trend_decision_fallback(trend_result: Optional[TrendAnalysisResult]) -> Optional[str]:
    if trend_result is None:
        return None
    signal_name = getattr(getattr(trend_result, "buy_signal", None), "name", "").lower()
    return {
        "strong_buy": "buy",
        "buy": "buy",
        "hold": "hold",
        "wait": "hold",
        "sell": "sell",
        "strong_sell": "sell",
    }.get(signal_name)


def mark_trend_fallback_source(result: AnalysisResult) -> None:
    if "trend:fallback" in (result.data_sources or ""):
        return
    result.data_sources = (
        f"{result.data_sources},trend:fallback"
        if result.data_sources
        else "trend:fallback"
    )


def summary_fallback_from_result(result: AnalysisResult, report_language: str) -> str:
    trend = (result.trend_prediction or "").strip()
    advice = (result.operation_advice or "").strip()
    if trend and advice:
        if report_language == "en":
            return f"Trend view: {trend}; action advice: {advice}."
        if report_language == "ko":
            return f"추세 결론: {trend}; 대응 전략: {advice}."
        return f"趋势结论：{trend}；操作建议：{advice}。"
    return ""


def backfill_agent_dashboard_fields(
    pipeline,
    result: AnalysisResult,
    trend_result: Optional[TrendAnalysisResult],
    report_language: str,
) -> None:
    if not isinstance(result.dashboard, dict):
        result.dashboard = {}
    dashboard = result.dashboard

    for key in (
        "sentiment_score",
        "trend_prediction",
        "operation_advice",
        "decision_type",
        "confidence_level",
        "analysis_summary",
    ):
        current = dashboard.get(key)
        if key == "sentiment_score":
            if pipeline._is_agent_field_missing(current, scalar=True):
                dashboard[key] = getattr(result, key)
        elif pipeline._is_agent_field_missing(current, scalar=True, expect_text=True):
            dashboard[key] = getattr(result, key)

    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    if pipeline._is_agent_field_missing(core.get("one_sentence"), scalar=True):
        core["one_sentence"] = result.analysis_summary or pipeline._summary_fallback_from_result(
            result,
            report_language,
        ) or (
            "Analysis pending" if report_language == "en"
            else "분석 보완 예정" if report_language == "ko"
            else "分析待补充"
        )

    intelligence = dashboard.get("intelligence")
    if not isinstance(intelligence, dict):
        intelligence = {}
        dashboard["intelligence"] = intelligence
    risk_alerts = intelligence.get("risk_alerts")
    if (
        "risk_alerts" not in intelligence
        or pipeline._is_agent_field_missing(risk_alerts)
        or not isinstance(risk_alerts, list)
    ):
        risk_factors = getattr(trend_result, "risk_factors", None) or []
        intelligence["risk_alerts"] = list(risk_factors)

    if result.decision_type in ("buy", "hold"):
        battle = dashboard.get("battle_plan")
        if not isinstance(battle, dict):
            battle = {}
            dashboard["battle_plan"] = battle
        sniper_points = battle.get("sniper_points")
        if not isinstance(sniper_points, dict):
            sniper_points = {}
            battle["sniper_points"] = sniper_points
        if pipeline._is_agent_field_missing(sniper_points.get("stop_loss"), scalar=True):
            sniper_points["stop_loss"] = pipeline._stop_loss_fallback_from_trend(
                trend_result,
                report_language,
            )


def stop_loss_fallback_from_trend(
    trend_result: Optional[TrendAnalysisResult],
    report_language: str,
) -> Any:


    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    get_placeholder_text = _pipeline.get_placeholder_text

    levels = getattr(trend_result, "support_levels", None) if trend_result else None
    if levels:
        return levels[0]
    return get_placeholder_text(report_language)


def apply_trend_fallback(
    result: AnalysisResult,
    trend_result: Optional[TrendAnalysisResult],
    report_language: str,
) -> None:


    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    StockAnalysisPipeline = _pipeline.StockAnalysisPipeline
    localize_operation_advice = _pipeline.localize_operation_advice

    if trend_result is None:
        result.sentiment_score = 50
        result.operation_advice = localize_operation_advice("观望", report_language)
        return

    score = getattr(trend_result, "signal_score", None)
    try:
        numeric_score = int(score)
    except (TypeError, ValueError):
        numeric_score = 50
    result.sentiment_score = numeric_score if numeric_score > 0 else 50

    trend_label = StockAnalysisPipeline._trend_label_fallback(trend_result, report_language)
    if trend_label:
        result.trend_prediction = trend_label

    buy_signal = getattr(trend_result, "buy_signal", None)
    signal_label = StockAnalysisPipeline._trend_signal_fallback(
        trend_result,
        report_language,
    )
    if signal_label:
        result.operation_advice = signal_label
    else:
        result.operation_advice = localize_operation_advice("观望", report_language)

    from src.agent.protocols import normalize_decision_signal

    signal_name = getattr(buy_signal, "name", "").lower()
    signal_to_decision = {
        "strong_buy": "buy",
        "buy": "buy",
        "hold": "hold",
        "wait": "hold",
        "sell": "sell",
        "strong_sell": "sell",
    }
    result.decision_type = signal_to_decision.get(signal_name, result.decision_type or "hold")
    result.decision_type = normalize_decision_signal(result.decision_type)
    result.data_sources = f"{result.data_sources},trend:fallback" if result.data_sources else "trend:fallback"


def is_placeholder_stock_name(name: str, code: str) -> bool:
    """Return True when the stock name is missing or placeholder-like."""
    if not name:
        return True
    normalized = str(name).strip()
    if not normalized:
        return True
    if normalized == code:
        return True
    if normalized.startswith("股票"):
        return True
    if "Unknown" in normalized:
        return True
    return False


def filter_agent_tools_for_index(self, executor: Any) -> Any:
    """Return an executor whose tool registry excludes index-incompatible tools.

    Maps ``INDEX_SKIP_MODULES`` to the agent tool names that would otherwise
    invoke the skipped bottom-layer providers (chip distribution, fundamental
    aggregation, capital flow). The filtered registry carries the source
    category-timeout map so per-category ceilings survive the subset copy.
    """

    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    INDEX_SKIP_MODULES = _pipeline.INDEX_SKIP_MODULES

    tool_modules = {
        "get_chip_distribution": {"chip_distribution"},
        "get_stock_info": {
            "fundamental",
            "belong_boards",
            "lhb",
            "corporate_events",
        },
        "get_capital_flow": {"capital_flow"},
    }
    index_skip_tool_names = {
        name
        for name, modules in tool_modules.items()
        if INDEX_SKIP_MODULES.intersection(modules)
    }
    registry = getattr(executor, "tool_registry", None)
    if registry is None:
        return executor
    from src.agent.tools.registry import ToolRegistry as _TR
    filtered = _TR(category_timeout_map=registry.category_timeout_map)
    for name in registry.list_names():
        if name in index_skip_tool_names:
            continue
        tool_def = registry.get(name)
        if tool_def is not None:
            filtered.register(tool_def)
    executor.tool_registry = filtered
    return executor


def append_daily_data_source(result, context, analysis_target):
    """Append an index's persisted daily provider without inferring sources."""
    if (
        analysis_target is None
        or analysis_target.asset_type != ParseStatus.INDEX
    ):
        return
    if not isinstance(context, dict):
        return
    today = context.get("today")
    if not isinstance(today, dict):
        return
    source = today.get("data_source")
    if not isinstance(source, str):
        return
    source = source.strip()
    if (
        not source
        or source.casefold() == "unknown"
        or source.casefold().startswith("realtime:")
    ):
        return

    existing = result.data_sources
    if existing is None:
        existing_tokens = []
    elif isinstance(existing, str):
        existing_tokens = [
            item.strip() for item in existing.split(",") if item.strip()
        ]
    else:
        return
    token = f"daily:{source}"
    if token in existing_tokens:
        return
    result.data_sources = ",".join([*existing_tokens, token])

