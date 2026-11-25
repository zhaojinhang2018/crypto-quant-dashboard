#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加密市场量化风控打分系统 V2 (零未来函数回测 + IC校准权重 + 防打脸滞后缓冲)
数据源（全部免费公开接口）：
1. Yahoo Finance: BTC-USD, ETH-USD, SOL-USD, MSTR, BMNR, COIN, CRCL, HOOD, QQQ, IGV, SOXX
2. FRED (圣路易斯联储): CPILFESL (滞后45天对齐真实发布日), EFFR, DGS2, DGS10, BAMLH0A0HYM2, WALCL, WTREGEN, RRPONTSYD
3. Farside Investors: 美国 BTC 现货 ETF & ETH 现货 ETF 每日净流入/流出
4. 本地配置文件: fed_stance.json (仅作用于今日实时打分；历史回测固定使用中性4/8避免静态偏差)
"""

import datetime
import json
import os
import shutil
import subprocess
import time
from html.parser import HTMLParser

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
FED_STANCE_PATH = os.path.join(WORKSPACE_DIR, "fed_stance.json")
OUT_JS_PATH = os.path.join(WORKSPACE_DIR, "quant_data.js")
OUT_JSON_PATH = os.path.join(WORKSPACE_DIR, "quant_data.json")
FALLBACK_BTC_ETF_JSON = os.path.join(WORKSPACE_DIR, "btc_etf_data.json")

START_DATE = "2023-06-01"      # 多拉半年用于计算 200D / 20W 均线
BACKTEST_START = "2024-01-11"  # BTC 现货 ETF 上市首日起回测
CPI_RELEASE_LAG_DAYS = 45      # 美国核心 CPI 月度数据实际公布滞后约 45 天

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

# V2 权重配置 (慢变量35 + 快变量40 + 技术面25 = 100)
WEIGHTS = {
    "rate_exp": 14.0,
    "cpi": 8.0,
    "fed": 8.0,
    "hy_oas": 5.0,
    "etf": 15.0,
    "crypto_stocks": 11.0,
    "us_tech": 6.0,
    "net_liq": 4.0,
    "yield_vel": 4.0,
    "ma20": 9.0,
    "bmsb": 9.0,
    "ma200": 7.0,
}

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
    for _ in range(3):
        try:
            raw_text = curl_get(url, timeout=25, use_ua=True)
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
    for _ in range(3):
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
    html = curl_get(url, timeout=25, use_ua=True)
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


def compute_bmsb_causal(dates, closes):
    """
    严格无未来函数（因果）计算 Bull Market Support Band (20W SMA & 21W EMA)：
    对任意日期 d，仅使用「之前已收盘的完整 ISO 周」+「当周截至日期 d 的最新收盘价」，
    绝不偷看当周后续几天的收盘价。
    """
    completed_weeks = []   # 已完整结束的周收盘价列表
    curr_wk = None
    curr_wk_last_close = None
    ema_prev_completed = None
    k = 2.0 / (21.0 + 1.0)

    sma20_daily = []
    ema21_daily = []

    for d, c in zip(dates, closes):
        if c is None:
            sma20_daily.append(sma20_daily[-1] if sma20_daily else None)
            ema21_daily.append(ema21_daily[-1] if ema21_daily else None)
            continue

        dt = datetime.datetime.strptime(d, "%Y-%m-%d")
        iso = dt.isocalendar()
        wk = f"{iso[0]}-W{iso[1]:02d}"

        if curr_wk is not None and wk != curr_wk and curr_wk_last_close is not None:
            # 上一周正式收盘，写入 completed_weeks 并推进已完成周的 EMA21
            completed_weeks.append(curr_wk_last_close)
            if len(completed_weeks) == 21:
                ema_prev_completed = sum(completed_weeks) / 21.0
            elif len(completed_weeks) > 21 and ema_prev_completed is not None:
                ema_prev_completed = curr_wk_last_close * k + ema_prev_completed * (1.0 - k)

        curr_wk = wk
        curr_wk_last_close = c

        # 计算截至当日 d 的实时周线序列（前 N 个已完成周 + 当日价格作为本周临时收盘）
        if len(completed_weeks) >= 19:
            s20 = (sum(completed_weeks[-19:]) + c) / 20.0
        else:
            s20 = None

        if len(completed_weeks) == 20:
            e21 = (sum(completed_weeks) + c) / 21.0
        elif len(completed_weeks) >= 21 and ema_prev_completed is not None:
            e21 = c * k + ema_prev_completed * (1.0 - k)
        else:
            e21 = None

        sma20_daily.append(s20)
        ema21_daily.append(e21)

    return sma20_daily, ema21_daily


def compute_core_cpi_3m_ann_lagged(dates, cpi_raw):
    """
    计算核心 CPI 近3个月折年率 (%)，并将日期向后平移 45 天对齐真实 BLS 发布日，消除回测未来函数。
    """
    months = sorted(cpi_raw.keys())
    ann_map = {}
    for i in range(3, len(months)):
        cur = cpi_raw[months[i]]
        prev3 = cpi_raw[months[i - 3]]
        if prev3 > 0:
            dt = datetime.datetime.strptime(months[i], "%Y-%m-%d")
            avail_dt = (dt + datetime.timedelta(days=CPI_RELEASE_LAG_DAYS)).strftime("%Y-%m-%d")
            ann_map[avail_dt] = ((cur / prev3) ** 4 - 1.0) * 100.0
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


def next_hysteresis_tier(prev_tier, score):
    """
    防打脸滞后缓冲区 (±3分)：
    仓位档位：100 (绿灯>=80) / 70 (蓝灯>=60) / 40 (黄灯>=40) / 20 (红灯<40)
    升级需额外高出 3 分，降级需额外跌破 3 分，避免在阈值附近反复横跳。
    """
    if prev_tier is None:
        return 100 if score >= 80 else (70 if score >= 60 else (40 if score >= 40 else 20))

    tier = prev_tier
    # 向上升级判定（允许跨档）
    if tier < 40 and score >= 43:
        tier = 40
    if tier < 70 and score >= 63:
        tier = 70
    if tier < 100 and score >= 83:
        tier = 100

    # 向下顺延降级判定（允许跨档）
    if tier > 70 and score <= 77:
        tier = 70
    if tier > 40 and score <= 57:
        tier = 40
    if tier > 20 and score <= 37:
        tier = 20

    return tier


def build_all():
    print("[1/4] 正在拉取 Yahoo Finance 行情数据（3 币 + 5 币股 + 3 美股科技指数）...")
    yf_data = {}
    for sym in SYMBOLS:
        yf_data[sym] = fetch_yahoo_daily(sym)
        print(f"      {sym:8s} -> {len(yf_data[sym])} 条日线 (最新: {max(yf_data[sym].keys())})")

    print("[2/4] 正在拉取 FRED 宏观与流动性数据...")
    fred_data = {}
    for fid in FRED_SERIES:
        fred_data[fid] = fetch_fred_series(fid)
        print(f"      {fid:12s} -> {len(fred_data[fid])} 条 (最新: {max(fred_data[fid].keys())})")

    print("[3/4] 正在拉取 Farside BTC/ETH 现货 ETF 资金流向...")
    btc_etf = load_btc_etf_flows()
    eth_etf = load_eth_etf_flows()
    print(f"      BTC ETF: {len(btc_etf)} 日 (最新: {max(btc_etf.keys()) if btc_etf else '--'}) | ETH ETF: {len(eth_etf)} 日 (最新: {max(eth_etf.keys()) if eth_etf else '--'})")

    fed_stance = load_fed_stance()
    all_dates = sorted(d for d in yf_data["BTC-USD"].keys() if d >= START_DATE)

    # 对齐所有资产，并分别保存美股自身交易日的真实 1D/5D 涨跌（修复周末显示 0% Bug）
    asset_series = {}
    asset_native_latest = {}
    for sym in SYMBOLS:
        closes = align_forward_fill(all_dates, yf_data[sym])
        sym_dates = sorted(d for d in yf_data[sym].keys() if d >= START_DATE)
        sym_closes = [yf_data[sym][d] for d in sym_dates]

        if SYMBOLS[sym]["group"] != "crypto":
            s20_raw = compute_sma(sym_closes, 20)
            s50_raw = compute_sma(sym_closes, 50)
            s200_raw = compute_sma(sym_closes, 200)
            bms_s_raw, bms_e_raw = compute_bmsb_causal(sym_dates, sym_closes)

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
            bms_s, bms_e = compute_bmsb_causal(all_dates, closes)
            ret10 = [None] * len(all_dates)
            for i in range(10, len(all_dates)):
                if closes[i] and closes[i - 10]:
                    ret10[i] = (closes[i] / closes[i - 10] - 1.0) * 100.0

        # 原生交易日最新 1D / 5D 涨跌
        n_c = sym_closes[-1] if sym_closes else None
        n_prev1 = sym_closes[-2] if len(sym_closes) >= 2 else None
        n_prev5 = sym_closes[-6] if len(sym_closes) >= 6 else None
        asset_native_latest[sym] = {
            "last_date": sym_dates[-1] if sym_dates else "--",
            "chg_1d": round((n_c / n_prev1 - 1.0) * 100.0, 2) if (n_c and n_prev1) else None,
            "chg_5d": round((n_c / n_prev5 - 1.0) * 100.0, 2) if (n_c and n_prev5) else None,
        }

        asset_series[sym] = {
            "close": closes,
            "sma20": s20,
            "sma50": s50,
            "sma200": s200,
            "bmsb_sma20w": bms_s,
            "bmsb_ema21w": bms_e,
            "ret10": ret10,
        }

    # 对齐宏观指标（CPI 已强制滞后 45 天消除未来函数）
    cpi_3m_ann = compute_core_cpi_3m_ann_lagged(all_dates, fred_data["CPILFESL"])
    effr = align_forward_fill(all_dates, fred_data["EFFR"])
    dgs2 = align_forward_fill(all_dates, fred_data["DGS2"])
    dgs10 = align_forward_fill(all_dates, fred_data["DGS10"])
    hy_oas = align_forward_fill(all_dates, fred_data["BAMLH0A0HYM2"])

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
    etf_5d_map, etf_10d_map, etf_neg_streak_map = {}, {}, {}
    neg_streak = 0
    combined_flows = []
    for td in etf_trade_dates:
        tot = (btc_etf.get(td) or 0.0) + (eth_etf.get(td) or 0.0)
        combined_flows.append((td, tot))
        neg_streak = (neg_streak + 1) if tot < 0 else (0 if tot > 0 else neg_streak)
        etf_neg_streak_map[td] = neg_streak
    for i, (td, _) in enumerate(combined_flows):
        w5 = [x[1] for x in combined_flows[max(0, i - 4) : i + 1]]
        w10 = [x[1] for x in combined_flows[max(0, i - 9) : i + 1]]
        etf_5d_map[td] = round(sum(w5), 2)
        etf_10d_map[td] = round(sum(w10), 2)

    etf_5d = align_forward_fill(all_dates, etf_5d_map)
    etf_10d = align_forward_fill(all_dates, etf_10d_map)
    etf_streak = align_forward_fill(all_dates, etf_neg_streak_map)

    print("[4/4] 正在计算 V2 每日量化评分、防打脸滞后仓位与无未来函数回测...")
    fed_level_live = int(fed_stance.get("level", 0))
    fed_score_live = clamp((fed_level_live + 2) * 2.0, 0.0, 8.0)  # -2->0 .. +2->8
    fed_score_hist_neutral = 4.0  # 历史回测统一用中性 4/8，避免静态泄露

    history = []
    btc_break_streak = 0
    brk_b_streak = 0
    brk_c_streak = 0
    prev_hyst_tier = None

    for i, d in enumerate(all_dates):
        if d < BACKTEST_START:
            continue

        # --- 一、慢变量 (35分: rate_exp 14 + cpi 8 + fed 8 + hy_oas 5) ---
        # 1. 债市加减息预期 DGS2 - EFFR (14分)
        spread_2y_ff = (dgs2[i] - effr[i]) if (dgs2[i] is not None and effr[i] is not None) else 0.0
        if spread_2y_ff <= -0.75:
            s_rate_exp = 14.0
        elif spread_2y_ff >= 0.50:
            s_rate_exp = 1.0
        else:
            s_rate_exp = round(1.0 + 13.0 * (0.50 - spread_2y_ff) / 1.25, 2)

        # 2. 核心 CPI 3M 年化 (8分, 已滞后45天)
        cpi_v = cpi_3m_ann[i]
        if cpi_v is None:
            s_cpi = 4.0
        elif cpi_v <= 2.2:
            s_cpi = 8.0
        elif cpi_v >= 4.5:
            s_cpi = 0.0
        else:
            s_cpi = round(8.0 * (4.5 - cpi_v) / (4.5 - 2.2), 2)

        # 3. Fed 官员口风 (8分: 历史回测中性 4.0)
        s_fed = fed_score_hist_neutral

        # 4. 信用利差 HY OAS (5分)
        hy_cur = hy_oas[i]
        hy_prev20 = hy_oas[max(0, i - 20)]
        hy_chg = (hy_cur - hy_prev20) if (hy_cur is not None and hy_prev20 is not None) else 0.0
        if hy_cur is None:
            s_hy = 3.0
        else:
            lvl_score = clamp(3.5 * (5.2 - hy_cur) / (5.2 - 3.0), 0.0, 3.5)
            chg_score = clamp(1.5 * (0.50 - hy_chg) / 0.60, 0.0, 1.5)
            s_hy = round(lvl_score + chg_score, 2)

        slow_score = round(s_rate_exp + s_cpi + s_fed + s_hy, 2)

        # --- 二、快变量 (40分: etf 15 + crypto_stocks 11 + us_tech 6 + net_liq 4 + yield_vel 4) ---
        # 5. ETF 5D/10D 净流入 (15分)
        f5 = etf_5d[i] or 0.0
        f10 = etf_10d[i] or 0.0
        streak = etf_streak[i] or 0
        s_etf_base = clamp(7.5 + (f5 / 1200.0) * 6.0 + (f10 / 3000.0) * 1.5, 0.0, 15.0)
        if streak >= 4:
            s_etf_base = max(0.0, s_etf_base - 2.5)
        s_etf = round(s_etf_base, 2)

        # 6. 币股篮子风向 MSTR/BMNR/COIN/CRCL/HOOD (11分)
        cs_syms = ["MSTR", "BMNR", "COIN", "CRCL", "HOOD"]
        cs_valid, cs_above20, cs_rets = 0, 0, []
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
        s_crypto_stocks = round(cs_ratio * 7.0 + clamp((cs_rel_qqq + 10.0) / 20.0 * 4.0, 0.0, 4.0), 2)

        # 7. 美股科技 & 高Beta成长 QQQ + IGV + SOXX (6分)
        q_s = score_asset_ma(asset_series["QQQ"]["close"][i], asset_series["QQQ"]["sma20"][i], asset_series["QQQ"]["sma50"][i])
        igv_s = score_asset_ma(asset_series["IGV"]["close"][i], asset_series["IGV"]["sma20"][i], asset_series["IGV"]["sma50"][i])
        soxx_s = score_asset_ma(asset_series["SOXX"]["close"][i], asset_series["SOXX"]["sma20"][i], asset_series["SOXX"]["sma50"][i])
        s_tech = round((q_s or 0.5) * 2.0 + (igv_s or 0.5) * 2.0 + (soxx_s or 0.5) * 2.0, 2)

        # 8. 美元净流动性 28日变化 (4分)
        nl_cur = net_liq[i]
        nl_prev28 = net_liq[max(0, i - 28)]
        nl_chg = (nl_cur - nl_prev28) if (nl_cur is not None and nl_prev28 is not None) else 0.0
        s_net_liq = round(clamp(2.0 + (nl_chg / 150.0) * 2.0, 0.0, 4.0), 2)

        # 9. 2Y & 10Y 美债收益率 14日斜率 (4分)
        d2_chg = (dgs2[i] - dgs2[max(0, i - 14)]) if (dgs2[i] is not None and dgs2[max(0, i - 14)] is not None) else 0.0
        d10_chg = (dgs10[i] - dgs10[max(0, i - 14)]) if (dgs10[i] is not None and dgs10[max(0, i - 14)] is not None) else 0.0
        avg_y_chg_bp = ((d2_chg + d10_chg) / 2.0) * 100.0
        s_yield_vel = round(clamp(2.3 - avg_y_chg_bp / 15.0, 0.0, 4.0), 2)

        fast_score = round(s_etf + s_crypto_stocks + s_tech + s_net_liq + s_yield_vel, 2)

        # --- 三、BTC 技术面均线 (25分: ma20 9 + bmsb 9 + ma200 7) ---
        btc_c = asset_series["BTC-USD"]["close"][i]
        btc_20 = asset_series["BTC-USD"]["sma20"][i]
        btc_20_prev = asset_series["BTC-USD"]["sma20"][max(0, i - 5)]
        btc_200 = asset_series["BTC-USD"]["sma200"][i]
        bms_s = asset_series["BTC-USD"]["bmsb_sma20w"][i]
        bms_e = asset_series["BTC-USD"]["bmsb_ema21w"][i]

        # 10. BTC vs 20D SMA (9分)
        if btc_c and btc_20:
            if btc_c > btc_20:
                s_ma20 = 9.0 if (btc_20_prev and btc_20 >= btc_20_prev) else 6.5
            elif btc_c > btc_20 * 0.98:
                s_ma20 = 3.0
            else:
                s_ma20 = 0.0
        else:
            s_ma20 = 4.5

        # 11. BTC vs 牛熊支撑带 BMSB (9分, 严格因果)
        if btc_c and bms_s and bms_e:
            band_hi = max(bms_s, bms_e)
            band_lo = min(bms_s, bms_e)
            if btc_c > band_hi:
                s_bmsb = 9.0
            elif btc_c >= band_lo:
                s_bmsb = 4.5
            else:
                s_bmsb = 0.0
        else:
            band_lo = None
            s_bmsb = 4.5

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

        # --- 三道硬风控熔断（全部带连续确认去噪） ---
        below_bmsb_or_200 = False
        if btc_c:
            cond_bmsb = (band_lo is not None and btc_c < band_lo)
            cond_200 = (btc_200 is not None and btc_c < btc_200)
            below_bmsb_or_200 = cond_bmsb or cond_200
        btc_break_streak = (btc_break_streak + 1) if below_bmsb_or_200 else 0
        breaker_a = btc_break_streak >= 3

        raw_brk_b = bool(btc_c and btc_20 and btc_c < btc_20 and (f5 < 0 or (cs_valid > 0 and cs_above20 <= 1)))
        brk_b_streak = (brk_b_streak + 1) if raw_brk_b else 0
        breaker_b = brk_b_streak >= 2

        igv_c, igv_20, igv_50 = asset_series["IGV"]["close"][i], asset_series["IGV"]["sma20"][i], asset_series["IGV"]["sma50"][i]
        soxx_c, soxx_20, soxx_50 = asset_series["SOXX"]["close"][i], asset_series["SOXX"]["sma20"][i], asset_series["SOXX"]["sma50"][i]
        eth_c, eth_20 = asset_series["ETH-USD"]["close"][i], asset_series["ETH-USD"]["sma20"][i]
        igv_weak = bool(igv_c and igv_20 and igv_50 and igv_c < igv_20 and igv_c < igv_50)
        soxx_weak = bool(soxx_c and soxx_20 and soxx_50 and soxx_c < soxx_20 and soxx_c < soxx_50)
        eth_weak = bool(eth_c and eth_20 and eth_c < eth_20)
        raw_brk_c = bool((igv_weak or soxx_weak) and eth_weak)
        brk_c_streak = (brk_c_streak + 1) if raw_brk_c else 0
        breaker_c = brk_c_streak >= 2

        # 防打脸滞后缓冲区仓位档位 + 熔断压制
        hyst_tier = next_hysteresis_tier(prev_hyst_tier, total_score)
        prev_hyst_tier = hyst_tier

        pos_cap = hyst_tier
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
            "hyst_tier": hyst_tier,
            "pos_cap": pos_cap,
            "breaker_a": breaker_a,
            "breaker_b": breaker_b,
            "breaker_c": breaker_c,
            "detail": {
                "rate_exp": s_rate_exp, "cpi": s_cpi, "fed": s_fed, "hy_oas": s_hy,
                "etf": s_etf, "crypto_stocks": s_crypto_stocks, "us_tech": s_tech,
                "net_liq": s_net_liq, "yield_vel": s_yield_vel,
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

    # 今日实时视图：叠加 fed_stance.json 手动档位（历史回测保持中性 4.0 无泄露）
    latest_live = json.loads(json.dumps(history[-1]))
    delta_fed = fed_score_live - fed_score_hist_neutral
    latest_live["detail"]["fed"] = fed_score_live
    latest_live["slow"] = round(latest_live["slow"] + delta_fed, 2)
    latest_live["total"] = round(latest_live["slow"] + latest_live["fast"] + latest_live["tech"], 1)
    live_tier = next_hysteresis_tier(
        history[-2]["hyst_tier"] if len(history) >= 2 else None,
        latest_live["total"],
    )
    latest_live["hyst_tier"] = live_tier
    live_cap = live_tier
    if latest_live["breaker_a"]:
        live_cap = min(live_cap, 30)
    if latest_live["breaker_b"]:
        live_cap = min(live_cap, 50)
    latest_live["pos_cap"] = live_cap

    # 生成最新一日 11 标的均线监控矩阵（使用美股原生交易日涨跌，修复周末 0% Bug）
    last_idx = len(all_dates) - 1
    assets_matrix = []
    for sym, meta in SYMBOLS.items():
        s = asset_series[sym]
        c = s["close"][last_idx]
        m20, m50, m200 = s["sma20"][last_idx], s["sma50"][last_idx], s["sma200"][last_idx]
        bs, be = s["bmsb_sma20w"][last_idx], s["bmsb_ema21w"][last_idx]
        b_lo = min(bs, be) if (bs and be) else None
        b_hi = max(bs, be) if (bs and be) else None
        bmsb_pos = ("上方" if c > b_hi else ("带内" if c >= b_lo else "下方")) if (c and b_lo and b_hi) else "--"

        native = asset_native_latest[sym]
        assets_matrix.append({
            "symbol": sym,
            "name": meta["name"],
            "group": meta["group"],
            "last_date": native["last_date"],
            "price": round(c, 2) if c else None,
            "chg_1d": native["chg_1d"],
            "chg_5d": native["chg_5d"],
            "vs_20d": round((c / m20 - 1) * 100, 2) if (c and m20) else None,
            "vs_50d": round((c / m50 - 1) * 100, 2) if (c and m50) else None,
            "vs_200d": round((c / m200 - 1) * 100, 2) if (c and m200) else None,
            "bmsb_pos": bmsb_pos,
            "sma20": round(m20, 2) if m20 else None,
            "sma200": round(m200, 2) if m200 else None,
        })

    # 各数据源实际最新日期清单（透明度与滞后监控）
    data_freshness = [
        {"source": "加密货币行情 (BTC/ETH/SOL)", "latest_date": max(yf_data["BTC-USD"].keys()), "note": "Yahoo Finance 7x24"},
        {"source": "美股与科技ETF (MSTR/QQQ/IGV/SOXX)", "latest_date": max(yf_data["QQQ"].keys()), "note": "美股交易日收盘"},
        {"source": "BTC 现货 ETF 资金流", "latest_date": max(btc_etf.keys()) if btc_etf else "--", "note": "Farside (T+1早间更新)"},
        {"source": "ETH 现货 ETF 资金流", "latest_date": max(eth_etf.keys()) if eth_etf else "--", "note": "Farside (T+1早间更新)"},
        {"source": "美债利率与信用利差 (FRED)", "latest_date": max(fred_data["DGS2"].keys()), "note": "DGS2/DGS10/EFFR/HY OAS"},
        {"source": "美元净流动性 (FRED 周度)", "latest_date": max(fred_data["WALCL"].keys()), "note": "每周四晚更新"},
        {"source": "核心 CPI (FRED 月度)", "latest_date": max(fred_data["CPILFESL"].keys()), "note": "回测已滞后45天对齐发布日"},
    ]

    payload = {
        "version": "2.0",
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_date": latest_live["date"],
        "weights": WEIGHTS,
        "fed_stance": fed_stance,
        "data_freshness": data_freshness,
        "latest": latest_live,
        "assets_matrix": assets_matrix,
        "history": history,
    }

    with open(OUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(OUT_JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.QUANT_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    print("\n" + "=" * 64)
    print(f"[V2] 最新日期: {latest_live['date']} | 总分: {latest_live['total']} / 100 | 建议仓位上限: <= {latest_live['pos_cap']}%")
    print(f"     分项得分: 慢变量 {latest_live['slow']}/35 | 快变量 {latest_live['fast']}/40 | 技术面 {latest_live['tech']}/25")
    print(f"     熔断状态: A(3日趋势破位)={latest_live['breaker_a']} | B(2日资金出逃)={latest_live['breaker_b']} | C(2日科技+ETH共振)={latest_live['breaker_c']}")
    print("=" * 64)


if __name__ == "__main__":
    build_all()
