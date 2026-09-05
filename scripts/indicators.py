# -*- coding: utf-8 -*-
"""独立技术指标查询工具（不影响主程序分析流程）。

用法:
    python scripts/indicators.py 600519                    # 日K+月K, 最新7组
    python scripts/indicators.py 600519 --freq daily       # 仅日K
    python scripts/indicators.py 300750 --last 10          # 最新10组
    python scripts/indicators.py 600519 --source baostock  # 指定数据源

说明:
    - 仅支持 A 股（600519 / 600519.SH / sh600519 均可）
    - 指标口径与通达信/同花顺一致: KDJ(9,3,3)、MACD(12,26,9, 柱=2*(DIF-DEA))、
      RSI(6,12,24, SMA[N,1] 平滑)，不复权
    - 月K由日线聚合（含当月进行中的实时月K），预热窗口 10 年
    - 数据源优先级: tushare(需 .env 配 TUSHARE_TOKEN) > baostock(免token) >
      本地库 data/stock_analysis.db(兜底)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LAST_N_DEFAULT = 7
DAILY_HISTORY_YEARS = 10


def parse_code(raw: str) -> tuple[str, str]:
    """解析股票代码，返回 (纯代码, tushare代码)。仅支持 A 股。"""
    code = raw.strip().lower()
    for suffix in (".sh", ".sz"):
        code = code.replace(suffix, "")
    if code.startswith(("sh", "sz")) and len(code) == 8:
        code = code[2:]
    if not (code.isdigit() and len(code) == 6):
        raise SystemExit(f"无法识别的股票代码: {raw}（当前仅支持 A 股 6 位代码）")
    suffix = "SH" if code[0] in "569" else "SZ"  # 5=基金 6/9=沪 0/2/3=深
    return code, f"{code}.{suffix}"


def fetch_tushare_daily(token: str, ts_code: str, start: str, end: str) -> pd.DataFrame:
    import tushare as ts
    pro = ts.pro_api(token)
    df = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError("tushare 返回空数据")
    df = df.rename(columns={"trade_date": "date"})[["date", "open", "high", "low", "close"]]
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


def fetch_baostock_daily(ts_code: str, start: str, end: str) -> pd.DataFrame:
    import baostock as bs
    bs_code = ("sh." if ts_code.endswith(".SH") else "sz.") + ts_code.split(".")[0]
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close",
            start_date=start, end_date=end, frequency="d", adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 查询失败: {rs.error_code} {rs.errmsg}")
        if not rows:
            raise RuntimeError("baostock 返回空数据")
    finally:
        bs.logout()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def fetch_local_daily(code: str) -> pd.DataFrame:
    import sqlite3
    conn = sqlite3.connect(PROJECT_ROOT / "data" / "stock_analysis.db")
    try:
        return pd.read_sql(
            "SELECT date, open, high, low, close FROM stock_daily WHERE code=? ORDER BY date",
            conn, params=(code,),
        )
    finally:
        conn.close()


def get_daily(code: str, ts_code: str, source: str) -> tuple[pd.DataFrame, str, list[str]]:
    start = (date.today() - timedelta(days=365 * DAILY_HISTORY_YEARS)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    errors = []
    if source in ("auto", "tushare"):
        try:
            from dotenv import dotenv_values
            token = (dotenv_values(PROJECT_ROOT / ".env").get("TUSHARE_TOKEN") or "").strip()
            if source == "tushare" and not token:
                raise SystemExit("未在 .env 配置 TUSHARE_TOKEN")
            if token:
                return fetch_tushare_daily(token, ts_code, start, end), "tushare", errors
        except SystemExit:
            raise
        except Exception as exc:  # 限频/网络错误时降级
            errors.append(f"tushare: {exc}")
    if source in ("auto", "baostock"):
        try:
            start_bs = pd.Timestamp(start).strftime("%Y-%m-%d")
            end_bs = pd.Timestamp(end).strftime("%Y-%m-%d")
            return fetch_baostock_daily(ts_code, start_bs, end_bs), "baostock", errors
        except Exception as exc:
            errors.append(f"baostock: {exc}")
    try:
        df = fetch_local_daily(code)
        if len(df):
            return df, "本地库(仅%d条, 指标预热有限)" % len(df), errors
    except Exception as exc:
        errors.append(f"local: {exc}")
    raise SystemExit("所有数据源获取失败:\n  " + "\n  ".join(errors))


def to_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """日线聚合成月K（当月为进行中的实时月K）。"""
    month = daily["date"].str[:7]
    monthly = daily.groupby(month).agg(
        date=("date", "last"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
    ).reset_index(drop=True)
    return monthly


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """追加 KDJ/MACD/RSI 列（通达信口径）。"""
    df = df.copy()
    low_n = df["low"].rolling(9).min()
    high_n = df["high"].rolling(9).max()
    rsv = ((df["close"] - low_n) / (high_n - low_n) * 100).fillna(50)
    df["K"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    df["D"] = df["K"].ewm(alpha=1 / 3, adjust=False).mean()
    df["J"] = 3 * df["K"] - 2 * df["D"]

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD"] = 2 * (df["DIF"] - df["DEA"])

    diff = df["close"].diff()
    for n in (6, 12, 24):
        up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        total = diff.abs().ewm(alpha=1 / n, adjust=False).mean()
        df[f"RSI{n}"] = up / total * 100
    return df


def print_table(title: str, df: pd.DataFrame, last: int, note: str = "") -> None:
    print(f"\n===== {title} (最新{min(last, len(df))}组) =====")
    header = "%-11s%9s%8s%8s%8s%9s%9s%8s%7s%7s%7s" % (
        "日期", "收盘", "K", "D", "J", "DIF", "DEA", "MACD", "RSI6", "RSI12", "RSI24")
    print(header)
    for _, r in df.tail(last).iterrows():
        print("%-11s%9.2f%8.2f%8.2f%8.2f%9.3f%9.3f%8.3f%7.2f%7.2f%7.2f" % (
            r["date"], r["close"], r["K"], r["D"], r["J"],
            r["DIF"], r["DEA"], r["MACD"], r["RSI6"], r["RSI12"], r["RSI24"]))
    if note:
        print(f"({note})")


def main() -> None:
    parser = argparse.ArgumentParser(description="A 股技术指标查询 (KDJ/MACD/RSI)")
    parser.add_argument("code", help="股票代码, 如 600519 / 600519.SH / sh600519")
    parser.add_argument("--freq", default="daily,monthly", help="daily,monthly 任意组合")
    parser.add_argument("--last", type=int, default=LAST_N_DEFAULT, help="每组输出条数")
    parser.add_argument("--source", choices=["auto", "tushare", "baostock", "local"],
                        default="auto", help="数据源")
    args = parser.parse_args()

    code, ts_code = parse_code(args.code)
    freqs = {f.strip() for f in args.freq.lower().split(",")} - {""}
    bad = freqs - {"daily", "monthly"}
    if bad:
        raise SystemExit(f"不支持的周期: {','.join(sorted(bad))}")

    daily, used, skipped = get_daily(code, ts_code, args.source)
    skip_note = (" | 已跳过: " + "; ".join(skipped)) if skipped else ""
    print(f"股票 {ts_code} | 数据源: {used} | 日线 {len(daily)} 条 "
          f"({daily['date'].iloc[0]} ~ {daily['date'].iloc[-1]}){skip_note}")

    if "daily" in freqs:
        print_table("日K KDJ/MACD/RSI", add_indicators(daily), args.last)
    if "monthly" in freqs:
        monthly = to_monthly(daily)
        print_table("月K KDJ/MACD/RSI", add_indicators(monthly), args.last,
                    note=f"月K由{len(daily)}条日线聚合, 含当月进行中数据; "
                         "月线MACD预热短于看盘软件全历史, 数值可能略有差异")


if __name__ == "__main__":
    main()
