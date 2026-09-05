# -*- coding: utf-8 -*-
"""通知渠道共用的 HTTP 重试语义。

从 Discord sender 的重试循环（429 Retry-After / 5xx / 指数退避）提炼，
供各 sender 复用，避免每个渠道各自实现一遍退避逻辑。
"""

import logging
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_MIN = 500


def get_retry_after_seconds(response: requests.Response, attempt: int) -> float:
    """Discord 风格 Retry-After：优先 JSON body，其次响应头，兜底指数退避。"""
    try:
        retry_after = response.json().get("retry_after")
        if retry_after is not None:
            return max(0.0, float(retry_after))
    except (AttributeError, TypeError, ValueError):
        pass

    try:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            return max(0.0, float(retry_after))
    except AttributeError:
        pass

    return float(2 ** attempt)


def send_with_retry(
    attempt: Callable[[], requests.Response],
    *,
    label: str,
    is_success: Callable[[requests.Response], bool],
    max_retries: int = 3,
    retry_after: Optional[Callable[[requests.Response, int], float]] = None,
    backoff_base_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Optional[logging.Logger] = None,
) -> bool:
    """执行一次带重试的发送。

    重试策略（与原 Discord sender 行为一致）：
    - 网络异常 / 5xx：指数退避（backoff_base ** attempt）后重试；
    - 429：仅在提供 retry_after 回调时按其给出的等待时长限流重试；
    - 其它 4xx：业务性失败，立即返回 False，不重试；
    - is_success(response) 为真即成功（可同时校验 HTTP 状态与业务码）。

    Args:
        attempt: 执行单次 HTTP 请求的零参函数（抛 requests 异常视为网络失败）。
        label: 日志前缀（渠道名）。
        is_success: 应用层成功判定。
        max_retries: 最大尝试次数（含首次）。
        retry_after: 可选；429 限流时给出等待秒数。
        backoff_base_seconds: 指数退避底数。
        sleep: 等待实现（测试可注入 no-op）。
        log: 日志器。
    """
    log = log or logger
    for attempt_no in range(1, max_retries + 1):
        try:
            response = attempt()
        except requests.exceptions.RequestException as exc:
            if attempt_no < max_retries:
                delay = backoff_base_seconds ** attempt_no
                log.warning(
                    "%s 请求异常（%d/%d）：%s，%s 秒后重试",
                    label, attempt_no, max_retries, exc, delay,
                )
                sleep(delay)
                continue
            log.error("%s 请求重试后仍失败: %s", label, exc)
            return False

        if is_success(response):
            log.info("%s 消息发送成功", label)
            return True

        if response.status_code == 429 and attempt_no < max_retries and retry_after is not None:
            wait_seconds = retry_after(response, attempt_no)
            log.warning(
                "%s 触发限流，%s 秒后重试（%d/%d）",
                label, wait_seconds, attempt_no, max_retries,
            )
            sleep(wait_seconds)
            continue

        if response.status_code >= RETRYABLE_STATUS_MIN and attempt_no < max_retries:
            delay = backoff_base_seconds ** attempt_no
            log.warning(
                "%s 服务端错误 HTTP %s（%d/%d），%s 秒后重试",
                label, response.status_code, attempt_no, max_retries, delay,
            )
            sleep(delay)
            continue

        log.error("%s 发送失败: HTTP %s", label, response.status_code)
        try:
            log.debug("%s 响应内容: %s", label, response.text[:200])
        except Exception:  # noqa: BLE001 - 读取响应体失败不影响结果
            pass
        return False
    return False
