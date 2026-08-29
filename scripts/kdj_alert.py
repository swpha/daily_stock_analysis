# -*- coding: utf-8 -*-
"""KDJ 指标预警（GitHub Actions 定时任务，独立于主分析流程）。

监控 KDJ_ALERT_CONFIG 配置的标的，日K/周K KDJ(9,3,3) 的 J 值低于阈值时通过邮件
发送买入信号。指标口径与 scripts/indicators.py 一致：通达信 SMA[N,1] 平滑，
不复权；周K由日线聚合（含本周进行中的实时数据）。

配置（环境变量）:
    KDJ_ALERT_CONFIG             监控列表，code:threshold[:freqs] 逗号分隔
                                 freqs: d=日K, w=周K, dw=两者；缺省为 w（周K）
                                 如 "159659:10:dw,600519:8"
    KDJ_ALERT_MIN_INTERVAL_DAYS  同一标的"持续低位"重复提醒的最小间隔天数（默认 2）
    KDJ_ALERT_FORCE              true 时跳过去重与数据时效检查，强制发送（用于测试）
    EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECEIVERS / EMAIL_SMTP_HOST

通知去重:
    首次跌破阈值 → 立即通知；之后仍在阈值下方 → 每 MIN_INTERVAL_DAYS 天提醒一次。
    状态按 "code:freq" 记录在 .github/state/kdj_alert_state.json，由工作流提交回仓库。

数据源:
    东财日线接口（akshare，免 token）> 新浪日线接口（兜底），周K由日线聚合。
    ETF 用 fund_etf_* 接口，个股用 stock_zh_a_* 接口，按代码前缀自动路由。
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / ".github" / "state" / "kdj_alert_state.json"
TZ_BJS = ZoneInfo("Asia/Shanghai")

FREQ_LABELS = {"d": "日K", "w": "周K"}

SMTP_HOSTS = {
    "qq.com": "smtp.qq.com",
    "foxmail.com": "smtp.qq.com",
    "163.com": "smtp.163.com",
    "126.com": "smtp.126.com",
    "sina.com": "smtp.sina.com",
    "gmail.com": "smtp.gmail.com",
    "outlook.com": "smtp.office365.com",
    "hotmail.com": "smtp.office365.com",
}


def log(msg: str) -> None:
    print(f"{datetime.now(TZ_BJS).strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)


def parse_config(raw: str) -> list[tuple[str, float, str]]:
    """解析 "159659:10:dw,600519:8" 为 [(code, threshold, freqs), ...]。"""
    items = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        fields = part.split(":")
        code = fields[0].strip()
        if not (code.isdigit() and len(code) == 6):
            raise SystemExit(f"KDJ_ALERT_CONFIG 中的代码不合法: {part!r}")
        threshold = float(fields[1].strip()) if len(fields) > 1 and fields[1].strip() else 10.0
        freqs = "".join(sorted(set(fields[2].strip().lower()))) if len(fields) > 2 else "w"
        if not freqs or set(freqs) - {"d", "w"}:
            raise SystemExit(f"KDJ_ALERT_CONFIG 周期不合法（只支持 d/w）: {part!r}")
        items.append((code, threshold, freqs))
    if not items:
        raise SystemExit("KDJ_ALERT_CONFIG 为空，未配置任何监控标的")
    return items


def is_etf(code: str) -> bool:
    return code[0] == "5" or code[:2] in ("15", "16")


def sina_symbol(code: str) -> str:
    return ("sh" if code[0] in "569" else "sz") + code


def aggregate_weekly(daily: list[dict]) -> list[dict]:
    """日线聚合成自然周K（本周为进行中的实时周K）。"""
    buckets: dict[str, dict] = {}
    for row in daily:
        d = datetime.strptime(row["date"], "%Y-%m-%d")
        key = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        if key not in buckets:
            buckets[key] = dict(row)
            continue
        b = buckets[key]
        b["high"] = max(b["high"], row["high"])
        b["low"] = min(b["low"], row["low"])
        b["close"] = row["close"]
        b["date"] = row["date"]
    return sorted(buckets.values(), key=lambda r: r["date"])


def fetch_daily(code: str) -> tuple[list[dict], str]:
    """返回 (日线列表, 数据源名)。东财主源，新浪兜底。"""
    import akshare as ak

    if is_etf(code):
        fetchers = (
            (lambda: ak.fund_etf_hist_em(symbol=code, period="daily", adjust=""), "东财"),
            (lambda: ak.fund_etf_hist_sina(symbol=sina_symbol(code)), "新浪"),
        )
    else:
        fetchers = (
            (lambda: ak.stock_zh_a_hist(symbol=code, period="daily", adjust=""), "东财"),
            (lambda: ak.stock_zh_a_daily(symbol=sina_symbol(code), adjust=""), "新浪"),
        )

    errors = []
    for fetcher, name in fetchers:
        try:
            df = fetcher()
            df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                    "最高": "high", "最低": "low"})
            rows = df[["date", "open", "high", "low", "close"]].to_dict("records")
            for r in rows:
                r["open"], r["high"] = float(r["open"]), float(r["high"])
                r["low"], r["close"] = float(r["low"]), float(r["close"])
                r["date"] = str(r["date"])[:10]
            if len(rows) > 3650:  # 预热窗口 10 年足够 KDJ 收敛
                rows = rows[-3650:]
            return rows, f"{name}日线"
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            log(f"  {name}日线获取失败({code}): {exc}")
    raise RuntimeError("所有数据源获取失败: " + "; ".join(errors))


def add_kdj(rows: list[dict]) -> list[dict]:
    """KDJ(9,3,3)，SMA[N,1] 平滑（等价 ewm(alpha=1/3)），与通达信一致。"""
    lows = [r["low"] for r in rows]
    highs = [r["high"] for r in rows]
    k = d = 50.0
    for i, r in enumerate(rows):
        llv = min(lows[max(0, i - 8):i + 1])
        hhv = max(highs[max(0, i - 8):i + 1])
        rsv = 50.0 if hhv == llv else (r["close"] - llv) / (hhv - llv) * 100
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        r["K"], r["D"], r["J"] = k, d, 3 * k - 2 * d
    return rows


def lookup_names(codes: list[str]) -> dict[str, str]:
    """尽力查询标的名称（失败不影响预警）。"""
    import akshare as ak

    names: dict[str, str] = {}
    etf_codes = [c for c in codes if is_etf(c)]
    stock_codes = [c for c in codes if not is_etf(c)]
    for codes_part, fetcher, key in (
        (etf_codes, ak.fund_etf_spot_em, "代码"),
        (stock_codes, ak.stock_zh_a_spot_em, "代码"),
    ):
        if not codes_part:
            continue
        try:
            df = fetcher()
            for c in codes_part:
                hit = df[df[key] == c]
                if len(hit):
                    names[c] = str(hit["名称"].iloc[0])
        except Exception as exc:
            log(f"  名称查询失败: {exc}")
    return names


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"notified": {}}


def send_email(subject: str, body: str) -> None:
    sender = os.environ["EMAIL_SENDER"].strip()
    password = os.environ["EMAIL_PASSWORD"].strip()
    receivers = [r.strip() for r in os.environ.get("EMAIL_RECEIVERS", "").split(",") if r.strip()] or [sender]
    host = os.environ.get("EMAIL_SMTP_HOST", "").strip() or SMTP_HOSTS.get(sender.split("@")[-1].lower())
    if not host:
        raise RuntimeError(f"无法识别 {sender} 的 SMTP 服务器，请设置 EMAIL_SMTP_HOST")
    port = 587 if host == "smtp.office365.com" else 465

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)

    if port == 587:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, receivers, msg.as_string())
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(sender, password)
            s.sendmail(sender, receivers, msg.as_string())
    log(f"邮件已发送: {subject} -> {receivers}")


def main() -> int:
    config = parse_config(os.environ.get("KDJ_ALERT_CONFIG", "159659:10:dw"))
    min_interval = int(os.environ.get("KDJ_ALERT_MIN_INTERVAL_DAYS", "2"))
    force = os.environ.get("KDJ_ALERT_FORCE", "").strip().lower() in ("1", "true", "yes")
    today = datetime.now(TZ_BJS).date()
    state = load_state()
    notified: dict = state.setdefault("notified", {})

    log(f"监控列表: {config} | 间隔: {min_interval}天 | 强制: {force}")
    names = lookup_names(sorted({c for c, _, _ in config}))

    signals, statuses, failures = [], [], []
    for code, threshold, freqs in config:
        label = f"{names.get(code, '?')}({code})"
        try:
            for attempt in (1, 2):
                try:
                    daily, source = fetch_daily(code)
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise
                    log(f"  {label} 取数失败，重试: {exc}")
                    time.sleep(3)

            for freq in freqs:
                flabel = FREQ_LABELS[freq]
                state_key = f"{code}:{freq}"
                try:
                    bars = daily if freq == "d" else aggregate_weekly(daily)
                    if len(bars) < 10:
                        log(f"  {label} {flabel}数据不足({len(bars)}条)，跳过")
                        continue
                    add_kdj(bars)
                    cur, prev = bars[-1], bars[-2]
                    cur_j, prev_j = cur["J"], prev["J"]
                    bar_age = (today - date.fromisoformat(str(cur["date"])[:10])).days
                    log(f"  {label} [{source}] {flabel}最新 {cur['date']} 收盘={cur['close']} "
                        f"K={cur['K']:.2f} D={cur['D']:.2f} J={cur_j:.2f} "
                        f"(阈值 {threshold}, 前一根J={prev_j:.2f})")
                    statuses.append({"code": code, "name": names.get(code, code), "threshold": threshold,
                                     "freq": flabel, "j": cur_j, "bar_date": str(cur["date"])[:10]})

                    if not force and bar_age > 5:
                        log(f"  {label} {flabel}数据停滞 {bar_age} 天（假期/停牌），跳过")
                        continue
                    if cur_j >= threshold:
                        log(f"  {label} {flabel}J={cur_j:.2f} 未低于阈值 {threshold}，无信号")
                        continue

                    fresh = prev_j >= threshold
                    last = notified.get(state_key, {}).get("last_notified", "")
                    days_since = (today - date.fromisoformat(last)).days if last else 9999
                    if not force and not fresh and days_since < min_interval:
                        log(f"  {label} {flabel}持续低位，距上次通知仅 {days_since} 天（< {min_interval}），跳过重复提醒")
                        continue

                    kind = "首次跌破" if fresh else "持续低位"
                    history = "\n".join(
                        f"    {r['date']}  收盘 {r['close']:>8.3f}  K {r['K']:>6.2f}  D {r['D']:>6.2f}  J {r['J']:>7.2f}"
                        for r in bars[-6:])
                    signals.append({
                        "code": code, "name": names.get(code, code), "threshold": threshold,
                        "freq": flabel, "kind": kind, "bar_date": str(cur["date"])[:10],
                        "close": cur["close"], "k": cur["K"], "d": cur["D"], "j": cur_j,
                        "prev_j": prev_j, "history": history,
                    })
                    notified[state_key] = {"last_notified": today.isoformat(), "last_j": round(cur_j, 2)}
                except Exception as exc:
                    log(f"  {label} {flabel}检查失败: {exc}")
                    failures.append(f"{code}{flabel}: {exc}")
        except Exception as exc:
            log(f"  {label} 检查失败: {exc}")
            failures.append(f"{code}: {exc}")

    if failures and not signals:
        log("所有标的检查失败:\n  " + "\n  ".join(failures))
        return 1

    if signals:
        prefix = "【测试】" if force else ""
        if len(signals) == 1:
            s = signals[0]
            subject = (f"{prefix}【买入信号】{s['name']}({s['code']}) {s['freq']} KDJ "
                       f"J={s['j']:.2f} < {s['threshold']:g}（{s['kind']}）")
        else:
            summary = "、".join(f"{s['code']}{s['freq']} J={s['j']:.1f}" for s in signals)
            subject = f"{prefix}【买入信号】{len(signals)} 个 KDJ 超卖信号: {summary}"
        body = "\n".join(
            f"买入信号（{s['freq']} KDJ 超卖，仅供参考，不构成投资建议）\n"
            f"{'=' * 52}\n"
            f"标的: {s['name']} ({s['code']})\n"
            f"信号: {s['freq']} J={s['j']:.2f} < 阈值 {s['threshold']:g}（{s['kind']}，前一根J={s['prev_j']:.2f}）\n"
            f"最新K线: {s['bar_date']}"
            + ("（含本周进行中数据，J 值随行情实时变化）" if s["freq"] == "周K" else "（当日收盘口径）") + "\n"
            f"收盘价: {s['close']:.3f}   K={s['k']:.2f}  D={s['d']:.2f}\n"
            f"\n{s['freq']}最近 6 根 KDJ:\n{s['history']}\n"
            for s in signals)
        body += ("\n" + "=" * 52 +
                 f"\n检查时间: {datetime.now(TZ_BJS).strftime('%Y-%m-%d %H:%M')}（北京时间）\n"
                 f"监控配置: KDJ_ALERT_CONFIG = {os.environ.get('KDJ_ALERT_CONFIG', '')}\n"
                 f"由 GitHub Actions 自动发送，修改阈值/标的/周期请到仓库 Settings -> Variables。")
        try:
            send_email(subject, body)
        except Exception as exc:
            log(f"邮件发送失败: {exc}")
            return 1
    elif force and statuses:
        # 强制模式：即使无信号也发送当前指标状态，用于验证整条通知链路
        summary = "、".join(f"{s['name']}({s['code']}){s['freq']} J={s['j']:.2f}" for s in statuses)
        subject = f"【测试】KDJ 预警链路正常，当前无信号：{summary}"
        body = "\n".join(
            f"{s['name']} ({s['code']})  {s['freq']}  最新K线 {s['bar_date']}   "
            f"J={s['j']:.2f}（阈值 {s['threshold']:g}，未触发）\n"
            for s in statuses)
        body += ("\n这是强制测试邮件，说明数据获取与邮件通道均正常。\n"
                 f"检查时间: {datetime.now(TZ_BJS).strftime('%Y-%m-%d %H:%M')}（北京时间）")
        try:
            send_email(subject, body)
        except Exception as exc:
            log(f"邮件发送失败: {exc}")
            return 1
    else:
        log("无触发信号，不发送通知")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"状态已写入 {STATE_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
