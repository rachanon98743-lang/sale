#!/usr/bin/env python3
"""
scanner.py — ฝ่ายสแกนหุ้นมืออาชีพ + Gemini (ไม่ต้องใช้ Webull API)

ทำอะไร
  1) ดึงราคาย้อนหลัง 2 ปี + ข้อมูลพื้นฐาน (งบ, ปันผล, เป้านักวิเคราะห์) ของหุ้น/ETF ใน universe.py จาก Yahoo Finance ฟรี
  2) คัดแบบนักวิเคราะห์มืออาชีพ แยก 3 กลุ่ม
       ⚡ เทรดเน้นกำไร  : ย่อในขาขึ้น / ทะลุฐาน / รอทะลุ  → โซนซื้อ, จุดตัดขาดทุน, เป้าขาย 1-2
       🌱 ถือยาว เน้นเติบโต : รายได้โต กำไรดี ราคายังขาขึ้น → โซนสะสม, เป้าราคา, จุดเลิกถือ
       💰 ถือยาว เน้นปันผล : ปันผลสูง จ่ายสม่ำเสมอ ไม่จ่ายเกินกำไร → โซนสะสม, ปันผลที่จะได้, จุดเลิกถือ
  3) ส่งตัวที่ผ่านเกณฑ์ให้ Gemini (ค้นข่าวล่าสุดด้วย Google Search) ตัดสินแบบผู้จัดการกองทุน: ซื้อ / รอ / ไม่ควรแตะ
  4) ส่งสรุปเข้า LINE วันละ 1 ข้อความ (หลังตลาด US ปิด) + เปิดหน้าเว็บดูละเอียดที่พอร์ต 8082
  5) ติดตามตัวที่เคยแนะนำ: เข้าโซนซื้อ / ถึงเป้าขาย / หลุดจุดตัดขาดทุน แจ้งในข้อความวันถัดไป + เก็บสถิติอัตราชนะ

รัน
    python scanner.py            # บริการ: เว็บ + สแกนเองทุกวันตามเวลาใน config.json → "scanner"
    python scanner.py --once     # สแกนเดี๋ยวนี้ 1 ครั้ง แล้วส่ง LINE
    python scanner.py --once --no-line
ไม่ใช่คำแนะนำการลงทุน: ทุกตัวเลขเป็นการประเมินจากข้อมูลย้อนหลัง ตัดสินใจและรับความเสี่ยงเอง
"""

import argparse
import base64
import hmac
import html
import json
import math
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from universe import UNIVERSE, BENCHMARK  # noqa: E402
from notifier import LineNotifier  # noqa: E402

TH = timezone(timedelta(hours=7))
REPORT = os.path.join(HERE, "scan_report.json")
PICKS = os.path.join(HERE, "scan_picks.json")
FUND = os.path.join(HERE, "scan_fundamentals.json")
STATE = os.path.join(HERE, "scan_state.json")

DEFAULT = {
    "scan_time_th": "07:40",        # หลังตลาด US ปิด (ปิด 03:00/04:00 เวลาไทย) ข้อมูลครบแล้ว
    "scan_days": [1, 2, 3, 4, 5],   # อังคาร-เสาร์ เวลาไทย = หลังวันทำการ จันทร์-ศุกร์ ของ US
    "port": 8082,
    "line": True,
    "max_trade": 4, "max_growth": 4, "max_dividend": 4,
    "min_dividend_yield": 2.5,      # %
    "min_revenue_growth": 12,       # %
    "min_dollar_volume_m": 40,      # มูลค่าซื้อขายเฉลี่ย/วัน (ล้าน USD)
    "account_usd": 0,               # ใส่ทุน (USD) เพื่อให้คำนวณขนาดไม้ให้ (0 = ไม่คำนวณ)
    "risk_per_trade_pct": 1,
    "track_trade_days": 20,         # ติดตามไม้เทรดนานสุดกี่วันทำการ
    "track_long_days": 180,
    "gemini": True,
    "gemini_model": "gemini-flash-latest",
    "gemini_max_calls_day": 8,
    "gemini_max_candidates": 18,
    "manual_scan_cooldown_min": 20,
}

_lock = threading.Lock()


# ===================================================================== ทั่วไป
def now_th():
    return datetime.now(TH)


def load_cfg():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        c = {}
    out = dict(DEFAULT)
    out.update(c.get("scanner") or {})
    for s, sec in (c.get("extra_symbols") or {}).items():
        UNIVERSE[s.upper()] = (sec, "stock")
    return out


