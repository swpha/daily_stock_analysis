# -*- coding: utf-8 -*-
"""
===================================
通知发送流（从 StockAnalysisPipeline 迁出）
===================================

职责：
1. 批量分析结果的通知编排（send_analysis_notifications，原 _send_notifications）
2. 单股推送（send_single_stock_notification，原 _send_single_stock_notification）

与 orchestrator 的关系：
- 首参 ``pipeline`` 即原 ``self``，仍访问实例状态；
- 部分模块级名字（logger/datetime/共享锁/图片负载工具）经由调用时
  ``import src.core.pipeline as _pipeline`` 读取——保持既有测试对
  ``src.core.pipeline.X`` 的 patch 语义（调用时解析，而非导入时绑定）；
- 其余依赖（AnalysisResult/ReportType/normalize_stock_code 等）为普通导入。
"""

from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.analyzer import AnalysisResult
from data_provider.base import normalize_stock_code
from src.enums import ReportType
from src.notification import NotificationChannel
from src.services.run_diagnostics import record_notification_run


def send_single_stock_notification(
    pipeline,
    result: AnalysisResult,
    report_type: ReportType = ReportType.SIMPLE,
    fallback_code: Optional[str] = None,
) -> None:
    """发送单股通知，供直接单股入口和批量串行推送共用。"""
    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    logger = _pipeline.logger
    datetime = _pipeline.datetime
    threading = _pipeline.threading
    _share_image_payload = _pipeline._share_image_payload
    _supports_explicit_keyword = _pipeline._supports_explicit_keyword
    _SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD = _pipeline._SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD

    stock_code = getattr(result, "code", None) or fallback_code or "unknown"
    notify_lock = getattr(pipeline, "_single_stock_notify_lock", None)
    if notify_lock is None:
        with _SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD:
            notify_lock = getattr(pipeline, "_single_stock_notify_lock", None)
            if notify_lock is None:
                notify_lock = threading.Lock()
                setattr(pipeline, "_single_stock_notify_lock", notify_lock)

    with notify_lock:
        try:
            if report_type == ReportType.FULL:
                report_content = pipeline.notifier.generate_dashboard_report([result])
                logger.info(f"[{stock_code}] 使用完整报告格式")
            elif report_type == ReportType.BRIEF:
                report_content = pipeline.notifier.generate_brief_report([result])
                logger.info(f"[{stock_code}] 使用简洁报告格式")
            else:
                report_content = pipeline.notifier.generate_single_stock_report(result)
                logger.info(f"[{stock_code}] 使用精简报告格式")

            save_report = getattr(pipeline.notifier, "save_report_to_file", None)
            if callable(save_report):
                try:
                    date_str = datetime.now().strftime('%Y%m%d')
                    filename = f"report_{date_str}_{stock_code}.md"
                    filepath = save_report(report_content, filename=filename)
                    logger.info(f"[{stock_code}] 单股报告已保存到本地: {filepath}")
                except Exception as exc:
                    logger.warning(f"[{stock_code}] 单股报告保存失败: {exc}")

            if not pipeline.notifier.is_available():
                notification_run = pipeline._build_notification_run_snapshot(
                    channel="report",
                    status="not_configured",
                    success=False,
                    attempts=0,
                )
                record_notification_run(
                    channel="report",
                    status="not_configured",
                    success=False,
                    attempts=0,
                )
                pipeline._refresh_saved_diagnostic_snapshot(
                    result=result,
                    fallback_code=fallback_code,
                    notification_run=notification_run,
                )
                return

            send_kwargs: Dict[str, Any] = {
                "email_stock_codes": [stock_code],
                "route_type": "report",
                "severity": "info",
                "dedup_key": f"report:single:{stock_code}:{report_type.value}",
                "cooldown_key": f"report:single:{stock_code}:{report_type.value}",
            }
            if _supports_explicit_keyword(pipeline.notifier.send, "structured_payload"):
                send_kwargs["structured_payload"] = _share_image_payload(result)
            sent = pipeline.notifier.send(report_content, **send_kwargs)
            notification_run = pipeline._build_notification_run_snapshot(
                channel="report",
                status="success" if sent else "failed",
                success=sent,
            )
            record_notification_run(
                channel="report",
                status="success" if sent else "failed",
                success=sent,
            )
            pipeline._refresh_saved_diagnostic_snapshot(
                result=result,
                fallback_code=fallback_code,
                notification_run=notification_run,
            )
            if sent:
                logger.info(f"[{stock_code}] 单股推送成功")
            else:
                logger.warning(f"[{stock_code}] 单股推送失败")
        except Exception as e:
            notification_run = pipeline._build_notification_run_snapshot(
                channel="report",
                status="failed",
                success=False,
                error_message=e,
            )
            record_notification_run(
                channel="report",
                status="failed",
                success=False,
                error_message=e,
            )
            pipeline._refresh_saved_diagnostic_snapshot(
                result=result,
                fallback_code=fallback_code,
                notification_run=notification_run,
            )
            logger.error(f"[{stock_code}] 单股推送异常: {e}")


