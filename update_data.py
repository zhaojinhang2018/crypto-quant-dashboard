#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 现货 ETF 净流入/流出 & BTC 日线价格看板 数据拉取与更新脚本
数据源：
1. BTC-USD 日线价格 (2024-01-01 至今): Yahoo Finance API
2. 美国 BTC 现货 ETF 每日净流入/流出 (2024-01-11 至今): Farside Investors
"""

import urllib.request
import json
import datetime
import os
import re
from html.parser import HTMLParser

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_JS_PATH = os.path.join(WORKSPACE_DIR, "data.js")
DATA_JSON_PATH = os.path.join(WORKSPACE_DIR, "btc_etf_data.json")
HTML_PATH = os.path.join(WORKSPACE_DIR, "index.html")


class FarsideTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.tables = []
        self.curr_table = []
        self.curr_row = []
        self.curr_cell = ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table = True
            self.curr_table = []
        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.curr_row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.curr_cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            self.in_cell = False
            self.curr_row.append(self.curr_cell.strip())
        elif tag == "tr" and self.in_row:
            self.in_row = False
            if self.curr_row:
                self.curr_table.append(self.curr_row)
        elif tag == "table" and self.in_table:
            self.in_table = False
            self.tables.append(self.curr_table)

    def handle_data(self, data):
        if self.in_cell:
            self.curr_cell += data


def parse_flow_value(val_str):
    """解析 Farside 表格单元格数值（单位：百万美元 US$m）"""
    if not val_str:
        return None
    s = val_str.strip().replace(",", "")
    if s in ("-", "", "N/A"):
        return None
    if s.startswith("(") and s.endswith(")"):
        try:
            return -round(float(s[1:-1]), 2)
        except ValueError:
            return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def fetch_btc_daily_prices():
    """从 Yahoo Finance 获取 2024-01-01 至今的 BTC-USD 日线数据"""
    print("[1/3] 正在拉取 BTC-USD 日线行情数据 (Yahoo Finance)...")
    # 2024-01-01 00:00:00 UTC = 1704067200
    period1 = 1704067200
    period2 = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?period1={period1}&period2={period2}&interval=1d"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    result = raw["chart"]["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    price_by_date = {}
    for i, ts in enumerate(timestamps):
        dt_str = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        o = opens[i] if (i < len(opens) and opens[i] is not None) else c
        h = highs[i] if (i < len(highs) and highs[i] is not None) else c
        l = lows[i] if (i < len(lows) and lows[i] is not None) else c
        v = volumes[i] if (i < len(volumes) and volumes[i] is not None) else 0
        price_by_date[dt_str] = {
            "open": round(float(o), 2),
            "high": round(float(h), 2),
            "low": round(float(l), 2),
            "close": round(float(c), 2),
            "volume": round(float(v), 0),
        }

    print(f"      成功获取 {len(price_by_date)} 个自然日的 BTC 价格数据。")
    return price_by_date


def fetch_etf_flows():
    """从 Farside Investors 获取 2024-01-11 至今的 BTC 现货 ETF 每日净流入/流出数据"""
    print("[2/3] 正在拉取美国 BTC 现货 ETF 净流入/流出数据 (Farside Investors)...")
    url = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    parser = FarsideTableParser()
    parser.feed(html)

    if not parser.tables:
        raise RuntimeError("未能从 Farside 页面解析到数据表格")

    main_table = max(parser.tables, key=len)
    header = main_table[0]
    # Header e.g. ['Date', 'IBIT', 'FBTC', 'BITB', 'ARKB', 'BTCO', 'EZBC', 'BRRR', 'HODL', 'BTCW', 'MSBT', 'GBTC', 'BTC', 'Total']
    tickers = header[1:-1]

    etf_by_date = {}
    summary_totals = {}

    for row in main_table[1:]:
        if len(row) < len(header):
            continue
        date_cell = row[0]
        if date_cell.lower() == "total":
            for idx, ticker in enumerate(tickers, start=1):
                val = parse_flow_value(row[idx])
                summary_totals[ticker] = val if val is not None else 0.0
            total_val = parse_flow_value(row[-1])
            summary_totals["Total"] = total_val if total_val is not None else 0.0
            continue

        try:
            dt = datetime.datetime.strptime(date_cell, "%d %b %Y")
            dt_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

        breakdown = {}
        has_any_value = False
        for idx, ticker in enumerate(tickers, start=1):
            val = parse_flow_value(row[idx])
            if val is not None:
                has_any_value = True
                breakdown[ticker] = val
            else:
                breakdown[ticker] = 0.0

        total_flow = parse_flow_value(row[-1])
        # 如果当日全为 '-' 且 Total 为 0.0（例如美股休市日 15 Jan 2024），标记为休市或无交易
        if not has_any_value and (total_flow is None or total_flow == 0.0):
            continue

        if total_flow is None:
            total_flow = round(sum(breakdown.values()), 2)

        etf_by_date[dt_str] = {
            "total": total_flow,
            "breakdown": breakdown,
        }

    print(f"      成功获取 {len(etf_by_date)} 个美股交易日的 ETF 资金流向数据（包含 {len(tickers)} 只 ETF：{', '.join(tickers)}）。")
    return tickers, etf_by_date, summary_totals


def build_dataset():
    price_by_date = fetch_btc_daily_prices()
    tickers, etf_by_date, summary_totals = fetch_etf_flows()

    print("[3/3] 正在合并对齐日线数据并生成看板数据文件...")
    all_dates = sorted(set(price_by_date.keys()) | set(etf_by_date.keys()))
    all_dates = [d for d in all_dates if d >= "2024-01-01"]

    records = []
    cum_flow = 0.0
    last_price = None

    for d in all_dates:
        p_info = price_by_date.get(d)
        if p_info:
            last_price = p_info
        elif last_price:
            p_info = last_price
        else:
            continue

        etf_info = etf_by_date.get(d)
        is_trading_day = etf_info is not None
        net_flow = etf_info["total"] if is_trading_day else None
        breakdown = etf_info["breakdown"] if is_trading_day else None

        if net_flow is not None:
            cum_flow = round(cum_flow + net_flow, 2)

        records.append({
            "date": d,
            "open": p_info["open"],
            "high": p_info["high"],
            "low": p_info["low"],
            "close": p_info["close"],
            "volume": p_info["volume"],
            "etf_net_flow": net_flow,          # 单位: 百万美元 (US$M)，非交易日为 None
            "etf_cum_flow": cum_flow,          # 累计净流入 (US$M)
            "is_etf_trading_day": is_trading_day,
            "etf_breakdown": breakdown,
        })

    payload = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start_date": records[0]["date"] if records else "2024-01-01",
        "end_date": records[-1]["date"] if records else "",
        "total_calendar_days": len(records),
        "total_trading_days": sum(1 for r in records if r["is_etf_trading_day"]),
        "tickers": tickers,
        "summary_totals_musd": summary_totals,
        "records": records,
    }

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    js_content = f"window.BTC_ETF_DATA = {json.dumps(payload, ensure_ascii=False)};\n"
    with open(DATA_JS_PATH, "w", encoding="utf-8") as f:
        f.write(js_content)

    # 如果 index.html 存在且包含内联数据占位符，同时同步更新内联备份，确保单文件移动也能打开
    if os.path.exists(HTML_PATH):
        with open(HTML_PATH, "r", encoding="utf-8") as f:
            html_text = f.read()
        pattern = r"(/\*__INLINE_DATA_START__\*/).*?(/\*__INLINE_DATA_END__\*/)"
        if re.search(pattern, html_text, flags=re.DOTALL):
            replacement = f"\\1\nwindow.BTC_ETF_FALLBACK_DATA = {json.dumps(payload, ensure_ascii=False)};\n\\2"
            html_text = re.sub(pattern, replacement, html_text, flags=re.DOTALL)
            with open(HTML_PATH, "w", encoding="utf-8") as f:
                f.write(html_text)
            print("      已同步更新 index.html 内置数据。")

    print(f"\n✅ 更新完成！")
    print(f"   - 时间范围: {payload['start_date']} 至 {payload['end_date']}")
    print(f"   - 自然日总数: {payload['total_calendar_days']} 天 | ETF 交易日总数: {payload['total_trading_days']} 天")
    print(f"   - 最新累计净流入: ${cum_flow:,.1f}M (${cum_flow/1000:,.2f}B)")
    print(f"   - 数据文件已保存至: {DATA_JS_PATH} 和 {DATA_JSON_PATH}")


if __name__ == "__main__":
    build_dataset()
