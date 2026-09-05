#!/usr/bin/env python3
"""每日股票分析 workflow (00-daily-analysis.yml) 的执行脚本。

从 workflow 内联 bash 迁出，职责：
1. 解析自选股配置（STOCK_LIST_CONFIG > STOCK_LIST > 最小默认值）并注入环境；
2. 落盘 LITELLM_CONFIG_YAML（如有配置）；
3. 打印运行模式与配置检查横幅；
4. 按模式分发调用 main.py 并透传退出码。

MODE / FORCE_RUN / STOCK_CODES / DRY_RUN 由 workflow 的 env 映射注入（不在 run 块内
使用 ${{ }} 表达式，避免 GitHub Actions 脚本注入面）。

- STOCK_CODES 非空时为"触发式日报"：仅分析指定标的、不做大盘复盘（KDJ 预警触发时使用）；
- DRY_RUN=true 为测试模式：仅拉取数据校验管线，不调用 AI、不发通知，节约 token。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_stock_list() -> str:
    """自选股解析：
    1. Repository/Environment secrets 或 Repository variables（STOCK_LIST_CONFIG）
    2. 同名 Environment variables 注入的 runner 环境变量（STOCK_LIST）
    3. 最小默认值
    """
    stock_list_config = os.environ.get("STOCK_LIST_CONFIG", "").strip()
    if stock_list_config:
        os.environ["STOCK_LIST"] = stock_list_config
    elif not os.environ.get("STOCK_LIST"):
        os.environ["STOCK_LIST"] = "600519"
    return os.environ["STOCK_LIST"]


def _write_litellm_config() -> None:
    config_path = os.environ.get("LITELLM_CONFIG", "").strip()
    config_yaml = os.environ.get("LITELLM_CONFIG_YAML", "").strip()
    if not (config_yaml and config_path):
        return
    print("📝 使用 GitHub Actions Secrets/Variables 中的 LITELLM_CONFIG_YAML 配置")
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config_path).write_text(config_yaml + "\n", encoding="utf-8")
    print(f"✅ LITELLM 配置文件已写入: {config_path}")


def _has(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _check(label: str, *names: str) -> None:
    print(f"  {label}: {'✅ 已配置' if any(_has(n) for n in names) else '⚪ 未配置'}")


def _print_config_banner() -> None:
    print("==========================================")
    print("📋 配置检查")
    print("==========================================")
    print("【AI 配置】")
    litellm_ready = any(
        _has(n) for n in ("LITELLM_CONFIG", "LITELLM_API_KEY", "LITELLM_MODEL", "ANSPIRE_API_KEYS")
    )
    print(f"  LiteLLM: {'✅ 已配置' if litellm_ready else '❌ 未配置'}")
    _check("Gemini API Key", "GEMINI_API_KEY")
    _check("DeepSeek Key", "DEEPSEEK_API_KEY")
    _check("Anspire Key", "ANSPIRE_API_KEYS")
    _check("AIHubMix Key", "AIHUBMIX_KEY")
    _check("OpenAI API Key", "OPENAI_API_KEY")
    _check("Anthropic Key", "ANTHROPIC_API_KEY")
    print()
    print("【数据源】")
    _check("Tushare Token", "TUSHARE_TOKEN")
    _check("TickFlow", "TICKFLOW_API_KEY")
    if _has("LONGBRIDGE_APP_KEY") and _has("LONGBRIDGE_APP_SECRET") and _has("LONGBRIDGE_ACCESS_TOKEN"):
        print("  Longbridge: ✅ 已配置（Legacy API Key）")
    elif _has("LONGBRIDGE_OAUTH_CLIENT_ID") or (_has("LONGBRIDGE_APP_KEY") and not _has("LONGBRIDGE_ACCESS_TOKEN")):
        state = "✅ 已配置（OAuth token cache）" if _has("LONGBRIDGE_OAUTH_TOKEN_CACHE_B64") else "🟡 已配置 OAuth client，缺少 token cache"
        print(f"  Longbridge: {state}")
    else:
        print("  Longbridge: ⚪ 未配置（仅 YFinance 等，无长桥调用）")
    print()
    print("【搜索引擎】")
    _check("Bocha API Keys", "BOCHA_API_KEYS")
    _check("Tavily API Keys", "TAVILY_API_KEYS")
    _check("SerpAPI Keys", "SERPAPI_API_KEYS")
    _check("MiniMax API Keys", "MINIMAX_API_KEYS")
    _check("Brave API Keys", "BRAVE_API_KEYS")
    _check("SearXNG Base URLs", "SEARXNG_BASE_URLS")
    public_instances = os.environ.get("SEARXNG_PUBLIC_INSTANCES_ENABLED", "").strip().lower()
    if public_instances in ("0", "false", "no", "off"):
        print("  SearXNG Public Instances: ❌ 已禁用")
    elif not public_instances:
        print("  SearXNG Public Instances: ⚪ 默认关闭（公共实例普遍限流，如需启用请显式设为 true）")
    else:
        print("  SearXNG Public Instances: ✅ 已启用")
    print()
    print("【通知渠道】")
    _check("PushPlus", "PUSHPLUS_TOKEN")
    _check("ntfy", "NTFY_URL")
    gotify_ready = _has("GOTIFY_URL") and _has("GOTIFY_TOKEN")
    print(f"  Gotify: {'✅ 已配置' if gotify_ready else '⚪ 未配置'}")
    _check("企业微信", "WECHAT_WEBHOOK_URL")
    _check("钉钉", "DINGTALK_WEBHOOK_URL")
    feishu_ready = _has("FEISHU_WEBHOOK_URL") or (
        _has("FEISHU_APP_ID") and _has("FEISHU_APP_SECRET") and _has("FEISHU_CHAT_ID")
    )
    print(f"  飞书: {'✅ 已配置' if feishu_ready else '⚪ 未配置'}")
    _check("Telegram", "TELEGRAM_BOT_TOKEN")
    _check("Discord", "DISCORD_WEBHOOK_URL")
    _check("AstrBot", "ASTRBOT_URL")
    slack_ready = _has("SLACK_WEBHOOK_URL") or (_has("SLACK_BOT_TOKEN") and _has("SLACK_CHANNEL_ID"))
    print(f"  Slack: {'✅ 已配置' if slack_ready else '⚪ 未配置'}")
    print("==========================================")


def _build_main_args(mode: str, force_run: bool, stock_codes: str, dry_run: bool) -> list[str]:
    args: list[str] = []
    if force_run or dry_run:
        # 测试模式一并跳过交易日检查，保证任何一天都能演练流程
        args.append("--force-run")
    if stock_codes:
        # 触发式日报：只分析指定标的，且不做大盘复盘
        args += ["--stocks", stock_codes, "--no-market-review"]
    elif mode == "market-only":
        args.append("--market-review")
    elif mode == "stocks-only":
        args.append("--no-market-review")
    if dry_run:
        # 测试模式：仅拉取数据与校验管线，不调用 AI、不发通知，节约 token
        args.append("--dry-run")
    return args


def main() -> int:
    os.chdir(PROJECT_ROOT)

    mode = os.environ.get("ANALYSIS_MODE", "full").strip() or "full"
    force_run = os.environ.get("FORCE_RUN", "").strip().lower() == "true"
    stock_codes = os.environ.get("STOCK_CODES", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() == "true"

    stock_list = _resolve_stock_list()
    _write_litellm_config()

    print("==========================================")
    print("🚀 A股自选股智能分析系统")
    print("==========================================")
    mode_label = f"{mode}（指定标的: {stock_codes}）" if stock_codes else mode
    print(f"🎯 运行模式: {mode_label}" + ("（测试模式，仅取数不调用 AI）" if dry_run else ""))
    print(f"📊 自选股: {stock_list}")
    print(f"📝 报告类型: {os.environ.get('REPORT_TYPE', '')}")
    print()
    _print_config_banner()
    print()

    if force_run:
        print("⚡ 已启用强制运行模式（跳过交易日检查）")

    command = [sys.executable, "main.py", *_build_main_args(mode, force_run, stock_codes, dry_run)]
    result = subprocess.run(command, env=os.environ.copy())
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