def send_analysis_notifications(
    pipeline,
    results: List[AnalysisResult],
    report_type: ReportType = ReportType.SIMPLE,
    skip_push: bool = False,
) -> None:
    """
    发送分析结果通知
    
    生成决策仪表盘格式的报告
    
    Args:
        results: 分析结果列表
        skip_push: 是否跳过推送（仅保存到本地，用于单股推送模式）
    """
    import src.core.pipeline as _pipeline

    # 调用时解析：保持对 src.core.pipeline 模块属性的测试 patch 语义
    logger = _pipeline.logger
    datetime = _pipeline.datetime
    get_config = _pipeline.get_config
    strip_hidden_markdown_metadata = _pipeline.strip_hidden_markdown_metadata
    _share_image_payload = _pipeline._share_image_payload

    noise_decision = None
    noise_finalized = False
    try:
        logger.info("生成决策仪表盘日报...")
        report = pipeline._generate_aggregate_report(results, report_type)
        
        # 跳过推送（单股推送模式 / 合并模式：报告已由 _save_local_report 保存）
        if skip_push:
            notification_run = pipeline._build_notification_run_snapshot(
                channel="report",
                status="skipped",
                success=False,
                attempts=0,
            )
            record_notification_run(
                channel="report",
                status="skipped",
                success=False,
                attempts=0,
            )
            pipeline._refresh_saved_diagnostic_snapshot(
                results=results,
                notification_run=notification_run,
            )
            return
        
        # 推送通知
        if pipeline.notifier.is_available():
            channels = pipeline.notifier.get_available_channels()
            channels = pipeline.notifier.get_channels_for_route("report", channels=channels)

            def _send_channel_safely(
                channel_label: str,
                send_func: Callable[[], bool],
            ) -> tuple[bool, Optional[Exception]]:
                try:
                    return bool(send_func()), None
                except Exception as e:
                    logger.exception(
                        "通知渠道 %s 推送异常，继续尝试其他渠道: %s",
                        channel_label,
                        e,
                    )
                    return False, e

            def _record_channel_result(
                channel_label: str,
                success: bool,
                error_message: Optional[Exception] = None,
                target_results: Optional[List[AnalysisResult]] = None,
            ) -> None:
                notification_run = pipeline._build_notification_run_snapshot(
                    channel=channel_label,
                    status="success" if success else "failed",
                    success=success,
                    error_message=error_message,
                )
                record_notification_run(
                    channel=channel_label,
                    status="success" if success else "failed",
                    success=success,
                    error_message=error_message,
                )
                pipeline._refresh_saved_diagnostic_snapshot(
                    results=results if target_results is None else target_results,
                    notification_run=notification_run,
                )

            send_context = pipeline.notifier.send_to_context(report)
            if send_context:
                _record_channel_result("__context__", True)

            should_broadcast_static = True
            should_broadcast_static_func = getattr(
                pipeline.notifier,
                "should_broadcast_static_channels",
                None,
            )
            if callable(should_broadcast_static_func):
                should_broadcast_static = bool(should_broadcast_static_func())
            if not should_broadcast_static:
                if not send_context:
                    _record_channel_result("__context__", False)
                if send_context:
                    logger.info("决策仪表盘推送成功")
                else:
                    logger.warning("决策仪表盘推送失败")
                logger.info("交互式消息上下文回复模式：已跳过静态通知渠道")
                return

            if channels and hasattr(pipeline.notifier, "evaluate_noise_control"):
                report_type_key = report_type.value if isinstance(report_type, ReportType) else str(report_type)
                codes_key = ",".join(
                    sorted(str(getattr(result, "code", "") or "") for result in results)
                )
                noise_key = f"report:aggregate:{report_type_key}:{codes_key}"
                noise_decision = pipeline.notifier.evaluate_noise_control(
                    report,
                    route_type="report",
                    severity="info",
                    dedup_key=noise_key,
                    cooldown_key=noise_key,
                )
                if not noise_decision.should_send:
                    notification_run = pipeline._build_notification_run_snapshot(
                        channel="report",
                        status="skipped",
                        success=False,
                        attempts=0,
                    )
                    record_notification_run(
                        channel="report",
                        status="skipped",
                        success=False,
                        attempts=0,
                    )
                    pipeline._refresh_saved_diagnostic_snapshot(
                        results=results,
                        notification_run=notification_run,
                    )
                    logger.info(noise_decision.message)
                    return

            # Issue #455: Markdown 转图片（与 notification.send 逻辑一致）
            from src.md2img import markdown_to_image

            channels_needing_image = {
                ch for ch in channels
                if ch.value in pipeline.notifier._markdown_to_image_channels
                and ch not in {NotificationChannel.NTFY, NotificationChannel.GOTIFY}
            }
            non_wechat_channels_needing_image = {
                ch for ch in channels_needing_image if ch != NotificationChannel.WECHAT
            }
            single_share_payload = (
                _share_image_payload(results[0]) if len(results) == 1 else None
            )

            def _get_md2img_hint() -> str:
                try:
                    engine = getattr(get_config(), "md2img_engine", "wkhtmltoimage")
                except Exception:
                    engine = "wkhtmltoimage"
                return (
                    "npm i -g markdown-to-file" if engine == "markdown-to-file"
                    else "wkhtmltopdf (apt install wkhtmltopdf / brew install wkhtmltopdf)"
                )

            image_bytes = None
            if non_wechat_channels_needing_image:
                image_kwargs: Dict[str, Any] = {
                    "max_chars": pipeline.notifier._markdown_to_image_max_chars,
                }
                if single_share_payload is not None:
                    image_kwargs["structured_payload"] = single_share_payload
                image_bytes = markdown_to_image(report, **image_kwargs)
                if image_bytes:
                    logger.info(
                        "Markdown 已转换为图片，将向 %s 发送图片",
                        [ch.value for ch in non_wechat_channels_needing_image],
                    )
                else:
                    logger.warning(
                        "Markdown 转图片失败，将回退为文本发送。请检查 MARKDOWN_TO_IMAGE_CHANNELS 配置并安装 %s",
                        _get_md2img_hint(),
                    )

            # 企业微信：只发精简版（平台限制）
            wechat_success = False
            if NotificationChannel.WECHAT in channels:
                def _send_wechat_report() -> bool:
                    if report_type == ReportType.BRIEF:
                        dashboard_content = pipeline.notifier.generate_brief_report(results)
                    else:
                        dashboard_content = pipeline.notifier.generate_wechat_dashboard(results)
                    logger.info(f"企业微信仪表盘长度: {len(dashboard_content)} 字符")
                    logger.debug(f"企业微信推送内容:\n{dashboard_content}")
                    wechat_image_bytes = None
                    if NotificationChannel.WECHAT in channels_needing_image:
                        wechat_image_kwargs: Dict[str, Any] = {
                            "max_chars": pipeline.notifier._markdown_to_image_max_chars,
                        }
                        if single_share_payload is not None:
                            wechat_image_kwargs["structured_payload"] = single_share_payload
                        wechat_image_bytes = markdown_to_image(
                            dashboard_content,
                            **wechat_image_kwargs,
                        )
                        if wechat_image_bytes is None:
                            logger.warning(
                                "企业微信 Markdown 转图片失败，将回退为文本发送。请检查 MARKDOWN_TO_IMAGE_CHANNELS 配置并安装 %s",
                                _get_md2img_hint(),
                            )
                    use_image = pipeline.notifier._should_use_image_for_channel(
                        NotificationChannel.WECHAT, wechat_image_bytes
                    )
                    if use_image:
                        return pipeline.notifier._send_wechat_image(wechat_image_bytes)
                    return pipeline.notifier.send_to_wechat(dashboard_content)

                wechat_success, wechat_error = _send_channel_safely(
                    NotificationChannel.WECHAT.value,
                    _send_wechat_report,
                )
                _record_channel_result(
                    NotificationChannel.WECHAT.value,
                    wechat_success,
                    wechat_error,
                )

            # 其他渠道：发完整报告（避免自定义 Webhook 被 wechat 截断逻辑污染）
            non_wechat_success = False
            stock_email_groups = getattr(pipeline.config, 'stock_email_groups', []) or []
            for channel in channels:
                if channel == NotificationChannel.WECHAT:
                    continue
                if channel == NotificationChannel.FEISHU:
                    def _send_feishu_report() -> bool:
                        if getattr(pipeline.notifier, "_feishu_send_as_file", False):
                            date_str = datetime.now().strftime('%Y%m%d')
                            filepath = pipeline.notifier.save_report_to_file(
                                strip_hidden_markdown_metadata(report).strip(),
                                filename=f"dashboard_{date_str}.md",
                            )
                            return pipeline.notifier.send_feishu_file(filepath)
                        return pipeline.notifier.send_to_feishu(report)

                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        _send_feishu_report,
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.TELEGRAM:
                    def _send_telegram_report() -> bool:
                        use_image = pipeline.notifier._should_use_image_for_channel(
                            channel, image_bytes
                        )
                        if use_image:
                            return pipeline.notifier._send_telegram_photo(image_bytes)
                        return pipeline.notifier.send_to_telegram(report)

                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        _send_telegram_report,
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.EMAIL:
                    if stock_email_groups:
                        code_to_emails: Dict[str, Optional[List[str]]] = {}
                        for r in results:
                            if r.code not in code_to_emails:
                                canonical = normalize_stock_code(r.code)
                                emails = []
                                for stocks, emails_list in stock_email_groups:
                                    if canonical in stocks:
                                        emails.extend(emails_list)
                                code_to_emails[r.code] = list(dict.fromkeys(emails)) if emails else None
                        emails_to_results: Dict[Optional[Tuple], List] = defaultdict(list)
                        for r in results:
                            recs = code_to_emails.get(r.code)
                            key = tuple(recs) if recs else None
                            emails_to_results[key].append(r)
                        for key, group_results in emails_to_results.items():
                            receivers = list(key) if key is not None else None

                            def _send_email_group(
                                group_results=group_results,
                                receivers=receivers,
                            ) -> bool:
                                grp_report = pipeline._generate_aggregate_report(group_results, report_type)
                                grp_image_bytes = None
                                if channel.value in pipeline.notifier._markdown_to_image_channels:
                                    group_payload = (
                                        _share_image_payload(group_results[0])
                                        if len(group_results) == 1
                                        else None
                                    )
                                    group_image_kwargs: Dict[str, Any] = {
                                        "max_chars": pipeline.notifier._markdown_to_image_max_chars,
                                    }
                                    if group_payload is not None:
                                        group_image_kwargs["structured_payload"] = group_payload
                                    grp_image_bytes = markdown_to_image(
                                        grp_report,
                                        **group_image_kwargs,
                                    )
                                use_image = pipeline.notifier._should_use_image_for_channel(
                                    channel, grp_image_bytes
                                )
                                if use_image:
                                    return pipeline.notifier._send_email_with_inline_image(
                                        grp_image_bytes, receivers=receivers
                                    )
                                return pipeline.notifier.send_to_email(
                                    strip_hidden_markdown_metadata(grp_report).strip(),
                                    receivers=receivers,
                                )

                            email_label = (
                                f"{channel.value}:{','.join(receivers)}"
                                if receivers else f"{channel.value}:default"
                            )
                            channel_success, channel_error = _send_channel_safely(
                                email_label,
                                _send_email_group,
                            )
                            non_wechat_success = channel_success or non_wechat_success
                            _record_channel_result(
                                email_label,
                                channel_success,
                                channel_error,
                                target_results=group_results,
                            )
                    else:
                        def _send_email_report() -> bool:
                            use_image = pipeline.notifier._should_use_image_for_channel(
                                channel, image_bytes
                            )
                            if use_image:
                                return pipeline.notifier._send_email_with_inline_image(image_bytes)
                            return pipeline.notifier.send_to_email(
                                strip_hidden_markdown_metadata(report).strip()
                            )

                        channel_success, channel_error = _send_channel_safely(
                            channel.value,
                            _send_email_report,
                        )
                        non_wechat_success = channel_success or non_wechat_success
                        _record_channel_result(
                            channel.value,
                            channel_success,
                            channel_error,
                        )
                elif channel == NotificationChannel.CUSTOM:
                    def _send_custom_report() -> bool:
                        use_image = pipeline.notifier._should_use_image_for_channel(
                            channel, image_bytes
                        )
                        if use_image:
                            return pipeline.notifier._send_custom_webhook_image(
                                image_bytes, fallback_content=report
                            )
                        return pipeline.notifier.send_to_custom(report)

                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        _send_custom_report,
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.PUSHPLUS:
                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        lambda: pipeline.notifier.send_to_pushplus(report),
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.SERVERCHAN3:
                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        lambda: pipeline.notifier.send_to_serverchan3(report),
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.DISCORD:
                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        lambda: pipeline.notifier.send_to_discord(report),
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.PUSHOVER:
                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        lambda: pipeline.notifier.send_to_pushover(report),
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.NTFY:
                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        lambda: pipeline.notifier.send_to_ntfy(report),
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.GOTIFY:
                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        lambda: pipeline.notifier.send_to_gotify(report),
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.ASTRBOT:
                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        lambda: pipeline.notifier.send_to_astrbot(report),
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                elif channel == NotificationChannel.SLACK:
                    def _send_slack_report() -> bool:
                        use_image = pipeline.notifier._should_use_image_for_channel(
                            channel, image_bytes
                        )
                        if use_image and pipeline.notifier._slack_bot_token and pipeline.notifier._slack_channel_id:
                            return pipeline.notifier._send_slack_image(
                                image_bytes, fallback_content=report
                            )
                        return pipeline.notifier.send_to_slack(report)

                    channel_success, channel_error = _send_channel_safely(
                        channel.value,
                        _send_slack_report,
                    )
                    non_wechat_success = channel_success or non_wechat_success
                    _record_channel_result(
                        channel.value,
                        channel_success,
                        channel_error,
                    )
                else:
                    logger.warning(f"未知通知渠道: {channel}")

            has_targeted_channels = bool(channels)
            success = wechat_success or non_wechat_success or send_context
            if (
                (wechat_success or non_wechat_success)
                and noise_decision is not None
                and hasattr(pipeline.notifier, "record_noise_control")
            ):
                pipeline.notifier.record_noise_control(noise_decision)
                noise_finalized = True
            elif (
                noise_decision is not None
                and hasattr(pipeline.notifier, "release_noise_control")
            ):
                pipeline.notifier.release_noise_control(noise_decision)
                noise_finalized = True
            if success:
                logger.info("决策仪表盘推送成功")
            else:
                logger.warning("决策仪表盘推送失败")
            if not has_targeted_channels and not send_context:
                channel_label = ",".join(channel.value for channel in channels) or "report"
                notification_run = pipeline._build_notification_run_snapshot(
                    channel=channel_label,
                    status="success" if success else "failed",
                    success=success,
                )
                record_notification_run(
                    channel=channel_label,
                    status="success" if success else "failed",
                    success=success,
                )
                pipeline._refresh_saved_diagnostic_snapshot(
                    results=results,
                    notification_run=notification_run,
                )
        else:
            notification_run = pipeline._build_notification_run_snapshot(
                channel="report",
                status="not_configured",
                success=False,
                attempts=0,
            )
            record_notification_run(
                channel="report",
                status="not_configured",
                success=False,
                attempts=0,
            )
            pipeline._refresh_saved_diagnostic_snapshot(
                results=results,
                notification_run=notification_run,
            )
            logger.info("通知渠道未配置，跳过推送")
            
    except Exception as e:
        notification_run = pipeline._build_notification_run_snapshot(
            channel="report",
            status="failed",
            success=False,
            error_message=e,
        )
        record_notification_run(
            channel="report",
            status="failed",
            success=False,
            error_message=e,
        )
        pipeline._refresh_saved_diagnostic_snapshot(
            results=results,
            notification_run=notification_run,
        )
        if (
            noise_decision is not None
            and not noise_finalized
            and hasattr(pipeline.notifier, "release_noise_control")
        ):
            pipeline.notifier.release_noise_control(noise_decision)
        import traceback
        logger.error(f"发送通知失败: {e}\n{traceback.format_exc()}")
