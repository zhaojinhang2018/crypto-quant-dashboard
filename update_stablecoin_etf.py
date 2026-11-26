#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
稳定币铸造/注销 + BTC 现货 ETF 流向 + BTC 价格 联动观察看板 数据拉取脚本
数据源：
1. DeFiLlama Stablecoins API: 全网美元稳定币总流通市值 (peggedUSD)
2. Yahoo Finance: BTC-USD 日线收盘价
3. Farside Investors: 美国 BTC 现货 ETF 每日净流入/流出
"""

import datetime
import json
import os
import shutil
import subprocess
import time
from html.parser import HTMLParser

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_JS_PATH = os.path.join(WORKSPACE_DIR, "stablecoin_etf_data.js")
OUT_JSON_PATH = os.path.join(WORKSPACE_DIR, "stablecoin_etf_data.json")
FALLBACK_BTC_ETF_JSON = os.path.join(WORKSPACE_DIR, "btc_etf_data.json")

START_DATE = "2023-12-01"
DISPLAY_START = "2024-01-11"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


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


def curl_get(url, timeout=25, use_ua=True):
    curl_bin = shutil.which("curl.exe") or shutil.which("curl") or "curl"
    cmd = [curl_bin, "-sS", "-L", "-m", str(timeout)]
    if use_ua:
        cmd.extend(["-A", UA])
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"curl 失败 ({url}): {res.stderr.strip()}")
    return res.stdout


def fetch_stablecoins():
    print("[1/3] 正在拉取 DeFiLlama 全网稳定币流通市值...")
    raw_text = curl_get("https://stablecoins.llama.fi/stablecoincharts/all", timeout=25, use_ua=False)
    raw = json.loads(raw_text)
    out = {}
    for item in raw:
        d = datetime.datetime.fromtimestamp(int(item["date"]), datetime.timezone.utc).strftime("%Y-%m-%d")
        val = item.get("totalCirculatingUSD", {}).get("peggedUSD")
        if val and d >= START_DATE:
            out[d] = round(float(val) / 1e9, 3)  # Billions USD
    print(f"      成功获取 {len(out)} 天稳定币数据 (最新: {max(out.keys())} = ${out[max(out.keys())]:.2f}B)")
    return out


def fetch_btc_daily():
    print("[2/3] 正在拉取 BTC-USD 日线价格 (Yahoo Finance)...")
    dt1 = datetime.datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    p1 = int(dt1.timestamp())
    p2 = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?period1={p1}&period2={p2}&interval=1d"
    for _ in range(3):
        try:
            raw = json.loads(curl_get(url, timeout=25, use_ua=True))
            res = raw["chart"]["result"][0]
            ts_list = res["timestamp"]
            closes = res["indicators"]["quote"][0].get("close", [])
            out = {}
            for i, ts in enumerate(ts_list):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
                out[d] = round(float(c), 2)
            print(f"      成功获取 {len(out)} 天 BTC 价格 (最新: {max(out.keys())} = ${out[max(out.keys())]:,.2f})")
            return out
        except Exception:
            time.sleep(0.8)
    raise RuntimeError("拉取 BTC-USD 失败")


def parse_flow_val(s):
    if not s:
        return None
    s = s.strip().replace(",", "")
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


def fetch_btc_etf():
    print("[3/3] 正在拉取 Farside BTC 现货 ETF 每日净流入/流出...")
    try:
        html = curl_get("https://farside.co.uk/bitcoin-etf-flow-all-data/", timeout=25, use_ua=True)
        parser = FarsideTableParser()
        parser.feed(html)
        if parser.tables:
            main_table = max(parser.tables, key=len)
            header = main_table[0]
            flows = {}
            for row in main_table[1:]:
                if len(row) < len(header):
                    continue
                date_cell = row[0].strip()
                if date_cell.lower() == "total":
                    continue
                try:
                    dt_str = datetime.datetime.strptime(date_cell, "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    continue
                total_val = parse_flow_val(row[-1])
                if total_val is None:
                    vals = [parse_flow_val(c) for c in row[1:-1]]
                    valid = [v for v in vals if v is not None]
                    if not valid:
                        continue
                    total_val = round(sum(valid), 2)
                flows[dt_str] = total_val
            if len(flows) > 50:
                print(f"      成功获取 {len(flows)} 个交易日 ETF 数据 (最新: {max(flows.keys())})")
                return flows
    except Exception as e:
        print(f"      [警告] Farside 拉取失败，使用本地缓存: {e}")

    if os.path.exists(FALLBACK_BTC_ETF_JSON):
        with open(FALLBACK_BTC_ETF_JSON, "r", encoding="utf-8") as f:
            cached = json.load(f)
        return {r["date"]: r["etf_total"] for r in cached.get("records", []) if r.get("etf_total") is not None}
    return {}


def build():
    stables = fetch_stablecoins()
    btc = fetch_btc_daily()
    etf = fetch_btc_etf()

    all_dates = sorted(d for d in btc.keys() if d >= START_DATE)

    # 对齐稳定币（前向填充缺失日）
    sc_aligned = {}
    last_sc = None
    for d in all_dates:
        if d in stables:
            last_sc = stables[d]
        sc_aligned[d] = last_sc

    # 计算 ETF 累计值（按自然日滚动 7D/14D/30D 以及累计总额，单位：百万美元 $M）
    records = []
    etf_cum = 0.0
    for idx, d in enumerate(all_dates):
        etf_day = etf.get(d, 0.0)
        etf_cum = round(etf_cum + etf_day, 2)

        if d < DISPLAY_START:
            continue

        sc_cur = sc_aligned[d]

        def sc_diff(days):
            if idx < days or sc_cur is None:
                return None
            prev = sc_aligned[all_dates[idx - days]]
            return round(sc_cur - prev, 3) if prev is not None else None

        def etf_sum(days):
            start_i = max(0, idx - days + 1)
            return round(sum(etf.get(all_dates[j], 0.0) for j in range(start_i, idx + 1)), 2)

        records.append({
            "date": d,
            "btc": btc[d],
            "sc_mcap": sc_cur,          # 十亿美元 ($B)
            "sc_1d": sc_diff(1),        # 十亿美元 ($B)
            "sc_7d": sc_diff(7),
            "sc_14d": sc_diff(14),
            "sc_30d": sc_diff(30),
            "etf_1d": etf.get(d),       # 百万美元 ($M), 非交易日为 null
            "etf_7d": etf_sum(7),       # 百万美元 ($M)
            "etf_14d": etf_sum(14),
            "etf_30d": etf_sum(30),
            "etf_cum": etf_cum,         # 百万美元 ($M)
        })

    payload = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_date": records[-1]["date"],
        "freshness": {
            "btc": max(btc.keys()),
            "stablecoin": max(stables.keys()),
            "etf": max(etf.keys()) if etf else "--",
        },
        "latest": records[-1],
        "records": records,
    }

    with open(OUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(OUT_JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.STABLECOIN_ETF_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    lt = records[-1]
    print("\n" + "=" * 62)
    print(f"最新日期: {lt['date']} | BTC: ${lt['btc']:,.2f} | 稳定币总市值: ${lt['sc_mcap']:.2f}B")
    print(f"稳定币净变动: 7D {lt['sc_7d']:+.2f}B | 14D {lt['sc_14d']:+.2f}B | 30D {lt['sc_30d']:+.2f}B")
    print(f"BTC ETF净流入: 7D {lt['etf_7d']:+.1f}M | 14D {lt['etf_14d']:+.1f}M | 30D {lt['etf_30d']:+.1f}M")
    print("=" * 62)


if __name__ == "__main__":
    build()
