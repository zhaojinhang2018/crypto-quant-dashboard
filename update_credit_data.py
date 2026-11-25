#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美国信用利差看板 (Credit Spreads Dashboard) 数据拉取与更新脚本
数据源：
1. FRED (圣路易斯联储) 每日信用利差 (ICE BofA US Corporate & High Yield OAS, 最近3年日线):
   - BAMLH0A0HYM2: 美国高收益债利差 (HY OAS)
   - BAMLC0A0CM:   美国投资级整体利差 (IG OAS)
   - BAMLC0A4CBBB: 美国 BBB 级公司债利差 (BBB OAS)
   - BAMLH0A3HYC:  美国 CCC 及以下高收益债利差 (CCC & Lower OAS)
2. BTC-USD 日线价格 (用于可选叠加对比): Yahoo Finance API
"""

import subprocess
import urllib.request
import json
import datetime
import os
import re

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_JS_PATH = os.path.join(WORKSPACE_DIR, "credit_data.js")
DATA_JSON_PATH = os.path.join(WORKSPACE_DIR, "credit_spreads_data.json")
HTML_PATH = os.path.join(WORKSPACE_DIR, "credit_spreads.html")

FRED_SERIES = {
    "BAMLH0A0HYM2": "HY",
    "BAMLC0A0CM": "IG",
    "BAMLC0A4CBBB": "BBB",
    "BAMLH0A3HYC": "CCC",
    "DGS10": "UST10Y",
    "DGS2": "UST2Y",
    "T10Y2Y": "T10Y2Y",
    "T10Y3M": "T10Y3M",
}


def fetch_fred_series(series_id):
    """使用系统 curl.exe 从 FRED 拉取单个序列的 CSV 数据"""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    res = subprocess.run(
        ["curl.exe", "-sS", "-m", "25", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"拉取 {series_id} 失败: {res.stderr.strip()}")

    data = {}
    lines = res.stdout.strip().splitlines()
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) != 2:
            continue
        dt_str, val_str = parts[0].strip(), parts[1].strip()
        if not val_str or val_str == ".":
            continue
        try:
            data[dt_str] = round(float(val_str), 4)
        except ValueError:
            continue
    return data


def fetch_btc_prices(start_date_str):
    """从 Yahoo Finance 获取对应时间段的 BTC-USD 日线收盘价"""
    print("[2/3] 正在拉取 BTC-USD 日线价格用于联动对比...")
    dt = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    period1 = int(dt.timestamp()) - 86400
    period2 = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?period1={period1}&period2={period2}&interval=1d"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        res = raw["chart"]["result"][0]
        timestamps = res["timestamp"]
        closes = res["indicators"]["quote"][0].get("close", [])
        prices = {}
        for i, ts in enumerate(timestamps):
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue
            d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
            prices[d] = round(float(c), 2)
        print(f"      成功获取 {len(prices)} 个自然日的 BTC 价格。")
        return prices
    except Exception as e:
        print(f"      [提示] BTC 价格拉取失败（不影响信用利差主数据）: {e}")
        return {}


def build_dataset():
    print("[1/3] 正在从 FRED (圣路易斯联储) 拉取信用利差数据...")
    raw_series = {}
    all_dates = set()
    for sid, short_name in FRED_SERIES.items():
        sdata = fetch_fred_series(sid)
        raw_series[short_name] = sdata
        all_dates.update(sdata.keys())
        print(f"      {sid} ({short_name}): {len(sdata)} 个交易日")

    sorted_dates = sorted(all_dates)
    if not sorted_dates:
        raise RuntimeError("未能获取到任何 FRED 信用利差数据")

    btc_prices = fetch_btc_prices(sorted_dates[0])

    print("[3/3] 正在计算利差衍生指标并生成看板数据文件...")
    records = []
    prev_hy = None
    prev_ig = None
    prev_bbb = None
    prev_ccc = None
    last_btc = None

    for d in sorted_dates:
        hy = raw_series["HY"].get(d)
        ig = raw_series["IG"].get(d)
        bbb = raw_series["BBB"].get(d)
        ccc = raw_series["CCC"].get(d)
        ust10y = raw_series["UST10Y"].get(d)
        ust2y = raw_series["UST2Y"].get(d)
        t10y2y = raw_series["T10Y2Y"].get(d)
        t10y3m = raw_series["T10Y3M"].get(d)

        # 仅保留至少有主系列数据的日期
        if hy is None and ig is None and bbb is None and ccc is None:
            continue

        btc = btc_prices.get(d, last_btc)
        if btc is not None:
            last_btc = btc

        hy_ig = round(hy - ig, 4) if (hy is not None and ig is not None) else None
        ccc_hy = round(ccc - hy, 4) if (ccc is not None and hy is not None) else None

        hy_chg_bps = round((hy - prev_hy) * 100, 1) if (hy is not None and prev_hy is not None) else 0.0
        ig_chg_bps = round((ig - prev_ig) * 100, 1) if (ig is not None and prev_ig is not None) else 0.0
        bbb_chg_bps = round((bbb - prev_bbb) * 100, 1) if (bbb is not None and prev_bbb is not None) else 0.0
        ccc_chg_bps = round((ccc - prev_ccc) * 100, 1) if (ccc is not None and prev_ccc is not None) else 0.0

        if hy is not None:
            prev_hy = hy
        if ig is not None:
            prev_ig = ig
        if bbb is not None:
            prev_bbb = bbb
        if ccc is not None:
            prev_ccc = ccc

        records.append({
            "date": d,
            "hy": hy,
            "ig": ig,
            "bbb": bbb,
            "ccc": ccc,
            "ust10y": ust10y,
            "ust2y": ust2y,
            "t10y2y": t10y2y,
            "t10y3m": t10y3m,
            "hy_ig": hy_ig,
            "ccc_hy": ccc_hy,
            "hy_chg_bps": hy_chg_bps,
            "ig_chg_bps": ig_chg_bps,
            "bbb_chg_bps": bbb_chg_bps,
            "ccc_chg_bps": ccc_chg_bps,
            "btc_close": btc,
        })

    # 计算各序列统计摘要（最新值、1周/1月/3月变动 bps、3年均值与分位数）
    stats = {}
    for key in ["hy", "ig", "bbb", "ccc", "ust10y", "ust2y", "t10y2y", "t10y3m", "hy_ig", "ccc_hy"]:
        vals = [r[key] for r in records if r[key] is not None]
        if not vals:
            continue
        latest_val = vals[-1]
        mean_val = round(sum(vals) / len(vals), 4)
        min_val = min(vals)
        max_val = max(vals)
        pct_rank = round(sum(1 for v in vals if v <= latest_val) / len(vals) * 100, 1)

        def chg_bps(lookback):
            if len(vals) > lookback:
                return round((latest_val - vals[-1 - lookback]) * 100, 1)
            return None

        stats[key] = {
            "latest": latest_val,
            "mean": mean_val,
            "min": min_val,
            "max": max_val,
            "pct_rank": pct_rank,
            "chg_1d_bps": chg_bps(1),
            "chg_1w_bps": chg_bps(5),
            "chg_1m_bps": chg_bps(21),
            "chg_3m_bps": chg_bps(63),
        }

    payload = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start_date": records[0]["date"],
        "end_date": records[-1]["date"],
        "total_days": len(records),
        "series_meta": {
            "hy": {"code": "BAMLH0A0HYM2", "name": "高收益债 (HY OAS)"},
            "ig": {"code": "BAMLC0A0CM", "name": "投资级整体 (IG OAS)"},
            "bbb": {"code": "BAMLC0A4CBBB", "name": "BBB 级 (BBB OAS)"},
            "ccc": {"code": "BAMLH0A3HYC", "name": "CCC 及以下 (CCC OAS)"},
            "ust10y": {"code": "DGS10", "name": "10Y 美债收益率"},
            "ust2y": {"code": "DGS2", "name": "2Y 美债收益率"},
            "t10y2y": {"code": "T10Y2Y", "name": "10Y-2Y 美债利差"},
            "t10y3m": {"code": "T10Y3M", "name": "10Y-3M 美债利差"},
        },
        "stats": stats,
        "records": records,
    }

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(DATA_JS_PATH, "w", encoding="utf-8") as f:
        f.write(f"window.CREDIT_SPREAD_DATA = {json.dumps(payload, ensure_ascii=False)};\n")

    if os.path.exists(HTML_PATH):
        with open(HTML_PATH, "r", encoding="utf-8") as f:
            html_text = f.read()
        pattern = r"(/\*__INLINE_DATA_START__\*/).*?(/\*__INLINE_DATA_END__\*/)"
        if re.search(pattern, html_text, flags=re.DOTALL):
            replacement = f"\\1\nwindow.CREDIT_SPREAD_FALLBACK_DATA = {json.dumps(payload, ensure_ascii=False)};\n\\2"
            html_text = re.sub(pattern, replacement, html_text, flags=re.DOTALL)
            with open(HTML_PATH, "w", encoding="utf-8") as f:
                f.write(html_text)
            print("      已同步更新 credit_spreads.html 内置数据。")

    print(f"\n✅ 信用利差数据更新完成！")
    print(f"   - 时间范围: {payload['start_date']} 至 {payload['end_date']} (共 {payload['total_days']} 个交易日)")
    print(f"   - HY OAS (BAMLH0A0HYM2): {stats['hy']['latest']}% (近1周 {stats['hy']['chg_1w_bps']:+.1f}bp, 近3月 {stats['hy']['chg_3m_bps']:+.1f}bp)")
    print(f"   - IG OAS (BAMLC0A0CM):   {stats['ig']['latest']}%")
    print(f"   - BBB OAS (BAMLC0A4CBBB): {stats['bbb']['latest']}%")
    print(f"   - CCC OAS (BAMLH0A3HYC): {stats['ccc']['latest']}%")
    if "ust10y" in stats and "t10y2y" in stats:
        print(f"   - 10Y 美债 (DGS10):      {stats['ust10y']['latest']}% | 10Y-2Y (T10Y2Y): {stats['t10y2y']['latest']}%")


if __name__ == "__main__":
    build_dataset()
