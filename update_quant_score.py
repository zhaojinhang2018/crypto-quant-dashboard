#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加密市场量化风控打分系统 (Macro + Liquidity + Tech/Crypto-Stock + MA Trend)
数据源（全部免费公开接口）：
1. Yahoo Finance: BTC-USD, ETH-USD, SOL-USD, MSTR, BMNR, COIN, CRCL, HOOD, QQQ, IGV, SOXX
2. FRED (圣路易斯联储): CPILFESL, EFFR, DGS2, DGS10, BAMLH0A0HYM2, WALCL, WTREGEN, RRPONTSYD
3. Farside Investors: 美国 BTC 现货 ETF & ETH 现货 ETF 每日净流入/流出
4. 本地配置文件: fed_stance.json (美联储官员口风手动档位)
"""

import datetime
import json
import math
import os
import shutil
import subprocess
import time
import urllib.request
from html.parser import HTMLParser

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
FED_STANCE_PATH = os.path.join(WORKSPACE_DIR, "fed_stance.json")
OUT_JS_PATH = os.path.join(WORKSPACE_DIR, "quant_data.js")
OUT_JSON_PATH = os.path.join(WORKSPACE_DIR, "quant_data.json")
FALLBACK_BTC_ETF_JSON = os.path.join(WORKSPACE_DIR, "btc_etf_data.json")

START_DATE = "2023-06-01"   # 多拉半年用于计算 200D / 20W 均线
BACKTEST_START = "2024-01-11"  # BTC 现货 ETF 上市首日起回测

SYMBOLS = {
    "BTC-USD": {"name": "Bitcoin", "group": "crypto"},
    "ETH-USD": {"name": "Ethereum", "group": "crypto"},
    "SOL-USD": {"name": "Solana", "group": "crypto"},
    "MSTR": {"name": "Strategy", "group": "crypto_stock"},
    "BMNR": {"name": "BitMine", "group": "crypto_stock"},
    "COIN": {"name": "Coinbase", "group": "crypto_stock"},
    "CRCL": {"name": "Circle", "group": "crypto_stock"},
    "HOOD": {"name": "Robinhood", "group": "crypto_stock"},
    "QQQ": {"name": "Nasdaq 100", "group": "us_tech"},
    "IGV": {"name": "US Software ETF", "group": "us_tech"},
    "SOXX": {"name": "Semiconductor ETF", "group": "us_tech"},
}

FRED_SERIES = [
    "CPILFESL",      # 美国核心 CPI (月度)
    "EFFR",          # 有效联邦基金利率
    "DGS2",          # 2年期美债收益率
    "DGS10",         # 10年期美债收益率
    "BAMLH0A0HYM2",  # 美国高收益债信用利差 (HY OAS)
    "WALCL",         # 美联储总资产 (百万美元)
    "WTREGEN",       # 财政部 TGA 账户 (百万美元)
    "RRPONTSYD",     # 隔夜逆回购 RRP (十亿美元)
]

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


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def curl_get(url, timeout=25, use_ua=True):
    curl_bin = shutil.which("curl.exe") or shutil.which("curl") or "curl"
    cmd = [curl_bin, "-sS", "-L", "-m", str(timeout)]
    if use_ua:
        cmd.extend(["-A", UA])
    cmd.append(url)
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"curl 失败 ({url}): {res.stderr.strip()}")
    return res.stdout


def fetch_yahoo_daily(symbol):
    dt1 = datetime.datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    p1 = int(dt1.timestamp())
    p2 = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={p1}&period2={p2}&interval=1d"
    last_err = None
    for attempt in range(3):
        try:
            raw_text = curl_get(url, timeout=25)
            raw = json.loads(raw_text)
            res = raw["chart"]["result"][0]
            ts_list = res["timestamp"]
            closes = res["indicators"]["quote"][0].get("close", [])
            out = {}
            for i, ts in enumerate(ts_list):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
                out[d] = round(float(c), 4)
            return out
        except Exception as e:
            last_err = e
            time.sleep(0.8)
    raise RuntimeError(f"拉取 {symbol} 失败: {last_err}")


def fetch_fred_series(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    last_err = None
    csv_text = None
    for attempt in range(3):
        try:
            csv_text = curl_get(url, timeout=20, use_ua=False)
            break
        except Exception as e:
            last_err = e
            time.sleep(0.8)
    if csv_text is None:
        raise last_err
    data = {}
    for line in csv_text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) != 2:
            continue
        dt_str, val_str = parts[0].strip(), parts[1].strip()
        if dt_str < "2023-01-01" or not val_str or val_str == ".":
            continue
        try:
            data[dt_str] = round(float(val_str), 4)
        except ValueError:
            continue
    return data


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


def fetch_farside_flows(url):
    html = curl_get(url, timeout=25)
    parser = FarsideTableParser()
    parser.feed(html)
    if not parser.tables:
        return {}
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
            dt = datetime.datetime.strptime(date_cell, "%d %b %Y")
            dt_str = dt.strftime("%Y-%m-%d")
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
    return flows


def load_btc_etf_flows():
    try:
        flows = fetch_farside_flows("https://farside.co.uk/bitcoin-etf-flow-all-data/")
        if len(flows) > 50:
            return flows
    except Exception as e:
        print(f"      [警告] Farside BTC ETF 拉取失败，尝试读取本地缓存: {e}")
    if os.path.exists(FALLBACK_BTC_ETF_JSON):
        with open(FALLBACK_BTC_ETF_JSON, "r", encoding="utf-8") as f:
            cached = json.load(f)
        return {r["date"]: r["etf_total"] for r in cached.get("records", []) if r.get("etf_total") is not None}
    return {}


def load_eth_etf_flows():
    try:
        return fetch_farside_flows("https://farside.co.uk/ethereum-etf-flow-all-data/")
    except Exception as e:
        print(f"      [提示] Farside ETH ETF 拉取失败（仅使用 BTC ETF 数据）: {e}")
        return {}


def load_fed_stance():
    default = {"level": 0, "label": "中性", "note": "未设置", "updated_at": ""}
    if not os.path.exists(FED_STANCE_PATH):
        return default
    try:
        with open(FED_STANCE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        default.update(data)
        return default
    except Exception:
        return default


def align_forward_fill(dates, raw_map):
    """按目标日期序列前向填充"""
    sorted_keys = sorted(raw_map.keys())
    idx = 0
    last_val = None
    aligned = []
    for d in dates:
        while idx < len(sorted_keys) and sorted_keys[idx] <= d:
            last_val = raw_map[sorted_keys[idx]]
            idx += 1
        aligned.append(last_val)
    return aligned


def compute_sma(series, window):
    out = [None] * len(series)
    for i in range(len(series)):
        if i + 1 < window:
            continue
        win = [x for x in series[i - window + 1 : i + 1] if x is not None]
        if len(win) == window:
            out[i] = sum(win) / window
    return out


def compute_bull_market_support_band(dates, closes):
    """
    计算标准 Bull Market Support Band (20W SMA & 21W EMA)
    按 ISO 周聚合每周最后一个有效收盘价，计算 20W SMA 和 21W EMA 后映射回每日序列
    """
    weekly_order = []
    weekly_close = {}
    date_to_week = {}
    for d, c in zip(dates, closes):
        if c is None:
            continue
        dt = datetime.datetime.strptime(d, "%Y-%m-%d")
        iso = dt.isocalendar()
        wk = f"{iso[0]}-W{iso[1]:02d}"
        if wk not in weekly_close:
            weekly_order.append(wk)
        weekly_close[wk] = c
        date_to_week[d] = wk

    w_vals = [weekly_close[wk] for wk in weekly_order]
    w_sma20 = {}
    w_ema21 = {}
    k = 2.0 / (21.0 + 1.0)
    ema = None
    for i, wk in enumerate(weekly_order):
        if i >= 19:
            w_sma20[wk] = sum(w_vals[i - 19 : i + 1]) / 20.0
        if i == 20:
            ema = sum(w_vals[:21]) / 21.0
            w_ema21[wk] = ema
        elif i > 20 and ema is not None:
            ema = w_vals[i] * k + ema * (1.0 - k)
            w_ema21[wk] = ema

    # 对每日日期使用「已收盘的最近完整周」或「当前周实时值」
    sma20_daily = []
    ema21_daily = []
    last_s = None
    last_e = None
    for d in dates:
        wk = date_to_week.get(d)
        if wk in w_sma20:
            last_s = w_sma20[wk]
        if wk in w_ema21:
            last_e = w_ema21[wk]
        sma20_daily.append(last_s)
        ema21_daily.append(last_e)
    return sma20_daily, ema21_daily


def compute_core_cpi_3m_ann(dates, cpi_raw):
    """计算核心 CPI 近3个月折年率 (%)，按月度发布日对齐"""
    months = sorted(cpi_raw.keys())
    ann_map = {}
    for i in range(3, len(months)):
        cur = cpi_raw[months[i]]
        prev3 = cpi_raw[months[i - 3]]
        if prev3 > 0:
            ann_map[months[i]] = ((cur / prev3) ** 4 - 1.0) * 100.0
    return align_forward_fill(dates, ann_map)


def score_asset_ma(price, sma20, sma50=None):
    if price is None or sma20 is None:
        return None
    if sma50 is not None:
        if price > sma20 and price > sma50:
            return 1.0
        if price > sma20 or price > sma50:
            return 0.5
        return 0.0
    return 1.0 if price > sma20 else 0.0


def build_all():
    print("[1/4] 正在拉取 Yahoo Finance 行情数据（3 币 + 5 币股 + 3 美股科技指数）...")
    yf_data = {}
    for sym in SYMBOLS:
        yf_data[sym] = fetch_yahoo_daily(sym)
        print(f"      {sym:8s} -> {len(yf_data[sym])} 条日线")

    print("[2/4] 正在拉取 FRED 宏观与流动性数据...")
    fred_data = {}
    for fid in FRED_SERIES:
        fred_data[fid] = fetch_fred_series(fid)
        print(f"      {fid:12s} -> {len(fred_data[fid])} 条")

    print("[3/4] 正在拉取 Farside BTC/ETH 现货 ETF 资金流向...")
    btc_etf = load_btc_etf_flows()
    eth_etf = load_eth_etf_flows()
    print(f"      BTC ETF: {len(btc_etf)} 个交易日 | ETH ETF: {len(eth_etf)} 个交易日")

    fed_stance = load_fed_stance()

    # 以 BTC-USD 日历为主轴（加密市场 7x24），美股/FRED 按交易日前向填充
    all_dates = sorted(d for d in yf_data["BTC-USD"].keys() if d >= START_DATE)

    # 对齐所有资产收盘价与均线
    asset_series = {}
    for sym in SYMBOLS:
        closes = align_forward_fill(all_dates, yf_data[sym])
        # 注意：非 7x24 的美股/ETF 在周末前向填充会导致日均线被拉长，因此对美股/ETF 用其自身交易日计算均线再对齐！
        if SYMBOLS[sym]["group"] != "crypto":
            sym_dates = sorted(d for d in yf_data[sym].keys() if d >= START_DATE)
            sym_closes = [yf_data[sym][d] for d in sym_dates]
            s20_raw = compute_sma(sym_closes, 20)
            s50_raw = compute_sma(sym_closes, 50)
            s200_raw = compute_sma(sym_closes, 200)
            bms_s_raw, bms_e_raw = compute_bull_market_support_band(sym_dates, sym_closes)

            s20 = align_forward_fill(all_dates, {d: v for d, v in zip(sym_dates, s20_raw) if v is not None})
            s50 = align_forward_fill(all_dates, {d: v for d, v in zip(sym_dates, s50_raw) if v is not None})
            s200 = align_forward_fill(all_dates, {d: v for d, v in zip(sym_dates, s200_raw) if v is not None})
            bms_s = align_forward_fill(all_dates, {d: v for d, v in zip(sym_dates, bms_s_raw) if v is not None})
            bms_e = align_forward_fill(all_dates, {d: v for d, v in zip(sym_dates, bms_e_raw) if v is not None})
            ret10_raw = {}
            for i in range(10, len(sym_dates)):
                ret10_raw[sym_dates[i]] = (sym_closes[i] / sym_closes[i - 10] - 1.0) * 100.0
            ret10 = align_forward_fill(all_dates, ret10_raw)
        else:
            s20 = compute_sma(closes, 20)
            s50 = compute_sma(closes, 50)
            s200 = compute_sma(closes, 200)
            bms_s, bms_e = compute_bull_market_support_band(all_dates, closes)
            ret10 = [None] * len(all_dates)
            for i in range(10, len(all_dates)):
                if closes[i] and closes[i - 10]:
                    ret10[i] = (closes[i] / closes[i - 10] - 1.0) * 100.0

        asset_series[sym] = {
            "close": closes,
            "sma20": s20,
            "sma50": s50,
            "sma200": s200,
            "bmsb_sma20w": bms_s,
            "bmsb_ema21w": bms_e,
            "ret10": ret10,
        }

    # 对齐宏观指标
    cpi_3m_ann = compute_core_cpi_3m_ann(all_dates, fred_data["CPILFESL"])
    effr = align_forward_fill(all_dates, fred_data["EFFR"])
    dgs2 = align_forward_fill(all_dates, fred_data["DGS2"])
    dgs10 = align_forward_fill(all_dates, fred_data["DGS10"])
    hy_oas = align_forward_fill(all_dates, fred_data["BAMLH0A0HYM2"])

    # 美元净流动性 (十亿美元): (WALCL - WTREGEN)/1000 - RRPONTSYD
    walcl = align_forward_fill(all_dates, fred_data["WALCL"])
    tga = align_forward_fill(all_dates, fred_data["WTREGEN"])
    rrp = align_forward_fill(all_dates, fred_data["RRPONTSYD"])
    net_liq = []
    for w, t, r in zip(walcl, tga, rrp):
        if w is not None and t is not None and r is not None:
            net_liq.append(round((w - t) / 1000.0 - r, 2))
        else:
            net_liq.append(None)

    # ETF 流向按美股交易日窗口计算 5日/10日累计净流入
    etf_trade_dates = sorted(set(btc_etf.keys()) | set(eth_etf.keys()))
    etf_5d_map = {}
    etf_10d_map = {}
    etf_neg_streak_map = {}
    neg_streak = 0
    combined_flows = []
    for td in etf_trade_dates:
        tot = (btc_etf.get(td) or 0.0) + (eth_etf.get(td) or 0.0)
        combined_flows.append((td, tot))
        if tot < 0:
            neg_streak += 1
        elif tot > 0:
            neg_streak = 0
        etf_neg_streak_map[td] = neg_streak
    for i, (td, _) in enumerate(combined_flows):
        w5 = [x[1] for x in combined_flows[max(0, i - 4) : i + 1]]
        w10 = [x[1] for x in combined_flows[max(0, i - 9) : i + 1]]
        etf_5d_map[td] = round(sum(w5), 2)
        etf_10d_map[td] = round(sum(w10), 2)

    etf_5d = align_forward_fill(all_dates, etf_5d_map)
    etf_10d = align_forward_fill(all_dates, etf_10d_map)
    etf_streak = align_forward_fill(all_dates, etf_neg_streak_map)

    print("[4/4] 正在计算每日量化评分、熔断状态与历史回测...")
    fed_level = int(fed_stance.get("level", 0))
    fed_score_val = clamp((fed_level + 2) * 2.0, 0.0, 8.0)  # -2->0, -1->2, 0->4, 1->6, 2->8

    history = []
    btc_break_streak = 0

    for i, d in enumerate(all_dates):
        if d < BACKTEST_START:
            continue

        # --- 一、慢变量 (35分) ---
        # 1. 核心 CPI 3M 年化 (10分)
        cpi_v = cpi_3m_ann[i]
        if cpi_v is None:
            s_cpi = 5.0
        elif cpi_v <= 2.2:
            s_cpi = 10.0
        elif cpi_v >= 4.5:
            s_cpi = 0.0
        else:
            s_cpi = round(10.0 * (4.5 - cpi_v) / (4.5 - 2.2), 2)

        # 2. 债市加减息预期 DGS2 - EFFR (10分)
        spread_2y_ff = (dgs2[i] - effr[i]) if (dgs2[i] is not None and effr[i] is not None) else 0.0
        if spread_2y_ff <= -0.75:
            s_rate_exp = 10.0
        elif spread_2y_ff >= 0.50:
            s_rate_exp = 1.0
        else:
            s_rate_exp = round(1.0 + 9.0 * (0.50 - spread_2y_ff) / 1.25, 2)

        # 3. Fed 官员口风 (8分)
        s_fed = fed_score_val

        # 4. 信用利差 HY OAS (7分)
        hy_cur = hy_oas[i]
        hy_prev20 = hy_oas[max(0, i - 20)]
        hy_chg = (hy_cur - hy_prev20) if (hy_cur is not None and hy_prev20 is not None) else 0.0
        if hy_cur is None:
            s_hy = 4.0
        else:
            lvl_score = clamp(5.0 * (5.2 - hy_cur) / (5.2 - 3.0), 0.0, 5.0)
            chg_score = clamp(2.0 * (0.50 - hy_chg) / 0.60, 0.0, 2.0)
            s_hy = round(lvl_score + chg_score, 2)

        slow_score = round(s_cpi + s_rate_exp + s_fed + s_hy, 2)

        # --- 二、快变量 (40分) ---
        # 5. ETF 5D/10D 净流入 (12分)
        f5 = etf_5d[i] or 0.0
        f10 = etf_10d[i] or 0.0
        streak = etf_streak[i] or 0
        s_etf_base = clamp(6.0 + (f5 / 1200.0) * 5.0 + (f10 / 3000.0) * 1.0, 0.0, 12.0)
        if streak >= 4:
            s_etf_base = max(0.0, s_etf_base - 2.0)
        s_etf = round(s_etf_base, 2)

        # 6. 币股篮子风向 MSTR/BMNR/COIN/CRCL/HOOD (8分)
        cs_syms = ["MSTR", "BMNR", "COIN", "CRCL", "HOOD"]
        cs_valid = 0
        cs_above20 = 0
        cs_rets = []
        for sym in cs_syms:
            c = asset_series[sym]["close"][i]
            m20 = asset_series[sym]["sma20"][i]
            r10 = asset_series[sym]["ret10"][i]
            if c is not None and m20 is not None:
                cs_valid += 1
                if c > m20:
                    cs_above20 += 1
            if r10 is not None:
                cs_rets.append(r10)
        cs_ratio = (cs_above20 / cs_valid) if cs_valid > 0 else 0.5
        qqq_r10 = asset_series["QQQ"]["ret10"][i] or 0.0
        cs_avg_r10 = (sum(cs_rets) / len(cs_rets)) if cs_rets else 0.0
        cs_rel_qqq = cs_avg_r10 - qqq_r10
        s_crypto_stocks = round(cs_ratio * 5.0 + clamp((cs_rel_qqq + 10.0) / 20.0 * 3.0, 0.0, 3.0), 2)

        # 7. 美股科技 & 高Beta成长 QQQ + IGV + SOXX (8分)
        q_s = score_asset_ma(asset_series["QQQ"]["close"][i], asset_series["QQQ"]["sma20"][i], asset_series["QQQ"]["sma50"][i])
        igv_s = score_asset_ma(asset_series["IGV"]["close"][i], asset_series["IGV"]["sma20"][i], asset_series["IGV"]["sma50"][i])
        soxx_s = score_asset_ma(asset_series["SOXX"]["close"][i], asset_series["SOXX"]["sma20"][i], asset_series["SOXX"]["sma50"][i])
        s_tech = round((q_s or 0.5) * 3.0 + (igv_s or 0.5) * 2.5 + (soxx_s or 0.5) * 2.5, 2)

        # 8. 2Y & 10Y 美债收益率 14日变化率 (6分)
        d2_chg = (dgs2[i] - dgs2[max(0, i - 14)]) if (dgs2[i] is not None and dgs2[max(0, i - 14)] is not None) else 0.0
        d10_chg = (dgs10[i] - dgs10[max(0, i - 14)]) if (dgs10[i] is not None and dgs10[max(0, i - 14)] is not None) else 0.0
        avg_y_chg_bp = ((d2_chg + d10_chg) / 2.0) * 100.0
        s_yield_vel = round(clamp(3.5 - avg_y_chg_bp / 10.0, 0.0, 6.0), 2)

        # 9. 美元净流动性 28日变化 (6分)
        nl_cur = net_liq[i]
        nl_prev28 = net_liq[max(0, i - 28)]
        nl_chg = (nl_cur - nl_prev28) if (nl_cur is not None and nl_prev28 is not None) else 0.0
        s_net_liq = round(clamp(3.0 + (nl_chg / 150.0) * 3.0, 0.0, 6.0), 2)

        fast_score = round(s_etf + s_crypto_stocks + s_tech + s_yield_vel + s_net_liq, 2)

        # --- 三、BTC 技术面均线 (25分) ---
        btc_c = asset_series["BTC-USD"]["close"][i]
        btc_20 = asset_series["BTC-USD"]["sma20"][i]
        btc_20_prev = asset_series["BTC-USD"]["sma20"][max(0, i - 5)]
        btc_200 = asset_series["BTC-USD"]["sma200"][i]
        bms_s = asset_series["BTC-USD"]["bmsb_sma20w"][i]
        bms_e = asset_series["BTC-USD"]["bmsb_ema21w"][i]

        # 10. BTC vs 20D SMA (8分)
        if btc_c and btc_20:
            if btc_c > btc_20:
                s_ma20 = 8.0 if (btc_20_prev and btc_20 >= btc_20_prev) else 6.0
            elif btc_c > btc_20 * 0.98:
                s_ma20 = 3.0
            else:
                s_ma20 = 0.0
        else:
            s_ma20 = 4.0

        # 11. BTC vs 牛熊支撑带 BMSB (10分)
        if btc_c and bms_s and bms_e:
            band_hi = max(bms_s, bms_e)
            band_lo = min(bms_s, bms_e)
            if btc_c > band_hi:
                s_bmsb = 10.0
            elif btc_c >= band_lo:
                s_bmsb = 5.0
            else:
                s_bmsb = 0.0
        else:
            band_lo = None
            s_bmsb = 5.0

        # 12. BTC vs 200D SMA (7分)
        if btc_c and btc_200:
            if btc_c > btc_200:
                s_ma200 = 7.0
            elif btc_c > btc_200 * 0.98:
                s_ma200 = 2.0
            else:
                s_ma200 = 0.0
        else:
            s_ma200 = 3.5

        tech_score = round(s_ma20 + s_bmsb + s_ma200, 2)
        total_score = round(slow_score + fast_score + tech_score, 1)

        # --- 熔断机制检查 ---
        below_bmsb_or_200 = False
        if btc_c:
            cond_bmsb = (band_lo is not None and btc_c < band_lo)
            cond_200 = (btc_200 is not None and btc_c < btc_200)
            below_bmsb_or_200 = cond_bmsb or cond_200
        btc_break_streak = (btc_break_streak + 1) if below_bmsb_or_200 else 0

        breaker_a = btc_break_streak >= 3
        breaker_b = bool(btc_c and btc_20 and btc_c < btc_20 and (f5 < 0 or (cs_valid > 0 and cs_above20 <= 1)))

        # 熔断 C: IGV / SOXX 成长科技破位与 ETH 共振警报
        igv_c = asset_series["IGV"]["close"][i]
        igv_20 = asset_series["IGV"]["sma20"][i]
        igv_50 = asset_series["IGV"]["sma50"][i]
        soxx_c = asset_series["SOXX"]["close"][i]
        soxx_20 = asset_series["SOXX"]["sma20"][i]
        soxx_50 = asset_series["SOXX"]["sma50"][i]
        eth_c = asset_series["ETH-USD"]["close"][i]
        eth_20 = asset_series["ETH-USD"]["sma20"][i]

        igv_weak = bool(igv_c and igv_20 and igv_50 and igv_c < igv_20 and igv_c < igv_50)
        soxx_weak = bool(soxx_c and soxx_20 and soxx_50 and soxx_c < soxx_20 and soxx_c < soxx_50)
        eth_weak = bool(eth_c and eth_20 and eth_c < eth_20)
        breaker_c = bool((igv_weak or soxx_weak) and eth_weak)

        # 仓位上限计算
        if total_score >= 80:
            raw_pos_max = 100
        elif total_score >= 60:
            raw_pos_max = 70
        elif total_score >= 40:
            raw_pos_max = 40
        else:
            raw_pos_max = 20

        pos_cap = raw_pos_max
        if breaker_a:
            pos_cap = min(pos_cap, 30)
        if breaker_b:
            pos_cap = min(pos_cap, 50)

        history.append({
            "date": d,
            "btc": btc_c,
            "eth": eth_c,
            "sol": asset_series["SOL-USD"]["close"][i],
            "total": total_score,
            "slow": slow_score,
            "fast": fast_score,
            "tech": tech_score,
            "pos_cap": pos_cap,
            "breaker_a": breaker_a,
            "breaker_b": breaker_b,
            "breaker_c": breaker_c,
            "detail": {
                "cpi": s_cpi, "rate_exp": s_rate_exp, "fed": s_fed, "hy_oas": s_hy,
                "etf": s_etf, "crypto_stocks": s_crypto_stocks, "us_tech": s_tech,
                "yield_vel": s_yield_vel, "net_liq": s_net_liq,
                "ma20": s_ma20, "bmsb": s_bmsb, "ma200": s_ma200,
            },
            "raw": {
                "cpi_3m_ann": round(cpi_v, 2) if cpi_v is not None else None,
                "spread_2y_ff": round(spread_2y_ff, 2),
                "hy_oas": hy_cur,
                "etf_5d": f5,
                "etf_10d": f10,
                "cs_above20": f"{cs_above20}/{cs_valid}",
                "avg_y_chg_bp": round(avg_y_chg_bp, 1),
                "nl_chg_b": round(nl_chg, 1),
                "btc_20d": round(btc_20, 2) if btc_20 else None,
                "btc_bmsb_lo": round(min(bms_s, bms_e), 2) if (bms_s and bms_e) else None,
                "btc_bmsb_hi": round(max(bms_s, bms_e), 2) if (bms_s and bms_e) else None,
                "btc_200d": round(btc_200, 2) if btc_200 else None,
            }
        })

    # 生成最新一日的 11 标的均线监控矩阵
    last_idx = len(all_dates) - 1
    assets_matrix = []
    for sym, meta in SYMBOLS.items():
        s = asset_series[sym]
        c = s["close"][last_idx]
        c_prev = s["close"][max(0, last_idx - 1)]
        c_5 = s["close"][max(0, last_idx - 5)]
        m20 = s["sma20"][last_idx]
        m50 = s["sma50"][last_idx]
        m200 = s["sma200"][last_idx]
        bs = s["bmsb_sma20w"][last_idx]
        be = s["bmsb_ema21w"][last_idx]
        b_lo = min(bs, be) if (bs and be) else None
        b_hi = max(bs, be) if (bs and be) else None

        if c and b_lo and b_hi:
            bmsb_pos = "上方" if c > b_hi else ("带内" if c >= b_lo else "下方")
        else:
            bmsb_pos = "--"

        assets_matrix.append({
            "symbol": sym,
            "name": meta["name"],
            "group": meta["group"],
            "price": round(c, 2) if c else None,
            "chg_1d": round((c / c_prev - 1) * 100, 2) if (c and c_prev) else None,
            "chg_5d": round((c / c_5 - 1) * 100, 2) if (c and c_5) else None,
            "vs_20d": round((c / m20 - 1) * 100, 2) if (c and m20) else None,
            "vs_50d": round((c / m50 - 1) * 100, 2) if (c and m50) else None,
            "vs_200d": round((c / m200 - 1) * 100, 2) if (c and m200) else None,
            "bmsb_pos": bmsb_pos,
            "sma20": round(m20, 2) if m20 else None,
            "sma200": round(m200, 2) if m200 else None,
        })

    payload = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_date": history[-1]["date"],
        "fed_stance": fed_stance,
        "latest": history[-1],
        "assets_matrix": assets_matrix,
        "history": history,
    }

    with open(OUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(OUT_JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.QUANT_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    lt = history[-1]
    print("\n" + "=" * 60)
    print(f"最新日期: {lt['date']} | 总分: {lt['total']} / 100 | 建议仓位上限: <= {lt['pos_cap']}%")
    print(f"分项得分: 慢变量 {lt['slow']}/35 | 快变量 {lt['fast']}/40 | 技术面 {lt['tech']}/25")
    print(f"熔断状态: A(趋势破位)={lt['breaker_a']} | B(资金出逃)={lt['breaker_b']} | C(IGV/SOXX+ETH共振)={lt['breaker_c']}")
    print("=" * 60)


if __name__ == "__main__":
    build_all()