def jload(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jsave(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, default=str)
    os.replace(tmp, p)


def log(msg):
    print("[%s] สแกนหุ้น: %s" % (now_th().strftime("%H:%M:%S"), msg), flush=True)


def num(x, default=None):
    try:
        x = float(x)
        return default if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return default


def r2(x):
    return None if x is None else round(float(x), 2)


def money(x):
    if x is None:
        return "-"
    return ("%.2f" % x) if x < 1000 else ("{:,.0f}".format(x))


THAI_MONTH = "ม.ค. ก.พ. มี.ค. เม.ย. พ.ค. มิ.ย. ก.ค. ส.ค. ก.ย. ต.ค. พ.ย. ธ.ค.".split()


def thai_date(s):
    try:
        d = datetime.strptime(str(s)[:10], "%Y-%m-%d")
        return "%d %s" % (d.day, THAI_MONTH[d.month - 1])
    except Exception:
        return str(s)


# ===================================================================== ข้อมูล
def fetch_prices(symbols):
    import yfinance as yf
    df = yf.download(symbols, period="2y", interval="1d", group_by="ticker", auto_adjust=True,
                     actions=True, threads=True, progress=False, timeout=30)
    out = {}
    for s in symbols:
        try:
            d = df[s]
        except KeyError:
            continue
        d = d.dropna(subset=["Close", "High", "Low"])
        if len(d) >= 210:
            out[s] = d
    return out


FKEYS = ("shortName", "sector", "industry", "marketCap", "trailingPE", "forwardPE", "revenueGrowth",
         "earningsGrowth", "profitMargins", "returnOnEquity", "debtToEquity", "freeCashflow", "payoutRatio",
         "trailingAnnualDividendYield", "fiveYearAvgDividendYield", "beta", "targetMeanPrice",
         "recommendationKey", "numberOfAnalystOpinions")


def fetch_fundamentals(symbols):
    """ข้อมูลพื้นฐานเปลี่ยนช้า: เก็บแคชไว้ 3 วัน"""
    import yfinance as yf
    cache = jload(FUND, {})
    old = (now_th() - timedelta(days=3)).strftime("%Y-%m-%d")
    today = now_th().strftime("%Y-%m-%d")
    need = [s for s in symbols if cache.get(s, {}).get("_d", "") <= old]
    for i, s in enumerate(need):
        try:
            t = yf.Ticker(s)
            info = t.info or {}
            f = {k: info.get(k) for k in FKEYS}
            try:
                cal = t.calendar or {}
                f["earnings"] = [str(x) for x in (cal.get("Earnings Date") or [])][:2]
            except Exception:
                f["earnings"] = []
            f["_d"] = today
            cache[s] = f
        except Exception as e:
            log("ข้อมูลพื้นฐาน %s ไม่ได้: %s" % (s, str(e)[:80]))
        if i % 20 == 19:
            jsave(FUND, cache)
        time.sleep(0.2)
    jsave(FUND, cache)
    return cache


def next_earnings_days(f):
    today = now_th().date()
    for e in (f or {}).get("earnings") or []:
        try:
            d = datetime.strptime(e[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= today:
            return (d - today).days, e[:10]
    return None, None


# ===================================================================== วิเคราะห์ทางเทคนิค
def indicators(d):
    import pandas as pd
    c, h, lo, v = d["Close"], d["High"], d["Low"], d["Volume"]
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    sma200 = c.rolling(200).mean()
    C = float(c.iloc[-1])

    def ret(n):
        return (C / float(c.iloc[-n - 1]) - 1) * 100 if len(c) > n else 0.0
    divs = d["Dividends"] if "Dividends" in d else None
    vol50 = float(v.iloc[-51:-1].mean() or 0)
    return {
        "date": str(d.index[-1])[:10], "close": C, "open": float(d["Open"].iloc[-1]),
        "high": float(h.iloc[-1]), "low": float(lo.iloc[-1]),
        "sma50": float(c.rolling(50).mean().iloc[-1]), "sma200": float(sma200.iloc[-1]),
        "sma200_up": float(sma200.iloc[-1]) > float(sma200.iloc[-22]),
        "ema20": float(c.ewm(span=20, adjust=False).mean().iloc[-1]),
        "rsi": float(100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1])) if dn.iloc[-1] else 100.0,
        "atr": float(tr.rolling(14).mean().iloc[-1]),
        "ret21": ret(21), "ret63": ret(63), "ret126": ret(126), "ret252": ret(min(252, len(c) - 1)),
        "hi52": float(h.iloc[-252:].max()), "lo52": float(lo.iloc[-252:].min()),
        "hi20": float(h.iloc[-21:-1].max()), "lo20": float(lo.iloc[-21:-1].min()),
        "low5": float(lo.iloc[-5:].min()),
        "vol_ratio": float(v.iloc[-1]) / vol50 if vol50 else 1.0,
        "dollar_vol_m": float((c * v).iloc[-20:].mean()) / 1e6,
        "div_ttm": float(divs.iloc[-252:].sum()) if divs is not None else 0.0,
        "div_count_2y": int((divs > 0).sum()) if divs is not None else 0,
    }


def market_regime(spy, vix):
    if not spy:
        return {"state": "neutral", "label": "ไม่ทราบ", "icon": "⚪", "vix": vix}
    up = spy["close"] > spy["sma200"] and spy["sma50"] > spy["sma200"]
    down = spy["close"] < spy["sma200"] and spy["sma50"] < spy["sma200"]
    st = "bull" if up else ("bear" if down else "neutral")
    lab = {"bull": "ขาขึ้น", "bear": "ขาลง (ระวัง ลดขนาดไม้)", "neutral": "ไซด์เวย์/ไม่ชัด"}[st]
    if vix and vix >= 25:
        lab += " · ตลาดกลัวสูง"
    return {"state": st, "label": lab, "icon": {"bull": "🟢", "bear": "🔴", "neutral": "🟡"}[st], "vix": vix}


def trade_setup(s, x, cfg):
    """3 แบบที่นักเทรดมืออาชีพใช้ (ดู skill stock-trade-setups) คืน dict หรือ None"""
    C, A = x["close"], x["atr"]
    trend = C > x["sma50"] > x["sma200"] and x["sma200_up"]
    if not trend or A <= 0 or x["rsi"] >= 80:
        return None
    base = (x["hi20"] - x["lo20"]) / C
    kind = order = None
    if C > x["hi20"] and x["vol_ratio"] >= 1.4 and base <= 0.15:
        kind, order = "ทะลุฐานพร้อมวอลุ่ม (Breakout)", "limit"
        lo_, hi_ = max(x["hi20"], C - 0.5 * A), C * 1.01
        stop = min(max(x["lo20"], x["hi20"] - 1.5 * A), lo_ - A)
    elif x["rs"] > 0 and 38 <= x["rsi"] <= 58 and x["low"] <= x["ema20"] * 1.01 and C >= x["sma50"] * 0.98 \
            and C <= x["ema20"] * 1.03:
        kind, order = "ย่อในขาขึ้น (Pullback)", "limit"
        lo_, hi_ = max(x["sma50"], C - A), C
        stop = min(x["low5"], x["sma50"]) - 0.5 * A
    elif x["rs"] > 0 and x["hi20"] * 0.97 <= C < x["hi20"] and base <= 0.12:
        kind, order = "รอทะลุกรอบ (ตั้งซื้อเมื่อราคาผ่าน)", "buy_stop"
        lo_ = x["hi20"] * 1.002
        hi_ = lo_ * 1.01
        stop = max(x["lo20"], lo_ - 2 * A)
    else:
        return None
    ref = (lo_ + hi_) / 2
    risk = (ref - stop) / ref
    if not 0.02 <= risk <= 0.10:   # stop แคบไป = โดนกวาดง่าย, กว้างไป = เสี่ยงต่อไม้เกิน
        return None
    t1, t2 = ref + 2 * (ref - stop), ref + 3 * (ref - stop)
    score = x["rs"] + x["rs63"] + 4 * min(x["vol_ratio"], 3) - 150 * max(0, risk - 0.06)
    out = {"cat": "trade", "kind": kind, "order": order, "zone": [r2(lo_), r2(hi_)], "stop": r2(stop),
           "t1": r2(t1), "t2": r2(t2), "risk_pct": round(risk * 100, 1), "score": round(score, 1),
           "horizon": "1-4 สัปดาห์"}
    acct = num(cfg.get("account_usd"), 0)
    if acct > 0:
        usd = min(acct * cfg["risk_per_trade_pct"] / 100 / risk, acct * 0.25)
        out["size_usd"] = round(usd, 0)
        out["shares"] = round(usd / ref, 2)
    return out


def growth_pick(s, x, f, cfg):
    rg, eg = num(f.get("revenueGrowth")), num(f.get("earningsGrowth"))
    pm, roe, fpe = num(f.get("profitMargins")), num(f.get("returnOnEquity")), num(f.get("forwardPE"))
    C = x["close"]
    if rg is None or rg * 100 < cfg["min_revenue_growth"] or (pm or 0) < 0.05:
        return None
    if C < x["sma200"] or x["rs"] < -5 or (fpe is not None and (fpe <= 0 or fpe > 80)):
        return None
    score = min(rg, 1) * 100 + min(max(eg or 0, -0.5), 1) * 30 + min(max(roe or 0, 0), 0.6) * 25 \
        + x["rs"] * 0.3 - max(0, (fpe or 30) - 40) * 0.6
    z_lo, z_hi = sorted([x["sma50"], x["ema20"]])
    ext = (C / x["sma50"] - 1) * 100
    if C < z_lo:    # ย่อลึกกว่าเส้น 50 วัน: โซนคือแถวราคาปัจจุบันถึงแนวรับถัดไป แบ่งไม้ซื้อ
        z_lo, z_hi = max(x["sma200"], C - 1.5 * x["atr"]), C
        action = "ย่อลึก ทยอยแบ่งไม้"
    else:
        action = "ทยอยซื้อได้" if C <= z_hi * 1.03 else "รอย่อเข้าโซน"
    tgt = num(f.get("targetMeanPrice"))
    if not tgt or tgt < C * 1.05:
        tgt = max(x["hi52"], C) * 1.15
    return {"cat": "growth", "kind": "หุ้นเติบโต", "action": action, "zone": [r2(z_lo), r2(z_hi)],
            "target": r2(tgt), "review": r2(x["sma200"] * 0.97), "ext50": round(ext, 1),
            "score": round(score, 1), "horizon": "6 เดือน - หลายปี",
            "trim_note": "ห่างเส้น 50 วันเกิน 25% = ขายทำกำไรบางส่วนได้" if ext > 25 else ""}


def dividend_pick(s, x, f, cfg, sector):
    C = x["close"]
    y = x["div_ttm"] / C if x["div_ttm"] else (num(f.get("trailingAnnualDividendYield")) or 0)
    if y * 100 < cfg["min_dividend_yield"] or x["div_count_2y"] < 4:
        return None
    payout = num(f.get("payoutRatio"))
    pay_cap = 1.5 if sector in ("RealEstate",) else 0.85   # REIT วัดจาก EPS แล้วสูงเกินจริง
    if payout is not None and (payout <= 0 or payout > pay_cap):
        return None
    if C < x["sma200"] * 0.93 or (C < x["sma200"] and not x["sma200_up"] and x["rs"] < -15):
        return None   # ราคาพังแรง มักมีปัญหาในบริษัท (เสี่ยงลดปันผล)
    beta = num(f.get("beta"), 1.0)
    score = y * 100 * 2 + (1 - min(beta, 2)) * 4 + x["rs"] * 0.15 - (5 if (payout or 0) > 0.75 else 0)
    z_lo, z_hi = sorted([x["sma200"], x["sma50"]])
    if C < z_lo:
        z_lo, z_hi = max(C * 0.95, C - 1.5 * x["atr"]), C
    action = "ทยอยซื้อได้" if C <= z_hi * 1.02 else "รอย่อเข้าโซน"
    return {"cat": "dividend", "kind": "หุ้นปันผล", "action": action, "zone": [r2(z_lo), r2(z_hi)],
            "yield_pct": round(y * 100, 2), "yield_at_zone": round(x["div_ttm"] / z_hi * 100, 2) if x["div_ttm"] else None,
            "payout_pct": round(payout * 100) if payout else None, "review": r2(min(x["sma200"], x["close"]) * 0.92),
            "score": round(score, 1), "horizon": "ถือยาว รับปันผล"}


# ===================================================================== Gemini
class Gemini:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
    FALLBACK = ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite")

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        self.err = ""

    @property
    def enabled(self):
        return bool(self.key) and bool(self.cfg.get("gemini", True))

    def _count(self):
        st = jload(STATE, {})
        today = now_th().strftime("%Y-%m-%d")
        g = st.get("gemini", {})
        if g.get("date") != today:
            g = {"date": today, "calls": 0, "tokens": 0}
        return st, g

    def call(self, prompt, max_tokens=16000):
        st, g = self._count()
        if g["calls"] >= int(self.cfg["gemini_max_calls_day"]):
            raise RuntimeError("ใช้ Gemini ครบ %d ครั้งวันนี้แล้ว" % g["calls"])
        err = None
        models = list(dict.fromkeys((self.cfg.get("gemini_model"),) + self.FALLBACK))
        for search in (True, False):
            for m in models:
                gen = {"temperature": 0.2, "maxOutputTokens": max_tokens}
                if not search:
                    gen["responseMimeType"] = "application/json"
                body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
                if search:
                    body["tools"] = [{"google_search": {}}]
                req = urllib.request.Request(self.URL % m, data=json.dumps(body).encode(), method="POST",
                                             headers={"x-goog-api-key": self.key, "content-type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=180) as r:
                        data = json.loads(r.read())
                except urllib.error.HTTPError as e:
                    err = "HTTP %s %s" % (e.code, e.read()[:160].decode("utf-8", "ignore"))
                    if e.code in (400, 404):
                        continue
                    raise RuntimeError(err)
                cand = (data.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])
                               if not p.get("thought"))
                us = data.get("usageMetadata", {})
                g["calls"] += 1
                g["tokens"] += int(us.get("totalTokenCount", 0))
                st["gemini"] = g
                jsave(STATE, st)
                if not text.strip():
                    err = "Gemini ตอบว่าง (%s)" % cand.get("finishReason")
                    continue
                return text, m, search
        raise RuntimeError(err or "เรียก Gemini ไม่ได้")

    @staticmethod
    def parse(text):
        t = text.replace("```json", "").replace("```", "")
        return json.loads(t[t.index("{"):t.rindex("}") + 1])

    def review(self, cands, regime, date):
        rows = []
        for c in cands:
            x, f, p = c["x"], c["f"], c["pick"]
            row = {"symbol": c["symbol"], "name": f.get("shortName"), "sector": f.get("sector") or c["sector"],
                   "category": p["cat"], "setup": p["kind"], "price": r2(x["close"]), "buy_zone": p["zone"],
                   "rsi": round(x["rsi"]), "ret_6m_pct": round(x["ret126"], 1), "rel_strength_vs_spy_6m": round(x["rs"], 1),
                   "pct_from_52w_high": round((x["close"] / x["hi52"] - 1) * 100, 1)}
            for k in ("stop", "t1", "t2", "target", "review", "yield_pct", "payout_pct"):
                if p.get(k) is not None:
                    row[k] = p[k]
            for k, n in (("revenueGrowth", "rev_growth"), ("earningsGrowth", "eps_growth"),
                         ("profitMargins", "net_margin"), ("returnOnEquity", "roe")):
                if num(f.get(k)) is not None:
                    row[n] = round(num(f.get(k)) * 100, 1)
            for k in ("forwardPE", "targetMeanPrice", "recommendationKey"):
                if f.get(k) is not None:
                    row[k] = round(f[k], 1) if isinstance(f[k], float) else f[k]
            dd, ed = next_earnings_days(f)
            if ed:
                row["next_earnings"] = "%s (อีก %d วัน)" % (ed, dd)
            rows.append(row)
        prompt = (
            "คุณคือผู้จัดการกองทุนหุ้นสหรัฐฯ มืออาชีพ วิเคราะห์ให้นักลงทุนรายย่อยชาวไทยที่ซื้อผ่าน Webull\n"
            "วันที่ข้อมูลปิดตลาดล่าสุด: %s | ภาวะตลาด (SPY): %s | VIX: %s\n\n"
            "ระบบคัดกรองเชิงปริมาณเลือกหุ้นด้านล่างมาแล้ว (category: trade = เทรดเน้นกำไร 1-4 สัปดาห์, "
            "growth = ถือยาวเน้นเติบโต, dividend = ถือยาวเน้นปันผล) พร้อมโซนซื้อ/จุดตัดขาดทุน/เป้าขาย\n"
            "ให้ใช้ Google Search ค้นข่าวสำคัญ 2 สัปดาห์ล่าสุดของแต่ละตัว (งบ, guidance, คดี, ลดปันผล, ดาวน์เกรด, "
            "ดีลใหญ่) แล้วตัดสินแต่ละตัว:\n"
            "- buy = น่าซื้อตามแผน, wait = รอ (เช่นใกล้ประกาศงบ, ข่าวยังไม่ชัด, แพงไป), avoid = ไม่ควรแตะ (ข่าวร้ายหนัก/พื้นฐานแย่)\n"
            "- trade ที่ประกาศงบภายใน 5 วัน ให้ wait เสมอ\n"
            "- category ให้คงเดิม ยกเว้นเห็นชัดว่าควรย้ายกลุ่ม\n"
            "- เขียนภาษาไทยกระชับ เข้าใจง่าย ไม่ใช้ศัพท์เทคนิคยาก reason ไม่เกิน 160 ตัวอักษร news ไม่เกิน 120 ตัวอักษร\n"
            "ตอบเป็น JSON อย่างเดียว ไม่มีข้อความอื่น:\n"
            '{"market_note":"ภาพรวมตลาดสั้นๆ 1-2 ประโยค","stocks":[{"symbol":"","verdict":"buy|wait|avoid",'
            '"confidence":0-100,"category":"trade|growth|dividend","reason":"","news":"","risk":""}]}\n\n'
            "ข้อมูล:\n%s" % (date, regime["label"], regime.get("vix"), json.dumps(rows, ensure_ascii=False)))
        text, model, searched = self.call(prompt)
        out = self.parse(text)
        out["model"] = model + (" + Google Search" if searched else "")
        return out


# ===================================================================== ติดตามตัวที่เคยแนะนำ
ACTIVE = ("waiting", "open", "t1", "watch", "target")


def update_tracking(picks, data, cfg):
    """ไล่แท่งเทียนใหม่ของแต่ละตัวที่ติดตามอยู่ คืนรายการแจ้งเตือน"""
    alerts = []
    for p in picks:
        if p["status"] not in ACTIVE or p["symbol"] not in data:
            continue
        d = data[p["symbol"]]
        bars = d[d.index.strftime("%Y-%m-%d") > p["seen"]]
        for idx, b in bars.iterrows():
            day = str(idx)[:10]
            o, h, lo, c = float(b["Open"]), float(b["High"]), float(b["Low"]), float(b["Close"])
            p["seen"] = day
            p["days"] = p.get("days", 0) + 1
            s = p["symbol"]
            if p["cat"] == "trade":
                if p["status"] == "waiting":
                    hit = h >= p["zone"][0] if p["order"] == "buy_stop" else lo <= p["zone"][1]
                    if hit:
                        p["fill"] = r2(max(o, p["zone"][0]) if p["order"] == "buy_stop" else min(o, p["zone"][1]))
                        p["status"], p["fill_day"], p["held"] = "open", day, 0
                        alerts.append("🟦 %s เข้าโซนซื้อแล้ว ($%s) ถ้าซื้อแล้วตั้ง stop $%s เป้า1 $%s"
                                      % (s, money(p["fill"]), money(p["stop"]), money(p["t1"])))
                    elif p["days"] > 5:
                        p["status"] = "expired"
                        continue
                    else:
                        continue
                p["held"] = p.get("held", 0) + 1
                if lo <= p["stop"]:
                    ex = min(o, p["stop"])
                    _close(p, ex, day)
                    alerts.append(("✅ %s ออกที่ทุนหลังขายครึ่งที่เป้า1 (รวม %+.1f%%)" if p["status_prev"] == "t1"
                                   else "🟥 %s หลุดจุดตัดขาดทุน $%s → ขายตามแผน (%+.1f%%)")
                                  % ((s, p["result_pct"]) if p["status_prev"] == "t1" else (s, money(p["stop"]), p["result_pct"])))
                    break
                if p["status"] == "open" and h >= p["t1"]:
                    p["status"], p["stop"] = "t1", p["fill"]
                    alerts.append("🟩 %s ถึงเป้า 1 ($%s) → ขายครึ่ง แล้วเลื่อน stop มาที่ทุน $%s"
                                  % (s, money(p["t1"]), money(p["fill"])))
                if p["status"] == "t1" and h >= p["t2"]:
                    _close(p, p["t2"], day)
                    alerts.append("🏆 %s ถึงเป้า 2 ($%s) → ขายส่วนที่เหลือ (รวม %+.1f%%)" % (s, money(p["t2"]), p["result_pct"]))
                    break
                if p["held"] >= cfg["track_trade_days"]:
                    _close(p, c, day)
                    alerts.append("⌛ %s ถือครบ %d วันแล้วไม่ถึงเป้า → ขายที่ราคาตลาด (%+.1f%%)"
                                  % (s, cfg["track_trade_days"], p["result_pct"]))
                    break
            else:
                if c <= p["review"]:
                    p["status"], p["exit_day"], p["result_pct"] = "closed", day, round((c / p["price"] - 1) * 100, 1)
                    alerts.append("🟥 %s ปิดต่ำกว่าจุดเลิกถือ $%s → ขาย/ทบทวน (%+.1f%% จากวันแนะนำ)"
                                  % (s, money(p["review"]), p["result_pct"]))
                    break
                if p["status"] == "watch" and p.get("target") and h >= p["target"]:
                    p["status"] = "target"
                    alerts.append("🟩 %s ถึงเป้าราคา $%s → ขายทำกำไรบางส่วนได้ (%+.1f%%)"
                                  % (s, money(p["target"]), (p["target"] / p["price"] - 1) * 100))
                if p["days"] >= cfg["track_long_days"]:
                    p["status"], p["exit_day"], p["result_pct"] = "ended", day, round((c / p["price"] - 1) * 100, 1)
                    break
    return alerts


def _close(p, ex, day):
    f = p["fill"]
    if p["status"] == "t1":     # ขายครึ่งที่เป้า 1 ไปแล้ว
        res = ((p["t1"] - f) / 2 + (ex - f) / 2) / f
    else:
        res = (ex - f) / f
    p["status_prev"] = p["status"]
    p["status"], p["exit"], p["exit_day"], p["result_pct"] = "closed", r2(ex), day, round(res * 100, 1)


def track_stats(picks):
    done = [p for p in picks if p["cat"] == "trade" and p["status"] == "closed"]
    if not done:
        return None
    wins = [p for p in done if p["result_pct"] > 0]
    return {"trades": len(done), "wins": len(wins), "win_rate": round(len(wins) / len(done) * 100),
            "avg_pct": round(sum(p["result_pct"] for p in done) / len(done), 2),
            "sum_pct": round(sum(p["result_pct"] for p in done), 1)}


# ===================================================================== สแกน
def scan(cfg, send_line=True, reason="ตามเวลา"):
    if not _lock.acquire(blocking=False):
        log("กำลังสแกนอยู่แล้ว")
        return None
    t0 = time.time()
    try:
        return _scan(cfg, send_line, reason, t0)
    finally:
        _lock.release()


def _scan(cfg, send_line, reason, t0):
    syms = sorted(UNIVERSE)
    log("เริ่มสแกน %d ตัว (%s)" % (len(syms), reason))
    data = fetch_prices(syms + ["^VIX"])
    if BENCHMARK not in data:
        raise RuntimeError("ดึงราคา SPY ไม่ได้ (Yahoo ไม่ตอบ) จะลองใหม่รอบหน้า")
    X = {s: indicators(data[s]) for s in syms if s in data}
    vix = num(data["^VIX"]["Close"].iloc[-1]) if "^VIX" in data else None
    spy = X[BENCHMARK]
    for x in X.values():
        x["rs"] = x["ret126"] - spy["ret126"]
        x["rs63"] = x["ret63"] - spy["ret63"]
    regime = market_regime(spy, r2(vix))
    bar_date = spy["date"]

    stocks = [s for s in X if UNIVERSE[s][1] == "stock" and X[s]["close"] >= X[s]["sma200"] * 0.9]
    F = fetch_fundamentals(stocks)

    cands = []
    for s, x in X.items():
        sector, typ = UNIVERSE[s]
        if x["close"] < 5 or x["dollar_vol_m"] < cfg["min_dollar_volume_m"]:
            continue
        f = F.get(s, {}) if typ == "stock" else {}
        picks = [trade_setup(s, x, cfg)]
        if typ == "stock" and f:
            picks.append(growth_pick(s, x, f, cfg))
        picks.append(dividend_pick(s, x, f, cfg, sector))
        for p in picks:
            if p:
                dd, ed = next_earnings_days(f)
                if ed and dd is not None and dd <= 14:
                    p["earnings"] = "%s (อีก %d วัน)" % (thai_date(ed), dd)
                cands.append({"symbol": s, "sector": sector, "x": x, "f": f, "pick": p})

    # คัดตัวท็อปของแต่ละกลุ่มก่อนส่ง Gemini (ขาลง: ลดจำนวนไม้เทรด)
    max_n = {"trade": cfg["max_trade"] if regime["state"] != "bear" else 2,
             "growth": cfg["max_growth"], "dividend": cfg["max_dividend"]}
    by = {}
    for c in sorted(cands, key=lambda c: -c["pick"]["score"]):
        by.setdefault(c["pick"]["cat"], []).append(c)
    cap = int(cfg["gemini_max_candidates"])
    short = []
    for cat in ("trade", "growth", "dividend"):
        short += by.get(cat, [])[:min(cap // 3, max_n[cat] * 2)]

    gem = Gemini(cfg)
    g_out, g_err = None, ""
    if gem.enabled and short:
        try:
            g_out = gem.review(short, regime, bar_date)
            log("Gemini วิเคราะห์ %d ตัวแล้ว (%s)" % (len(short), g_out.get("model")))
        except Exception as e:
            g_err = str(e)[:200]
            log("Gemini ใช้ไม่ได้: " + g_err)
    elif not gem.key:
        g_err = "ยังไม่ได้ใส่ GEMINI_API_KEY"
    verdicts = {}
    for v in (g_out or {}).get("stocks") or []:
        if isinstance(v, dict) and v.get("symbol"):
            verdicts[(str(v["symbol"]).upper(), v.get("category"))] = v
            verdicts.setdefault((str(v["symbol"]).upper(), None), v)

    results = []
    for c in short:
        p = c["pick"]
        v = verdicts.get((c["symbol"], p["cat"])) or verdicts.get((c["symbol"], None))
        if v:
            verdict = v.get("verdict") if v.get("verdict") in ("buy", "wait", "avoid") else "wait"
        else:
            verdict = "buy" if not g_out else "wait"   # Gemini ใช้ไม่ได้ = ใช้ผลระบบอย่างเดียว
        if p["cat"] == "trade" and p.get("earnings") and verdict == "buy":
            dd = next_earnings_days(c["f"])[0]
            if dd is not None and dd <= 5:
                verdict = "wait"
        f, x = c["f"], c["x"]
        results.append({
            "symbol": c["symbol"], "name": f.get("shortName") or c["symbol"], "sector": c["sector"],
            "price": r2(x["close"]), "rsi": round(x["rsi"]), "rs": round(x["rs"], 1), "ret126": round(x["ret126"], 1),
            "rev_growth": round(num(f.get("revenueGrowth"), 0) * 100, 1) if f.get("revenueGrowth") is not None else None,
            "fpe": r2(num(f.get("forwardPE"))), "verdict": verdict,
            "confidence": (v or {}).get("confidence"), "reason": (v or {}).get("reason", ""),
            "news": (v or {}).get("news", ""), "risk": (v or {}).get("risk", ""), **p})
    order = {"buy": 0, "wait": 1, "avoid": 2}
    results.sort(key=lambda r: (order[r["verdict"]], -r["score"]))
    final = {}
    for r in results:
        if r["verdict"] == "buy" and len(final.setdefault(r["cat"], [])) < max_n[r["cat"]]:
            final[r["cat"]].append(r)

    # ติดตามตัวเดิม + เพิ่มตัวใหม่
    picks = jload(PICKS, [])
    alerts = update_tracking(picks, data, cfg)
    if send_line:   # สแกนเองจากหน้าเว็บ ไม่นับเป็นคำแนะนำใหม่ (กันซ้ำ)
        active = {(p["symbol"], p["cat"]) for p in picks if p["status"] in ACTIVE}
        for cat, rows in final.items():
            for r in rows:
                if (r["symbol"], cat) in active:
                    continue
                base = {"symbol": r["symbol"], "cat": cat, "date": bar_date, "seen": bar_date, "price": r["price"],
                        "zone": r["zone"], "days": 0}
                if cat == "trade":
                    base.update(status="waiting", order=r["order"], stop=r["stop"], t1=r["t1"], t2=r["t2"])
                else:
                    base.update(status="watch", review=r["review"], target=r.get("target"))
                picks.append(base)
        picks = [p for p in picks if p["status"] in ACTIVE or p.get("exit_day", p["date"]) >= (
            now_th() - timedelta(days=400)).strftime("%Y-%m-%d")]
        jsave(PICKS, picks)

    rep = {"time": now_th().strftime("%Y-%m-%d %H:%M"), "bar_date": bar_date, "reason": reason,
           "regime": regime, "scanned": len(X), "candidates": len(cands), "reviewed": len(short),
           "gemini": {"on": bool(g_out), "model": (g_out or {}).get("model"), "error": g_err,
                      "market_note": (g_out or {}).get("market_note", "")},
           "final": final, "results": results, "alerts": alerts,
           "tracking": [p for p in picks if p["status"] in ACTIVE],
           "closed": [p for p in picks if p["status"] == "closed"][-30:],
           "stats": track_stats(picks), "secs": round(time.time() - t0)}
    jsave(REPORT, rep)
    log("เสร็จใน %d วินาที: ผ่านเกณฑ์ %d ตัว, แนะนำ %d ตัว" % (rep["secs"], len(cands), sum(len(v) for v in final.values())))
    if send_line and cfg.get("line", True):
        n = LineNotifier(jload(os.path.join(HERE, "config.json"), {}), prefix="📊 ฝ่ายสแกนหุ้น US")
        if n.enabled:
            ok = n.send(line_text(rep, cfg), key="scan_daily", every_sec=3600)
            log("ส่ง LINE แล้ว" if ok else "ส่ง LINE ไม่ได้ (โควตาเต็ม/ส่งไปแล้วชั่วโมงนี้/ตั้งค่าไม่ครบ)")
    return rep


# ===================================================================== ข้อความ LINE
CAT = {"trade": ("⚡", "เทรดเน้นกำไร"), "growth": ("🌱", "ถือยาว เน้นเติบโต"), "dividend": ("💰", "ถือยาว เน้นปันผล")}


def line_text(rep, cfg):
    g, rg = rep["gemini"], rep["regime"]
    L = ["ข้อมูลปิดตลาด %s | ตลาด %s %s%s" % (thai_date(rep["bar_date"]), rg["icon"], rg["label"],
                                               (" · VIX %.1f" % rg["vix"]) if rg.get("vix") else "")]
    if g.get("market_note"):
        L.append("🧠 " + g["market_note"])
    elif g.get("error"):
        L.append("🧠 Gemini ไม่ได้ช่วยรอบนี้ (%s) ใช้ผลระบบอย่างเดียว" % g["error"][:60])
    if rep["alerts"]:
        L += ["", "🔔 ติดตามตัวเดิม"] + ["• " + a for a in rep["alerts"][:8]]
    for cat in ("trade", "growth", "dividend"):
        rows = rep["final"].get(cat) or []
        ic, name = CAT[cat]
        L += ["", "%s %s" % (ic, name) + (" (ถือ 1-4 สัปดาห์)" if cat == "trade" else "")]
        if not rows:
            L.append("วันนี้ไม่มีตัวที่เข้าเกณฑ์ (ไม่ซื้อก็ไม่ผิด)")
        for i, r in enumerate(rows, 1):
            if cat == "trade":
                buy = ("ตั้งซื้อเมื่อผ่าน %s" % money(r["zone"][0])) if r["order"] == "buy_stop" \
                    else "ซื้อ %s-%s" % (money(r["zone"][0]), money(r["zone"][1]))
                L.append("%d) %s $%s · %s" % (i, r["symbol"], money(r["price"]), r["kind"]))
                L.append("   %s | ตัดขาดทุน %s (-%s%%)" % (buy, money(r["stop"]), r["risk_pct"]))
                L.append("   ขาย: เป้า1 %s (ครึ่ง) · เป้า2 %s" % (money(r["t1"]), money(r["t2"])))
                if r.get("size_usd"):
                    L.append("   ขนาดไม้ ~$%s (%s หุ้น)" % (money(r["size_usd"]), r["shares"]))
            elif cat == "growth":
                L.append("%d) %s $%s · รายได้โต %s%%" % (i, r["symbol"], money(r["price"]), r.get("rev_growth")))
                L.append("   %s %s-%s | เป้า %s | เลิกถือถ้าปิดต่ำกว่า %s"
                         % (r["action"], money(r["zone"][0]), money(r["zone"][1]), money(r["target"]), money(r["review"])))
            else:
                L.append("%d) %s $%s · ปันผล %s%%/ปี" % (i, r["symbol"], money(r["price"]), r["yield_pct"]))
                L.append("   %s %s-%s | เลิกถือถ้าปิดต่ำกว่า %s หรือบริษัทลดปันผล"
                         % (r["action"], money(r["zone"][0]), money(r["zone"][1]), money(r["review"])))
            if r.get("earnings"):
                L.append("   📅 ประกาศงบ " + r["earnings"])
            if r.get("reason"):
                L.append("   💬 " + r["reason"])
    wait = [r["symbol"] for r in rep["results"] if r["verdict"] == "wait"]
    if wait:
        L += ["", "⏳ รอจังหวะ: " + ", ".join(dict.fromkeys(wait))]
    st = rep.get("stats")
    if st:
        L += ["", "📈 สถิติไม้เทรดที่แนะนำ: %d ไม้ ชนะ %d%% เฉลี่ย %+.2f%%/ไม้" % (st["trades"], st["win_rate"], st["avg_pct"])]
    url = cfg.get("public_url") or _tailscale_url(cfg)
    if url:
        L.append("ดูละเอียด: " + url)
    L.append("⚠️ ข้อมูลประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน")
    return "\n".join(L)


def _tailscale_url(cfg):
    try:
        ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
        return "http://%s:%d" % (ip[0], cfg["port"]) if ip else ""
    except Exception:
        return ""


# ===================================================================== หน้าเว็บ
CSS = """body{font-family:-apple-system,'Segoe UI',sans-serif;background:#0f1419;color:#e6e6e6;margin:0;padding:12px;font-size:15px}
h1{font-size:20px;margin:4px 0}h2{font-size:17px;margin:20px 0 8px;border-bottom:1px solid #2a3340;padding-bottom:4px}
.card{background:#1a222c;border-radius:10px;padding:10px 12px;margin:8px 0}.muted{color:#8a96a3;font-size:13px}
.sym{font-weight:700;font-size:17px}.tag{display:inline-block;border-radius:6px;padding:1px 7px;font-size:12px;margin-left:6px}
.buy{background:#123d24;color:#5fe08f}.wait{background:#3d3412;color:#e0c25f}.avoid{background:#3d1616;color:#ef7a7a}
.row{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:4px}.k{color:#8a96a3}.g{color:#5fe08f}.r{color:#ef7a7a}
button{background:#2f6fed;color:#fff;border:0;border-radius:8px;padding:10px 14px;font-size:15px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:5px 4px;border-bottom:1px solid #2a3340;text-align:left}"""


def _e(s):
    return html.escape(str(s if s is not None else ""))


def render(rep, msg=""):
    if not rep:
        return "<html><body style='font-family:sans-serif'>ยังไม่มีผลสแกน กำลังสแกนรอบแรก รอ 2-3 นาทีแล้วรีเฟรช</body></html>"
    rg, g = rep["regime"], rep["gemini"]
    H = ["<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>สแกนหุ้น US</title><style>%s</style></head><body>" % CSS,
         "<h1>📊 ฝ่ายสแกนหุ้น US</h1><div class='muted'>สแกนล่าสุด %s (เวลาไทย) · ข้อมูลปิดตลาด %s · %d ตัว · %s</div>"
         % (_e(rep["time"]), _e(thai_date(rep["bar_date"])), rep["scanned"], _e(rep["reason"])),
         "<div class='card'>ตลาด: %s %s%s<br>🧠 Gemini: %s</div>" % (
             rg["icon"], _e(rg["label"]), (" · VIX %.1f" % rg["vix"]) if rg.get("vix") else "",
             _e(g.get("market_note") or ("ไม่ได้ใช้ — " + (g.get("error") or "")))
             + ("<div class='muted'>%s</div>" % _e(g["model"]) if g.get("model") else "")),
         "<form method='post' action='scan'><button>🔄 สแกนใหม่ตอนนี้</button> <span class='muted'>%s</span></form>" % _e(msg)]
    if rep["alerts"]:
        H.append("<h2>🔔 ติดตามตัวเดิม</h2>" + "".join("<div class='card'>%s</div>" % _e(a) for a in rep["alerts"]))
    for cat in ("trade", "growth", "dividend"):
        ic, name = CAT[cat]
        rows = [r for r in rep["results"] if r["cat"] == cat]
        H.append("<h2>%s %s</h2>" % (ic, name))
        if not rows:
            H.append("<div class='muted'>ไม่มีตัวที่เข้าเกณฑ์วันนี้</div>")
        for r in rows:
            tag = {"buy": "ซื้อได้", "wait": "รอจังหวะ", "avoid": "ไม่ควรแตะ"}[r["verdict"]]
            conf = (" %s%%" % r["confidence"]) if r.get("confidence") is not None else ""
            H.append("<div class='card'><span class='sym'>%s</span> <span class='muted'>%s</span>"
                     "<span class='tag %s'>%s%s</span><div class='row'><span>$%s</span><span class='k'>%s</span>"
                     "<span class='k'>RSI %s</span><span class='k'>แข็งกว่าตลาด %+.1f%%</span></div>"
                     % (_e(r["symbol"]), _e(r["name"]), r["verdict"], tag, conf, money(r["price"]), _e(r["kind"]),
                        r["rsi"], r["rs"]))
            if cat == "trade":
                buy = ("ตั้งซื้อเมื่อผ่าน $%s" % money(r["zone"][0])) if r["order"] == "buy_stop" \
                    else "โซนซื้อ $%s-%s" % (money(r["zone"][0]), money(r["zone"][1]))
                H.append("<div class='row'><span>%s</span><span class='r'>ตัดขาดทุน $%s (-%s%%)</span>"
                         "<span class='g'>เป้า1 $%s (ขายครึ่ง)</span><span class='g'>เป้า2 $%s</span>%s</div>"
                         % (buy, money(r["stop"]), r["risk_pct"], money(r["t1"]), money(r["t2"]),
                            ("<span>ขนาดไม้ ~$%s</span>" % money(r["size_usd"])) if r.get("size_usd") else ""))
            elif cat == "growth":
                H.append("<div class='row'><span>%s $%s-%s</span><span class='g'>เป้า $%s</span>"
                         "<span class='r'>เลิกถือถ้าปิดต่ำกว่า $%s</span><span class='k'>รายได้โต %s%% · Fwd P/E %s</span></div>"
                         % (_e(r["action"]), money(r["zone"][0]), money(r["zone"][1]), money(r["target"]),
                            money(r["review"]), r.get("rev_growth"), r.get("fpe") or "-"))
                if r.get("trim_note"):
                    H.append("<div class='muted'>%s</div>" % _e(r["trim_note"]))
            else:
                H.append("<div class='row'><span>ปันผล %s%%/ปี</span><span>%s $%s-%s</span>"
                         "<span class='k'>จ่ายจากกำไร %s%%</span><span class='r'>เลิกถือถ้าปิดต่ำกว่า $%s หรือลดปันผล</span></div>"
                         % (r["yield_pct"], _e(r["action"]), money(r["zone"][0]), money(r["zone"][1]),
                            r.get("payout_pct") or "-", money(r["review"])))
            if r.get("earnings"):
                H.append("<div class='muted'>📅 ประกาศงบ %s</div>" % _e(r["earnings"]))
            for k, ic2 in (("reason", "💬"), ("news", "📰"), ("risk", "⚠️")):
                if r.get(k):
                    H.append("<div>%s %s</div>" % (ic2, _e(r[k])))
            H.append("</div>")
    st = rep.get("stats")
    H.append("<h2>📈 ติดตามผลคำแนะนำ</h2>")
    if st:
        H.append("<div class='card'>ไม้เทรดที่จบแล้ว %d ไม้ · ชนะ %d%% · เฉลี่ย %+.2f%%/ไม้ · รวม %+.1f%%</div>"
                 % (st["trades"], st["win_rate"], st["avg_pct"], st["sum_pct"]))
    TH_ST = {"waiting": "รอเข้าโซน", "open": "ถืออยู่", "t1": "ขายครึ่งแล้ว", "watch": "ถือยาว", "target": "ถึงเป้าแล้ว"}
    if rep["tracking"]:
        H.append("<table><tr><th>หุ้น</th><th>กลุ่ม</th><th>วันที่</th><th>สถานะ</th><th>แผน</th></tr>")
        for p in rep["tracking"]:
            plan = ("stop %s · เป้า %s/%s" % (money(p["stop"]), money(p["t1"]), money(p["t2"]))) if p["cat"] == "trade" \
                else "เลิกถือ < %s%s" % (money(p["review"]), (" · เป้า %s" % money(p["target"])) if p.get("target") else "")
            H.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (_e(p["symbol"]), CAT[p["cat"]][0], _e(thai_date(p["date"])), TH_ST.get(p["status"], p["status"]), _e(plan)))
        H.append("</table>")
    if rep["closed"]:
        H.append("<h2>ไม้ที่จบแล้ว</h2><table><tr><th>หุ้น</th><th>แนะนำ</th><th>จบ</th><th>ผล</th></tr>")
        for p in reversed(rep["closed"]):
            cls = "g" if (p.get("result_pct") or 0) > 0 else "r"
            H.append("<tr><td>%s %s</td><td>%s</td><td>%s</td><td class='%s'>%+.1f%%</td></tr>"
                     % (CAT[p["cat"]][0], _e(p["symbol"]), _e(thai_date(p["date"])), _e(thai_date(p.get("exit_day"))),
                        cls, p.get("result_pct") or 0))
        H.append("</table>")
    H.append("<p class='muted'>เกณฑ์: เทรด = แนวโน้มขาขึ้น (ราคา &gt; เส้น 50 &gt; เส้น 200 วัน) + จังหวะย่อ/ทะลุฐาน, เสี่ยงไม่เกิน 10%%/ไม้, "
             "เป้า 2-3 เท่าของความเสี่ยง · เติบโต = รายได้โต ≥%s%% มีกำไร ราคาเหนือเส้น 200 วัน · ปันผล = ≥%s%%/ปี จ่ายสม่ำเสมอ "
             "ไม่จ่ายเกินกำไร · ข้อมูลจาก Yahoo Finance · ไม่ใช่คำแนะนำการลงทุน</p></body></html>"
             % (load_cfg()["min_revenue_growth"], load_cfg()["min_dividend_yield"]))
    return "".join(H)


def serve(cfg):
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if len(pw) < 10:
        log("ไม่เปิดหน้าเว็บ: ต้องตั้ง DASHBOARD_PASSWORD อย่างน้อย 10 ตัว")
        return
    state = {"last_manual": 0, "msg": ""}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            a = self.headers.get("Authorization", "")
            try:
                ok = a.startswith("Basic ") and hmac.compare_digest(
                    base64.b64decode(a[6:]).decode("utf-8", "ignore").split(":", 1)[1], pw)
            except Exception:
                ok = False
            if not ok:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="stock scanner"')
                self.end_headers()
            return ok

        def _send(self, body, ctype="text/html; charset=utf-8", code=200):
            b = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if not self._auth():
                return
            if self.path.startswith("/report.json"):
                return self._send(json.dumps(jload(REPORT, {}), ensure_ascii=False), "application/json")
            self._send(render(jload(REPORT, None), state["msg"]))

        def do_POST(self):
            if not self._auth():
                return
            wait = cfg["manual_scan_cooldown_min"] * 60 - (time.time() - state["last_manual"])
            if _lock.locked():
                state["msg"] = "กำลังสแกนอยู่ รอ 2-3 นาทีแล้วรีเฟรช"
            elif wait > 0:
                state["msg"] = "เพิ่งสแกนไป รออีก %d นาที" % (wait // 60 + 1)
            else:
                state["last_manual"] = time.time()
                state["msg"] = "เริ่มสแกนแล้ว รอ 2-3 นาทีแล้วรีเฟรช (ไม่ส่ง LINE)"
                threading.Thread(target=_safe_scan, args=(load_cfg(), False, "สแกนเองจากหน้าเว็บ"), daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "./")
            self.end_headers()

    srv = ThreadingHTTPServer(("0.0.0.0", int(cfg["port"])), H)
    srv.daemon_threads = True
    log("หน้าเว็บ: พอร์ต %s" % cfg["port"])
    srv.serve_forever()


def _safe_scan(cfg, send_line, reason):
    try:
        return scan(cfg, send_line, reason)
    except Exception as e:
        traceback.print_exc()
        log("สแกนไม่สำเร็จ: %s" % e)
        if send_line:
            n = LineNotifier(jload(os.path.join(HERE, "config.json"), {}), prefix="📊 ฝ่ายสแกนหุ้น US")
            n.send("⚠️ สแกนไม่สำเร็จ: %s (จะลองใหม่อัตโนมัติ)" % str(e)[:200], key="scan_err", every_sec=6 * 3600)
        return None


def run_service():
    cfg = load_cfg()
    threading.Thread(target=serve, args=(cfg,), daemon=True).start()
    if not os.path.exists(REPORT):
        _safe_scan(cfg, False, "รอบแรกหลังติดตั้ง")
    while True:
        cfg = load_cfg()
        n = now_th()
        hh, mm = [int(v) for v in str(cfg["scan_time_th"]).split(":")]
        st = jload(STATE, {})
        today = n.strftime("%Y-%m-%d")
        if n.weekday() in cfg["scan_days"] and (n.hour, n.minute) >= (hh, mm) and st.get("last_auto") != today \
                and n.hour < 20:
            latest = _latest_bar()
            if latest and latest == st.get("last_auto_bar"):
                st["last_auto"] = today
                jsave(STATE, st)
                log("ไม่มีข้อมูลใหม่ (ตลาด US หยุด) ไม่ส่ง LINE")
            else:
                res = _safe_scan(cfg, True, "ตามเวลา %s น." % cfg["scan_time_th"])
                if res:
                    st = jload(STATE, {})
                    st["last_auto"], st["last_auto_bar"] = today, res["bar_date"]
                    jsave(STATE, st)
                else:
                    time.sleep(25 * 60)   # สแกนพลาด: ลองใหม่อีก 25 นาที
        time.sleep(30)


def _latest_bar():
    """วันที่แท่งล่าสุดของ SPY (ใช้เช็กว่าวันหยุดตลาดหรือไม่)"""
    try:
        import yfinance as yf
        d = yf.download(BENCHMARK, period="5d", interval="1d", progress=False, auto_adjust=True)
        d = d.dropna()
        return str(d.index[-1])[:10]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="สแกนเดี๋ยวนี้ 1 ครั้ง")
    ap.add_argument("--no-line", action="store_true")
    a = ap.parse_args()
    os.chdir(HERE)
    if a.once:
        rep = scan(load_cfg(), not a.no_line, "สั่งสแกนเอง")
        if rep:
            print(line_text(rep, load_cfg()))
        return
    run_service()


if __name__ == "__main__":
    main()
