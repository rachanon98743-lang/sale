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
     แล้วให้ Claude (ค้นข่าวด้วย web search ของตัวเอง) เป็นคณะกรรมการความเสี่ยงตรวจซ้ำอีกชั้น
     ซื้อได้ก็ต่อเมื่อ AI ที่ทำงานรอบนั้นไม่มีตัวไหนค้าน (ตัวไหนบอก avoid = ไม่ซื้อ, ไม่ตรงกัน = รอ)
  4) ส่งสรุปเข้า LINE วันละ 1 ข้อความ (หลังตลาด US ปิด) + เปิดหน้าเว็บดูละเอียดที่พอร์ต 8082
  5) ติดตามตัวที่เคยแนะนำ: เข้าโซนซื้อ / ถึงเป้าขาย / หลุดจุดตัดขาดทุน แจ้งในข้อความวันถัดไป + เก็บสถิติอัตราชนะ

รัน
    python scanner.py            # บริการ: เว็บ + สแกนเองทุกวันตามเวลาใน config.json → "scanner"
    python scanner.py --once     # สแกนเดี๋ยวนี้ 1 ครั้ง แล้วส่ง LINE
    python scanner.py --once --no-line
ไม่ใช่คำแนะนำการลงทุน: ทุกตัวเลขเป็นการประเมินจากข้อมูลย้อนหลัง ตัดสินใจและรับความเสี่ยงเอง
"""

import argparse
import hmac
import html
import http.cookies
import secrets
import urllib.parse
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
    "claude": True,
    "claude_model": "claude-sonnet-5-5",
    "claude_max_calls_day": 2,
    "claude_max_searches": 6,       # จำนวนครั้งค้นเว็บต่อรอบ (Anthropic คิดเพิ่มต่อครั้ง)
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
def ai_rows(cands):
    """ข้อมูลของแต่ละตัวที่ส่งให้ AI ตรวจ (ชุดเดียวกันทั้ง Gemini และ Claude)"""
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
    return rows


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
        rows = ai_rows(cands)
        return self._review(rows, regime, date)

    def _review(self, rows, regime, date):
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


class Claude:
    """คณะกรรมการความเสี่ยงชั้นที่ 2: ตรวจตัวที่ระบบ + Gemini คัดมา ค้นข่าวเองด้วย web search ของ Anthropic"""
    URL = "https://api.anthropic.com/v1/messages"
    SEARCH_TOOLS = ("web_search_20260209", "web_search_20250305")

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")

    @property
    def enabled(self):
        return bool(self.key) and bool(self.cfg.get("claude", True))

    def _post(self, body):
        req = urllib.request.Request(self.URL, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())

    def call(self, prompt, max_tokens=6000):
        st = jload(STATE, {})
        today = now_th().strftime("%Y-%m-%d")
        c = st.get("claude", {})
        if c.get("date") != today:
            c = {"date": today, "calls": 0, "in": 0, "out": 0, "searches": 0}
        if c["calls"] >= int(self.cfg["claude_max_calls_day"]):
            raise RuntimeError("ใช้ Claude ครบ %d ครั้งวันนี้แล้ว" % c["calls"])
        err = None
        tools = [[{"type": t, "name": "web_search", "max_uses": int(self.cfg["claude_max_searches"])}]
                 for t in self.SEARCH_TOOLS] + [None]
        for tl in tools:
            body = {"model": self.cfg["claude_model"], "max_tokens": max_tokens, "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}]}
            if tl:
                body["tools"] = tl
            try:
                data = self._post(body)
            except urllib.error.HTTPError as e:
                err = "HTTP %s %s" % (e.code, e.read()[:200].decode("utf-8", "ignore"))
                if e.code == 400 and tl:   # รุ่นนี้ใช้เครื่องมือค้นเว็บแบบนี้ไม่ได้ ลองแบบถัดไป
                    continue
                raise RuntimeError(err)
            us = data.get("usage") or {}
            c["calls"] += 1
            c["in"] += int(us.get("input_tokens", 0))
            c["out"] += int(us.get("output_tokens", 0))
            c["searches"] += int((us.get("server_tool_use") or {}).get("web_search_requests", 0))
            st["claude"] = c
            jsave(STATE, st)
            texts = [b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"]
            text = "\n".join(texts)
            if "{" not in text:
                err = "Claude ตอบไม่เป็น JSON (%s)" % data.get("stop_reason")
                continue
            return text, data.get("model", self.cfg["claude_model"]), bool(tl)
        raise RuntimeError(err or "เรียก Claude ไม่ได้")

    def review(self, rows, gemini_out, regime, date):
        gem = {}
        for v in (gemini_out or {}).get("stocks") or []:
            if isinstance(v, dict) and v.get("symbol"):
                gem[str(v["symbol"]).upper()] = {k: v.get(k) for k in ("verdict", "confidence", "reason", "news", "risk")}
        for r in rows:
            if r["symbol"] in gem:
                r["gemini_view"] = gem[r["symbol"]]
        prompt = (
            "คุณคือคณะกรรมการบริหารความเสี่ยงของกองทุนหุ้นสหรัฐฯ (ไม่ใช้เลเวอเรจ ไม่ short) ตรวจรอบสุดท้ายก่อนแนะนำ"
            "นักลงทุนรายย่อยชาวไทยที่ซื้อผ่าน Webull\n"
            "วันที่ข้อมูลปิดตลาดล่าสุด: %s | ภาวะตลาด (SPY): %s | VIX: %s\n"
            "ข้อมูลราคา/งบมาจาก Yahoo Finance; gemini_view = ความเห็นนักวิเคราะห์อีกคน (อาจผิดได้ ห้ามเชื่อตาม)\n\n"
            "งานของคุณ:\n"
            "1) ใช้ web search ตรวจข่าวสำคัญ 2 สัปดาห์ล่าสุด เฉพาะตัวที่สงสัย (งบ, guidance, คดี, ลดปันผล, ดาวน์เกรด, ดีลใหญ่)"
            " ใช้แหล่งน่าเชื่อถือ: เว็บบริษัท/SEC, Reuters, Bloomberg, CNBC, WSJ, MarketWatch, Yahoo Finance\n"
            "2) ตรวจตัวเลข: จุดตัดขาดทุนต่ำกว่าโซนซื้อจริงไหม, เป้าคุ้มความเสี่ยง (อย่างน้อย 2 เท่า) ไหม, ปันผลจ่ายเกินกำไรไหม,"
            " valuation แพงเกินไหม\n"
            "3) ตัดสิน buy / wait / avoid ด้วยมาตรฐานของคุณเอง ถ้าไม่แน่ใจ = wait; trade ที่ประกาศงบภายใน 5 วัน = wait\n"
            "4) ภาษาไทยเข้าใจง่าย reason ไม่เกิน 160 ตัวอักษร news ไม่เกิน 120 ตัวอักษร ห้ามแต่งข่าว ถ้าไม่เจอให้เว้นว่าง\n"
            "ตอบเป็น JSON อย่างเดียว ไม่มีข้อความอื่น:\n"
            '{"market_note":"ภาพรวมตลาด 1-2 ประโยค","stocks":[{"symbol":"","verdict":"buy|wait|avoid",'
            '"confidence":0-100,"reason":"","news":"","risk":"","source":"ชื่อแหล่งข่าวหลัก"}]}\n\n'
            "ข้อมูล:\n%s" % (date, regime["label"], regime.get("vix"), json.dumps(rows, ensure_ascii=False)))
        text, model, searched = self.call(prompt)
        out = Gemini.parse(text)
        out["model"] = model + (" + web search" if searched else "")
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
    cl = Claude(cfg)
    c_out, c_err = None, ""
    if cl.enabled and short:
        try:
            c_out = cl.review(ai_rows(short), g_out, regime, bar_date)
            log("Claude ตรวจ %d ตัวแล้ว (%s)" % (len(short), c_out.get("model")))
        except Exception as e:
            c_err = str(e)[:200]
            log("Claude ใช้ไม่ได้: " + c_err)
    elif not cl.key:
        c_err = "ยังไม่ได้ใส่ ANTHROPIC_API_KEY"

    def _index(out):
        idx = {}
        for v in (out or {}).get("stocks") or []:
            if isinstance(v, dict) and v.get("symbol"):
                idx[(str(v["symbol"]).upper(), v.get("category"))] = v
                idx.setdefault((str(v["symbol"]).upper(), None), v)
        return idx
    verdicts, c_verdicts = _index(g_out), _index(c_out)

    def _vd(v):
        return v.get("verdict") if v and v.get("verdict") in ("buy", "wait", "avoid") else ("wait" if v else None)

    results = []
    for c in short:
        p = c["pick"]
        v = verdicts.get((c["symbol"], p["cat"])) or verdicts.get((c["symbol"], None))
        cv = c_verdicts.get((c["symbol"], p["cat"])) or c_verdicts.get((c["symbol"], None))
        # AI ที่ทำงานรอบนี้ต้องไม่มีใครค้าน: มีคนบอก avoid = avoid, ทุกคนบอก buy = buy, นอกนั้น = wait
        votes = [x for x in ((_vd(v) if g_out else None), (_vd(cv) if c_out else None)) if x]
        if (g_out and not v) or (c_out and not cv):
            votes.append("wait")   # AI ทำงานแต่ไม่ได้ตอบตัวนี้ = ยังไม่ผ่าน
        if not votes:
            verdict = "buy"        # ไม่มี AI ทำงาน = ใช้ผลระบบอย่างเดียว
        elif "avoid" in votes:
            verdict = "avoid"
        elif all(x == "buy" for x in votes):
            verdict = "buy"
        else:
            verdict = "wait"
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
            "news": (v or {}).get("news", ""), "risk": (v or {}).get("risk", ""),
            "g_verdict": _vd(v), "c_verdict": _vd(cv), "c_confidence": (cv or {}).get("confidence"),
            "c_reason": (cv or {}).get("reason", ""), "c_news": (cv or {}).get("news", ""),
            "c_risk": (cv or {}).get("risk", ""), "c_source": (cv or {}).get("source", ""), **p})
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
           "spy_ret126": round(spy["ret126"], 2),
           "regime": regime, "scanned": len(X), "candidates": len(cands), "reviewed": len(short),
           "gemini": {"on": bool(g_out), "model": (g_out or {}).get("model"), "error": g_err,
                      "market_note": (g_out or {}).get("market_note", "")},
           "claude": {"on": bool(c_out), "model": (c_out or {}).get("model"), "error": c_err,
                      "market_note": (c_out or {}).get("market_note", "")},
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
        L.append("🧠 Gemini ไม่ได้ช่วยรอบนี้ (%s)" % g["error"][:60])
    c = rep.get("claude") or {}
    if c.get("market_note"):
        L.append("🛡️ Claude: " + c["market_note"])
    elif c.get("error"):
        L.append("🛡️ Claude ไม่ได้ตรวจรอบนี้ (%s)" % c["error"][:60])
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
                L.append("   💬 Gemini: " + r["reason"])
            if r.get("c_reason"):
                L.append("   🛡️ Claude: " + r["c_reason"])
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
CSS = """body{font:15px system-ui,Tahoma,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:0 12px 40px}
h1{font-size:19px;margin:12px 0 4px}h2{font-size:16px;margin:16px 0 8px}
.top{position:sticky;top:0;background:#0d1117;padding:8px 0;z-index:5;border-bottom:1px solid #30363d}
.tabs{display:flex;gap:6px;overflow-x:auto;white-space:nowrap}.tabs button{background:#161b22;color:#e6edf3;border:1px solid #30363d;
border-radius:8px;padding:8px 12px;font-size:14px}.tabs button.on{border-color:#58a6ff;color:#58a6ff}
.sec{display:none}.sec.on{display:block}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:12px;margin:10px 0}.muted{color:#8b949e;font-size:13px}
.sym{font-weight:700;font-size:17px}.tag{display:inline-block;border-radius:6px;padding:1px 7px;font-size:12px;margin-left:6px}
.buy{background:#12361f;color:#3fb950}.wait{background:#3a2f0b;color:#d29922}.avoid{background:#3d1214;color:#f85149}
.row{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:6px}.k{color:#8b949e}.g{color:#3fb950}.r{color:#f85149}
.btn{background:#58a6ff;color:#0d1117;border:0;border-radius:8px;padding:10px 14px;font-size:15px;font-weight:600}
.stat{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.stat div{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:10px}
.stat b{display:block;font-size:20px}a{color:#58a6ff}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 4px;border-bottom:1px solid #30363d;text-align:left}\n.cbtn{float:right;background:#21262d;color:#58a6ff;border:1px solid #30363d;border-radius:8px;padding:4px 10px;font-size:13px}\n.rg button{padding:5px 10px;font-size:13px}"""


def _e(s):
    return html.escape(str(s if s is not None else ""))


def _card(r):
    cat = r["cat"]
    tag = {"buy": "ซื้อได้", "wait": "รอจังหวะ", "avoid": "ไม่ควรแตะ"}[r["verdict"]]
    conf = (" %s%%" % r["confidence"]) if r.get("confidence") is not None else ""
    H = ["<div class='card'><span class='sym'>%s</span> <span class='muted'>%s</span>"
         "<span class='tag %s'>%s%s</span><button class='cbtn' onclick=\"openChart('%s')\">📉 กราฟ</button><div class='row'><span>$%s</span><span class='k'>%s</span>"
         "<span class='k'>RSI %s</span><span class='k'>แข็งกว่าตลาด %+.1f%%</span></div>"
         % (_e(r["symbol"]), _e(r["name"]), r["verdict"], tag, conf, _e(r["symbol"]), money(r["price"]), _e(r["kind"]),
            r["rsi"], r["rs"])]
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
    vd = {"buy": "ซื้อ", "wait": "รอ", "avoid": "ไม่แตะ"}
    if r.get("g_verdict") or r.get("c_verdict"):
        H.append("<div class='muted'>ผลโหวต: Gemini %s · Claude %s</div>" % (
            vd.get(r.get("g_verdict"), "-"), vd.get(r.get("c_verdict"), "-")))
    for k, ic in (("reason", "💬 Gemini:"), ("news", "📰"), ("risk", "⚠️"),
                  ("c_reason", "🛡️ Claude:"), ("c_news", "📰"), ("c_risk", "⚠️")):
        if r.get(k):
            H.append("<div>%s %s</div>" % (ic, _e(r[k])))
    if r.get("c_source"):
        H.append("<div class='muted'>แหล่งข่าว (Claude): %s</div>" % _e(r["c_source"]))
    H.append("</div>")
    return "".join(H)


TABS = [("home", "🏠 ภาพรวม"), ("trade", "⚡ เทรด"), ("growth", "🌱 เติบโต"), ("dividend", "💰 ปันผล"),
        ("chart", "📉 กราฟ"), ("track", "📈 ติดตามผล"), ("rules", "📘 เกณฑ์")]


CHART_JS = r"""<script>
let CD=null,CR=130;
const $=id=>document.getElementById(id);
function fm(x){return x==null?'-':(x>=1000?x.toLocaleString('en',{maximumFractionDigits:0}):x.toFixed(2))}
function openChart(s){show('chart');$('csym').value=s;loadChart(s)}
async function loadChart(s){s=(s||$('csym').value||'').trim().toUpperCase();if(!s)return;
 $('cinfo').innerHTML='<div class="muted">กำลังโหลดกราฟ '+s+' ...</div>';$('cbox').innerHTML='';
 try{const r=await fetch('api/chart?s='+encodeURIComponent(s));const d=await r.json();
  if(d.error){$('cinfo').innerHTML='<div class="card r">'+d.error+'</div>';return}CD=d;drawChart();info()}
 catch(e){$('cinfo').innerHTML='<div class="card r">โหลดไม่ได้ ลองใหม่</div>'}}
function setR(n){CR=n;document.querySelectorAll('.rg button').forEach(b=>b.classList.toggle('on',+b.dataset.r==n));if(CD)drawChart()}
function drawChart(){const D=CD,n=D.c.length,k=Math.min(CR,n),s=n-k,A=D.ahead,box=$('cbox');
 const W=Math.max(320,box.clientWidth||340),PR=52,H1=280,H2=46,H=H1+H2+22,slots=k+A,cw=(W-PR)/slots;
 const X=i=>(i-s+.5)*cw;let lo=1e18,hi=-1e18;const take=v=>{if(v!=null&&isFinite(v)){lo=Math.min(lo,v);hi=Math.max(hi,v)}};
 for(let i=s;i<n;i++){take(D.l[i]);take(D.h[i])}
 const ch=D.channel;for(let j=0;j<ch.mid.length;j++){if(ch.start+j>=s){take(ch.up[j]);take(ch.dn[j])}}
 const P=D.plan||{};[P.stop,P.t1,P.t2,P.target,P.review].forEach(v=>{if(v&&v<hi*1.25&&v>lo*.75)take(v)});
 if(D.forecast)D.forecast.range.forEach(take);
 const pad=(hi-lo)*.04;lo-=pad;hi+=pad;const Y=v=>8+(hi-v)/(hi-lo)*(H1-16);
 let g=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" id="csvg" style="touch-action:pan-y">
 <defs><clipPath id="cp"><rect x="0" y="0" width="${W-PR}" height="${H1}"/></clipPath></defs>`;
 for(let t=0;t<=4;t++){const v=lo+(hi-lo)*t/4,y=Y(v);g+=`<line x1="0" x2="${W-PR}" y1="${y}" y2="${y}" stroke="#21262d"/><text x="${W-PR+4}" y="${y+4}" fill="#8b949e" font-size="10">${fm(v)}</text>`}
 g+=`<line x1="${X(n-1)+cw/2}" x2="${X(n-1)+cw/2}" y1="0" y2="${H1}" stroke="#30363d" stroke-dasharray="3 3"/><text x="${X(n-1)+cw/2-3}" y="${H1-4}" fill="#8b949e" font-size="10" text-anchor="end">คาดการณ์ ${A} วัน →</text><g clip-path="url(#cp)">`;
 const cc=ch.slope20_pct>2?'#3fb950':(ch.slope20_pct<-2?'#f85149':'#8b949e');let pu=[],pd=[],pm=[];
 for(let j=0;j<ch.mid.length;j++){const i=ch.start+j;if(i<s)continue;pu.push([X(i),Y(ch.up[j])]);pd.push([X(i),Y(ch.dn[j])]);pm.push([X(i),Y(ch.mid[j])])}
 if(pu.length){g+=`<polygon points="${pu.map(p=>p.join(',')).join(' ')} ${pd.slice().reverse().map(p=>p.join(',')).join(' ')}" fill="${cc}" opacity=".07"/>`;
  [pu,pd].forEach(a=>g+=`<polyline points="${a.map(p=>p.join(',')).join(' ')}" fill="none" stroke="${cc}" stroke-width="1.2" stroke-dasharray="5 3"/>`);
  g+=`<polyline points="${pm.map(p=>p.join(',')).join(' ')}" fill="none" stroke="${cc}" stroke-width=".8" opacity=".6" stroke-dasharray="2 3"/>`}
 if(P.zone){const a=Y(Math.max(...P.zone)),b=Y(Math.min(...P.zone));g+=`<rect x="0" width="${W-PR}" y="${a}" height="${Math.max(2,b-a)}" fill="#58a6ff" opacity=".15"/>`}
 const used=[];const hl=(v,c,t,dash)=>{if(v==null||v<lo||v>hi)return;const y=Y(v);g+=`<line x1="0" x2="${W-PR}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="1" ${dash?'stroke-dasharray="6 4"':''}/>`;
  if(used.some(u=>Math.abs(u-y)<12))return;used.push(y);const tx=`${t} ${fm(v)}`;
  g+=`<rect x="2" y="${y-12}" width="${tx.length*5.6+6}" height="12" rx="3" fill="#0d1117" opacity=".85"/><text x="5" y="${y-3}" fill="${c}" font-size="10">${tx}</text>`};
 hl(P.stop,'#f85149','ตัดขาดทุน');hl(P.review,'#f85149','เลิกถือ');hl(P.t1,'#3fb950','เป้า1');hl(P.t2,'#3fb950','เป้า2');hl(P.target,'#3fb950','เป้า');
 D.support.slice(0,2).forEach(x=>hl(x.price,'#3fb950aa','รับ',1));D.resistance.slice(0,2).forEach(x=>hl(x.price,'#f85149aa','ต้าน',1));
 const ml=(arr,c,w)=>{let p=[];for(let i=s;i<n;i++)if(arr[i]!=null)p.push(X(i)+','+Y(arr[i]));g+=`<polyline points="${p.join(' ')}" fill="none" stroke="${c}" stroke-width="${w}"/>`};
 ml(D.sma200,'#bc8cff',1.5);ml(D.sma50,'#58a6ff',1.3);ml(D.ema20,'#d29922',1.1);
 for(let i=s;i<n;i++){const o=D.o[i],c=D.c[i],u=c>=o,col=u?'#3fb950':'#f85149',x=X(i);
  g+=`<line x1="${x}" x2="${x}" y1="${Y(D.h[i])}" y2="${Y(D.l[i])}" stroke="${col}"/>`;
  const bw=Math.max(1,cw*.65);g+=`<rect x="${x-bw/2}" y="${Y(Math.max(o,c))}" width="${bw}" height="${Math.max(1,Math.abs(Y(o)-Y(c)))}" fill="${col}"/>`}
 D.lines.forEach(L=>{const c=L.type=='up'?'#3fb950':'#f85149';g+=`<line x1="${X(L.x1)}" y1="${Y(L.y1)}" x2="${X(L.x2)}" y2="${Y(L.y2)}" stroke="${c}" stroke-width="2"/>`});
 const F=D.forecast;if(F){const x=X(n-1+A),c=F.view=='ขึ้น'?'#3fb950':(F.view=='ลง'?'#f85149':'#d29922');
  g+=`<line x1="${X(n-1)}" y1="${Y(D.c[n-1])}" x2="${x}" y2="${Y(D.c[n-1]*(1+F.median/100))}" stroke="${c}" stroke-width="2" stroke-dasharray="4 3"/>
  <rect x="${x-5}" y="${Y(F.range[1])}" width="10" height="${Math.max(2,Y(F.range[0])-Y(F.range[1]))}" fill="${c}" opacity=".35"/>`}
 g+=`</g><rect x="${W-PR}" y="${Y(D.price)-8}" width="${PR}" height="16" fill="#58a6ff"/><text x="${W-PR+3}" y="${Y(D.price)+4}" fill="#0d1117" font-size="10" font-weight="700">${fm(D.price)}</text>`;
 let vm=0;for(let i=s;i<n;i++)vm=Math.max(vm,D.v[i]);
 for(let i=s;i<n;i++){const hh=vm?D.v[i]/vm*H2:0;g+=`<rect x="${X(i)-Math.max(1,cw*.65)/2}" y="${H1+H2-hh}" width="${Math.max(1,cw*.65)}" height="${hh}" fill="${D.c[i]>=D.o[i]?'#3fb95066':'#f8514966'}"/>`}
 [s,Math.floor((s+n-1)/2),n-1].forEach(i=>g+=`<text x="${Math.min(X(i),W-PR-60)}" y="${H-4}" fill="#8b949e" font-size="10">${D.dates[i]}</text>`);
 g+='</svg>';box.innerHTML='<div id="ctip" class="muted" style="height:18px">แตะกราฟเพื่อดูราคาแต่ละวัน</div>'+g;
 const sv=$('csvg');const tip=e=>{const r=sv.getBoundingClientRect(),t=e.touches?e.touches[0]:e,i=Math.round((t.clientX-r.left)/cw-.5)+s;
  if(i>=s&&i<n)$('ctip').textContent=`${D.dates[i]}  เปิด ${fm(D.o[i])} สูง ${fm(D.h[i])} ต่ำ ${fm(D.l[i])} ปิด ${fm(D.c[i])}`};
 sv.addEventListener('click',tip);sv.addEventListener('touchmove',tip,{passive:true})}
function info(){const D=CD,F=D.forecast,ch=D.channel,P=D.plan;let h='';
 h+=`<div class="card"><span class="sym">${D.symbol}</span> <span class="muted">${D.name}</span><div class="row"><span>$${fm(D.price)}</span><span class="${D.change_pct>=0?'g':'r'}">${D.change_pct>=0?'+':''}${D.change_pct}%</span><span class="k">RSI ${D.rsi}</span><span class="k">ข้อมูล ${D.date}</span></div>
 <div class="muted" style="margin-top:6px"><span style="color:#d29922">━ EMA20</span> · <span style="color:#58a6ff">━ เส้น 50 วัน</span> · <span style="color:#bc8cff">━ เส้น 200 วัน</span> · <span class="g">━ แนวโน้มขาขึ้น</span> · <span class="r">━ แนวโน้มขาลง</span> · ┅ ช่องแนวโน้ม · <span style="color:#58a6ff">▇ โซนซื้อ</span></div></div>`;
 if(F){const c=F.view=='ขึ้น'?'g':(F.view=='ลง'?'r':'');
  h+=`<div class="card"><b>🔮 คาดการณ์ ${D.ahead} วันทำการ (~1 เดือน): <span class="${c}">${F.view=='ไม่ชัด'?'ยังไม่ชัด':'มีแนวโน้ม'+F.view}</span></b>
  <div class="row"><span>โอกาสขึ้น <b>${F.p_up}%</b></span><span>ค่ากลาง ${F.median>=0?'+':''}${F.median}%</span><span>ช่วงที่น่าจะอยู่ $${fm(F.range[0])}-${fm(F.range[1])}</span></div>
  <div class="muted">จากสถิติหุ้นตัวนี้ 5 ปี: เคยอยู่${F.basis} ${F.samples} วัน แล้ว ${D.ahead} วันถัดมาขึ้น ${F.p_up}% ของครั้ง · รวมกับความชันของช่องแนวโน้ม · เป็นสถิติ ไม่ใช่การรับประกัน</div></div>`}
 const pos=ch.pos<-.6?'ใกล้ขอบล่าง'+(ch.slope20_pct>2?' (ในช่องขาขึ้น = จังหวะซื้อที่ดี)':' (อาจเด้ง แต่แนวโน้มยังไม่ขึ้น)'):(ch.pos>.6?'ใกล้ขอบบน (ระวังย่อ อย่าไล่ซื้อ)':(ch.pos<-1.05?'หลุดขอบล่าง (อ่อนแรงผิดปกติ)':'กลางช่อง'));
 h+=`<div class="card"><b>📐 ช่องแนวโน้ม 60 วัน: <span class="${ch.slope20_pct>2?'g':(ch.slope20_pct<-2?'r':'')}">${ch.label}</span></b><div>ชัน ${ch.slope20_pct>=0?'+':''}${ch.slope20_pct}% ต่อ 20 วัน · ราคาตอนนี้${pos}</div>`;
 D.lines.forEach(L=>h+=`<div>${L.type=='up'?'🟢':'🔴'} ${L.label} ตอนนี้อยู่ที่ $${fm(L.now)} → ${L.type=='up'?(L.broken?'<span class="r">หลุดแล้ว (สัญญาณอ่อน)</span>':'<span class="g">ยังยืนเหนือเส้น</span>'):(L.broken?'<span class="g">ทะลุขึ้นแล้ว (สัญญาณกลับตัว)</span>':'<span class="r">ยังกดอยู่ใต้เส้น</span>')}</div>`);
 h+=`<div class="row"><span class="g">แนวรับ: ${D.support.map(x=>'$'+fm(x.price)).join(', ')||'-'}</span><span class="r">แนวต้าน: ${D.resistance.map(x=>'$'+fm(x.price)).join(', ')||'-'}</span></div></div>`;
 if(P){h+=`<div class="card"><b>📋 แผนจากระบบสแกน (${P.kind})</b><div class="row">`+
  (P.zone?`<span>โซนซื้อ $${fm(P.zone[0])}-${fm(P.zone[1])}</span>`:'')+(P.stop?`<span class="r">ตัดขาดทุน $${fm(P.stop)}</span>`:'')+(P.t1?`<span class="g">เป้า1 $${fm(P.t1)}</span><span class="g">เป้า2 $${fm(P.t2)}</span>`:'')+
  (P.target?`<span class="g">เป้า $${fm(P.target)}</span>`:'')+(P.review?`<span class="r">เลิกถือถ้าต่ำกว่า $${fm(P.review)}</span>`:'')+`</div>${P.reason?'<div>💬 '+P.reason+'</div>':''}</div>`}
 h+=`<div class="card"><b>✅ เช็กลิสต์มืออาชีพ ผ่าน ${D.score}</b>`+D.checklist.map(x=>`<div style="margin-top:4px">${x.ok?'✅':'❌'} ${x.label} <span class="muted">${x.detail}</span></div>`).join('')+'</div>';
 $('cinfo').innerHTML=h}
addEventListener('resize',()=>{if(CD)drawChart()});
</script>
"""


def render(rep, msg=""):
    head = ("<!doctype html><html lang='th'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><title>ฝ่ายสแกนหุ้น US</title>"
            "<style>%s</style></head><body>" % CSS)
    if not rep:
        return head + ("<h1>📊 ฝ่ายสแกนหุ้น US</h1><div class='card'>ยังไม่มีผลสแกน กำลังสแกนรอบแรก "
                       "รอ 2-3 นาทีแล้วรีเฟรช</div><a href='logout'>ออกจากระบบ</a></body></html>")
    rg, g = rep["regime"], rep["gemini"]
    H = [head, "<div class='top'><div style='display:flex;justify-content:space-between;align-items:center'>"
         "<h1>📊 ฝ่ายสแกนหุ้น US</h1><a href='logout' class='muted'>ออกจากระบบ</a></div><div class='tabs'>"]
    H += ["<button data-t='%s'>%s</button>" % (k, n) for k, n in TABS]
    H.append("</div></div>")

    # ---- ภาพรวม
    fin = rep["final"]
    st = rep.get("stats")
    H.append("<div class='sec' id='home'>")
    H.append("<div class='muted' style='margin-top:8px'>สแกนล่าสุด %s น. · ข้อมูลปิดตลาด %s · %d ตัว · %s</div>"
             % (_e(rep["time"]), _e(thai_date(rep["bar_date"])), rep["scanned"], _e(rep["reason"])))
    H.append("<div class='stat' style='margin-top:10px'><div>ตลาด<b>%s %s</b></div><div>VIX (ความกลัว)<b>%s</b></div>"
             "<div>แนะนำวันนี้<b>%d ตัว</b></div><div>อัตราชนะไม้เทรด<b>%s</b></div></div>"
             % (rg["icon"], _e(rg["label"].split(" ·")[0]), rg.get("vix") or "-", sum(len(v) for v in fin.values()),
                ("%d%% (%d ไม้)" % (st["win_rate"], st["trades"])) if st else "ยังไม่มี"))
    H.append("<div class='card'>🧠 <b>Gemini</b>: %s%s</div>" % (
        _e(g.get("market_note") or ("ไม่ได้ใช้รอบนี้ — " + (g.get("error") or ""))),
        ("<div class='muted'>%s</div>" % _e(g["model"])) if g.get("model") else ""))
    c = rep.get("claude") or {}
    H.append("<div class='card'>🛡️ <b>Claude</b> (ตรวจซ้ำ): %s%s</div>" % (
        _e(c.get("market_note") or ("ไม่ได้ใช้รอบนี้ — " + (c.get("error") or "ยังไม่เคยรัน"))),
        ("<div class='muted'>%s</div>" % _e(c["model"])) if c.get("model") else ""))
    if rep["alerts"]:
        H.append("<h2>🔔 ติดตามตัวเดิม</h2>" + "".join("<div class='card'>%s</div>" % _e(a) for a in rep["alerts"]))
    H.append("<h2>✅ ตัวที่แนะนำวันนี้</h2>")
    for cat in ("trade", "growth", "dividend"):
        ic, name = CAT[cat]
        rows = fin.get(cat) or []
        H.append("<div class='card'><b>%s %s</b><div class='row'>%s</div></div>" % (
            ic, name, " · ".join("<span><b>%s</b> $%s</span>" % (_e(r["symbol"]), money(r["price"])) for r in rows)
            or "<span class='muted'>วันนี้ไม่มี</span>"))
    H.append("<form method='post' action='scan' style='margin-top:12px'><button class='btn'>🔄 สแกนใหม่ตอนนี้</button>"
             " <span class='muted'>%s</span></form></div>" % _e(msg or "สแกนเองไม่ส่ง LINE (LINE ส่งเองทุกเช้า)"))

    # ---- 3 กลุ่ม
    for cat in ("trade", "growth", "dividend"):
        ic, name = CAT[cat]
        rows = [r for r in rep["results"] if r["cat"] == cat]
        H.append("<div class='sec' id='%s'><h2>%s %s</h2>" % (cat, ic, name))
        H.append("".join(_card(r) for r in rows) or "<div class='muted'>ไม่มีตัวที่เข้าเกณฑ์วันนี้</div>")
        H.append("</div>")

    # ---- กราฟ
    chips = list(dict.fromkeys([r["symbol"] for v in fin.values() for r in v] + [p["symbol"] for p in rep["tracking"]]))
    H.append("<div class='sec' id='chart'><h2>📉 กราฟ + วิเคราะห์แนวโน้ม</h2><div style='display:flex;gap:6px'>"
             "<input id='csym' placeholder='ชื่อหุ้น เช่น NVDA' autocapitalize='characters' "
             "style='flex:1;padding:10px;border-radius:8px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-size:16px'"
             " onkeydown=\"if(event.key=='Enter')loadChart()\"><button class='btn' onclick='loadChart()'>ดู</button></div>"
             "<div class='tabs' style='margin-top:8px'>%s</div>"
             "<div class='tabs rg' style='margin-top:8px'><button data-r='66' onclick='setR(66)'>3 เดือน</button>"
             "<button data-r='130' class='on' onclick='setR(130)'>6 เดือน</button><button data-r='260' onclick='setR(260)'>1 ปี</button></div>"
             "<div id='cbox' style='margin-top:6px'></div><div id='cinfo'><div class='muted'>เลือกหุ้นด้านบน หรือกดปุ่ม 📉 กราฟ ในการ์ดหุ้น</div></div></div>"
             % "".join("<button onclick=\"openChart('%s')\">%s</button>" % (_e(x), _e(x)) for x in chips[:20]))

    # ---- ติดตามผล
    H.append("<div class='sec' id='track'><h2>📈 ติดตามผลคำแนะนำ</h2>")
    if st:
        H.append("<div class='stat'><div>ไม้เทรดที่จบ<b>%d</b></div><div>ชนะ<b>%d%%</b></div><div>เฉลี่ย/ไม้<b>%+.2f%%</b></div>"
                 "<div>รวม<b>%+.1f%%</b></div></div>" % (st["trades"], st["win_rate"], st["avg_pct"], st["sum_pct"]))
    TH_ST = {"waiting": "รอเข้าโซน", "open": "ถืออยู่", "t1": "ขายครึ่งแล้ว", "watch": "ถือยาว", "target": "ถึงเป้าแล้ว"}
    if rep["tracking"]:
        H.append("<div class='card'><table><tr><th>หุ้น</th><th>วันที่</th><th>สถานะ</th><th>แผน</th></tr>")
        for p in rep["tracking"]:
            plan = ("stop %s · เป้า %s/%s" % (money(p["stop"]), money(p["t1"]), money(p["t2"]))) if p["cat"] == "trade" \
                else "เลิกถือ < %s%s" % (money(p["review"]), (" · เป้า %s" % money(p["target"])) if p.get("target") else "")
            H.append("<tr><td>%s %s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (CAT[p["cat"]][0], _e(p["symbol"]), _e(thai_date(p["date"])), TH_ST.get(p["status"], p["status"]), _e(plan)))
        H.append("</table></div>")
    else:
        H.append("<div class='muted'>ยังไม่มีตัวที่ติดตาม (เริ่มนับจากข้อความ LINE รอบแรก)</div>")
    if rep["closed"]:
        H.append("<h2>ไม้ที่จบแล้ว</h2><div class='card'><table><tr><th>หุ้น</th><th>แนะนำ</th><th>จบ</th><th>ผล</th></tr>")
        for p in reversed(rep["closed"]):
            cls = "g" if (p.get("result_pct") or 0) > 0 else "r"
            H.append("<tr><td>%s %s</td><td>%s</td><td>%s</td><td class='%s'>%+.1f%%</td></tr>"
                     % (CAT[p["cat"]][0], _e(p["symbol"]), _e(thai_date(p["date"])), _e(thai_date(p.get("exit_day"))),
                        cls, p.get("result_pct") or 0))
        H.append("</table></div>")
    H.append("</div>")

    # ---- เกณฑ์
    c = load_cfg()
    H.append("<div class='sec' id='rules'><h2>📘 เกณฑ์ที่ใช้คัด</h2><div class='card'>"
             "<b>⚡ เทรดเน้นกำไร</b> ขาขึ้นชัด (ราคา &gt; เส้น 50 &gt; เส้น 200 วัน) + จังหวะย่อ/ทะลุฐาน/รอทะลุ, "
             "เสี่ยง 2-10%%/ไม้, ขายครึ่งที่เป้า 1 (2 เท่าของความเสี่ยง) ที่เหลือเป้า 2 (3 เท่า), ถือไม่เกิน %d วัน, "
             "ใกล้ประกาศงบ 5 วัน = รอ</div><div class='card'><b>🌱 ถือยาวเน้นเติบโต</b> รายได้โต ≥%s%%, มีกำไร, "
             "ราคาเหนือเส้น 200 วัน, ไม่แพงเกิน (Fwd P/E ≤ 80) · เลิกถือเมื่อปิดต่ำกว่าเส้น 200 วัน 3%%</div>"
             "<div class='card'><b>💰 ถือยาวเน้นปันผล</b> ปันผล ≥%s%%/ปี, จ่ายสม่ำเสมอ, จ่ายไม่เกิน 85%% ของกำไร, "
             "ราคาไม่พัง · เลิกถือเมื่อหลุดแนวรับ 8%% หรือบริษัทลดปันผล</div>"
             "<div class='card'><b>🧠 Gemini</b> ค้นข่าว 2 สัปดาห์ล่าสุดด้วย Google Search แล้วตัดสิน ซื้อ / รอ / ไม่ควรแตะ</div>"
             "<div class='muted'>ข้อมูลจาก Yahoo Finance · ไม่ใช่คำแนะนำการลงทุน</div></div>"
             % (c["track_trade_days"], c["min_revenue_growth"], c["min_dividend_yield"]))

    H.append("""<script>
const b=[...document.querySelectorAll('.top .tabs button')];
function show(t){b.forEach(x=>x.classList.toggle('on',x.dataset.t==t));
document.querySelectorAll('.sec').forEach(s=>s.classList.toggle('on',s.id==t));try{localStorage.tab=t}catch(e){}}
b.forEach(x=>x.onclick=()=>show(x.dataset.t));let t='home';try{t=localStorage.tab||'home'}catch(e){}show(t);
</script>""" + CHART_JS + "</body></html>")
    return "".join(H)


def chart_json(sym):
    import chart_analysis
    sym = (sym or "").upper().strip()
    rep = jload(REPORT, {}) or {}
    plan = next((r for r in rep.get("results", []) if r["symbol"] == sym and r["verdict"] != "avoid"), None) \
        or next((r for r in rep.get("results", []) if r["symbol"] == sym), None)
    if plan:
        plan = {k: plan.get(k) for k in ("kind", "zone", "stop", "t1", "t2", "target", "review", "reason")}
    try:
        return chart_analysis.analyze(sym, plan, rep.get("regime"), jload(FUND, {}).get(sym), rep.get("spy_ret126"))
    except Exception as e:
        return {"error": "ดูกราฟ %s ไม่ได้: %s" % (sym, str(e)[:120])}


LOGIN_PAGE = r"""<!DOCTYPE html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>เข้าสู่ระบบ</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;font:15px system-ui,Tahoma,sans-serif}
form{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:min(320px,90vw)}
input,button{width:100%;box-sizing:border-box;padding:12px;margin-top:10px;border-radius:8px;font-size:16px}
input{background:#0d1117;color:#e6edf3;border:1px solid #30363d}button{background:#58a6ff;border:0;font-weight:600}
p{color:#f85149;min-height:1em}</style></head><body>
<form method="post" action="login"><h2>📊 ฝ่ายสแกนหุ้น US</h2>
<input type="password" name="password" placeholder="รหัสผ่าน" autofocus required>
<button>เข้าสู่ระบบ</button><p>%MSG%</p></form></body></html>"""


def serve(cfg):
    pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if len(pw) < 10:
        log("ไม่เปิดหน้าเว็บ: ต้องตั้ง DASHBOARD_PASSWORD อย่างน้อย 10 ตัว")
        return
    state = {"last_manual": 0, "msg": ""}
    sessions, lk = {}, threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="text/html; charset=utf-8", code=200, headers=None):
            b = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _go(self, where="./", cookie=None):
            self._send("", "text/plain", 303, dict({"Location": where}, **({"Set-Cookie": cookie} if cookie else {})))

        def _authed(self):
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            tok = c["ssid"].value if "ssid" in c else ""
            with lk:
                exp = sessions.get(tok)
                if exp and exp > time.time():
                    return True
                sessions.pop(tok, None)
            return False

        def do_GET(self):
            if not self._authed():
                return self._send(LOGIN_PAGE.replace("%MSG%", ""))
            if self.path.startswith("/logout"):
                return self._go("./", "ssid=; Max-Age=0; Path=/")
            if self.path.startswith("/api/chart"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._send(json.dumps(chart_json((qs.get("s") or [""])[0]), ensure_ascii=False),
                                  "application/json; charset=utf-8")
            if self.path.startswith("/report.json"):
                return self._send(json.dumps(jload(REPORT, {}), ensure_ascii=False), "application/json")
            self._send(render(jload(REPORT, None), state["msg"]))

        def do_POST(self):
            ip = self.client_address[0]
            if self.path.startswith("/login"):
                length = min(int(self.headers.get("Content-Length", 0) or 0), 2000)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
                given = (form.get("password") or [""])[0].strip()
                if hmac.compare_digest(given.encode(), pw.encode()):
                    tok = secrets.token_urlsafe(32)
                    with lk:
                        sessions[tok] = time.time() + 7 * 24 * 3600
                    return self._go("./", "ssid=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (tok, 7 * 24 * 3600))
                log("ใส่รหัสหน้าเว็บผิดจาก %s" % ip)
                time.sleep(1)
                return self._send(LOGIN_PAGE.replace("%MSG%", "รหัสผ่านไม่ถูกต้อง"))
            if not self._authed():
                return self._go()
            wait = cfg["manual_scan_cooldown_min"] * 60 - (time.time() - state["last_manual"])
            if _lock.locked():
                state["msg"] = "กำลังสแกนอยู่ รอ 2-3 นาทีแล้วรีเฟรช"
            elif wait > 0:
                state["msg"] = "เพิ่งสแกนไป รออีก %d นาที" % (wait // 60 + 1)
            else:
                state["last_manual"] = time.time()
                state["msg"] = "เริ่มสแกนแล้ว รอ 2-3 นาทีแล้วรีเฟรช (ไม่ส่ง LINE)"
                threading.Thread(target=_safe_scan, args=(load_cfg(), False, "สแกนเองจากหน้าเว็บ"), daemon=True).start()
            self._go()

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
