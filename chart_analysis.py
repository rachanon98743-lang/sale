"""
chart_analysis.py — ข้อมูลกราฟ + วิเคราะห์ทางเทคนิคให้หน้าเว็บสแกนหุ้น (scanner.py)

ให้ข้อมูล:
  - แท่งเทียนรายวัน + เส้น EMA20 / SMA50 / SMA200 + วอลุ่ม
  - ช่องแนวโน้ม (Regression channel 60 วัน ±2 SD) และเส้นที่ยื่นไปข้างหน้า 20 วัน
  - เส้นแนวโน้มขาขึ้น (ลากผ่านจุดต่ำที่ยกตัว) / ขาลง (ลากผ่านจุดสูงที่ต่ำลง)
  - แนวรับ/แนวต้านจากจุดกลับตัวที่ราคาเคยแตะซ้ำ
  - คาดการณ์ 20 วันทำการ: ดูย้อนหลัง 5 ปีว่าเวลาหุ้นตัวนี้อยู่ในสภาพเดียวกับตอนนี้ 20 วันถัดมาขึ้นกี่ %
  - เช็กลิสต์มืออาชีพ 12 ข้อ
ทั้งหมดเป็นสถิติย้อนหลัง ไม่ใช่การรับประกันอนาคต
"""

import math
import re
import threading
import time

_cache, _lk = {}, threading.Lock()
SYM_RE = re.compile(r"^[A-Z0-9.\-^]{1,10}$")


def _fetch(sym):
    with _lk:
        hit = _cache.get(sym)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    import yfinance as yf
    d = yf.download(sym, period="5y", interval="1d", auto_adjust=True, progress=False, multi_level_index=False)
    d = d.dropna(subset=["Close", "High", "Low"])
    with _lk:
        _cache[sym] = (time.time(), d)
        if len(_cache) > 60:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    return d


def _nz(x, nd=2):
    try:
        x = float(x)
        return None if math.isnan(x) else round(x, nd)
    except (TypeError, ValueError):
        return None


def _pivots(h, lo, start, w=5):
    """จุดกลับตัว: สูงสุด/ต่ำสุดในช่วง ±w แท่ง"""
    highs, lows = [], []
    for i in range(max(start, w), len(h) - w):
        if h[i] == max(h[i - w:i + w + 1]):
            highs.append(i)
        if lo[i] == min(lo[i - w:i + w + 1]):
            lows.append(i)
    return highs, lows


def _levels(points, price, below):
    """รวมจุดกลับตัวที่ใกล้กัน (ห่างไม่เกิน 1.5%) เป็นแนวรับ/ต้าน เอา 3 ระดับที่ใกล้ราคาที่สุด"""
    pts = sorted(points)
    groups = []
    for p in pts:
        if groups and abs(p / groups[-1][-1] - 1) <= 0.015:
            groups[-1].append(p)
        else:
            groups.append([p])
    lv = [(sum(g) / len(g), len(g)) for g in groups]
    lv = [x for x in lv if (x[0] < price if below else x[0] > price)]
    lv.sort(key=lambda x: abs(x[0] - price))
    return [{"price": round(p, 2), "touches": n} for p, n in lv[:3]]


def analyze(sym, plan=None, regime=None, fund=None, spy=None):
    import numpy as np
    import pandas as pd
    sym = sym.upper().strip()
    if not SYM_RE.match(sym):
        raise ValueError("ชื่อหุ้นไม่ถูกต้อง")
    d = _fetch(sym)
    if len(d) < 220:
        raise ValueError("ไม่พบข้อมูล %s หรือข้อมูลน้อยเกินไป" % sym)
    c, h, lo, o, v = (d[k].astype(float) for k in ("Close", "High", "Low", "Open", "Volume"))
    ema20 = c.ewm(span=20, adjust=False).mean()
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    n = len(c)
    C = float(c.iloc[-1])
    SHOW = min(260, n)
    s0 = n - SHOW
    dates = [str(x)[:10] for x in d.index[s0:]]

    # ---- ช่องแนวโน้ม 60 วัน (regression บน log ราคา)
    N, AHEAD = 60, 20
    y = np.log(c.values[-N:])
    x = np.arange(N)
    slope, icpt = np.polyfit(x, y, 1)
    sd = float(np.std(y - (slope * x + icpt)))
    xs = np.arange(N + AHEAD)
    mid = np.exp(slope * xs + icpt)
    chan = {"start": SHOW - N, "mid": [round(float(m), 2) for m in mid],
            "up": [round(float(m * math.exp(2 * sd)), 2) for m in mid],
            "dn": [round(float(m * math.exp(-2 * sd)), 2) for m in mid]}
    slope20 = (math.exp(slope * AHEAD) - 1) * 100
    chan["slope20_pct"] = round(slope20, 1)
    chan["label"] = "ช่องขาขึ้น" if slope20 > 2 else ("ช่องขาลง" if slope20 < -2 else "ช่องไซด์เวย์")
    pos = (math.log(C) - (slope * (N - 1) + icpt)) / (2 * sd) if sd else 0
    chan["pos"] = round(pos, 2)    # -1 = ขอบล่าง, 0 = กลาง, +1 = ขอบบน

    # ---- เส้นแนวโน้มจากจุดกลับตัว (120 วันล่าสุด)
    hv, lv_ = h.values, lo.values
    ph, pl = _pivots(hv, lv_, n - 160)
    lines = []
    if len(pl) >= 2:
        a, b = pl[-2], pl[-1]
        if lv_[b] > lv_[a]:
            k = (lv_[b] - lv_[a]) / (b - a)
            now = lv_[a] + k * (n - 1 - a)
            lines.append({"type": "up", "label": "เส้นแนวโน้มขาขึ้น", "x1": a - s0, "y1": round(lv_[a], 2),
                          "x2": n - 1 + AHEAD - s0, "y2": round(lv_[a] + k * (n - 1 + AHEAD - a), 2),
                          "now": round(now, 2), "broken": C < now})
    if len(ph) >= 2:
        a, b = ph[-2], ph[-1]
        if hv[b] < hv[a]:
            k = (hv[b] - hv[a]) / (b - a)
            now = hv[a] + k * (n - 1 - a)
            lines.append({"type": "down", "label": "เส้นแนวโน้มขาลง", "x1": a - s0, "y1": round(hv[a], 2),
                          "x2": n - 1 + AHEAD - s0, "y2": round(hv[a] + k * (n - 1 + AHEAD - a), 2),
                          "now": round(now, 2), "broken": C > now})
    ph2, pl2 = _pivots(hv, lv_, n - 260)
    sup = _levels([lv_[i] for i in pl2], C, True)
    res = _levels([hv[i] for i in ph2], C, False)

    # ---- คาดการณ์ 20 วัน จากสถิติย้อนหลังของหุ้นตัวนี้เอง
    def state(i, full=True):
        r = rsi.iloc[i]
        b = 0 if r < 40 else (1 if r < 60 else (2 if r < 70 else 3))
        s = (c.iloc[i] > sma200.iloc[i], c.iloc[i] > sma50.iloc[i], sma50.iloc[i] > sma200.iloc[i])
        return s + (b,) if full else s
    fwd = (c.shift(-AHEAD) / c - 1) * 100
    fc = None
    for full in (True, False):
        cur = state(n - 1, full)
        rets = [float(fwd.iloc[i]) for i in range(200, n - AHEAD) if state(i, full) == cur]
        if len(rets) >= 30 or not full:
            break
    if len(rets) >= 15:
        arr = np.array(rets)
        p_up = float((arr > 0).mean())
        med = float(np.median(arr))
        fc = {"samples": len(rets), "p_up": round(p_up * 100), "median": round(med, 1),
              "p25": round(float(np.percentile(arr, 25)), 1), "p75": round(float(np.percentile(arr, 75)), 1),
              "basis": "สภาพเดียวกัน" + (" (รวม RSI)" if full else " (ไม่รวม RSI ข้อมูลน้อย)")}
        votes = (1 if p_up >= 0.58 else (-1 if p_up <= 0.45 else 0)) + (1 if slope20 > 2 else (-1 if slope20 < -2 else 0))
        fc["view"] = "ขึ้น" if votes >= 1 and med > 0 else ("ลง" if votes <= -1 and med < 0 else "ไม่ชัด")
        fc["range"] = [round(C * (1 + fc["p25"] / 100), 2), round(C * (1 + fc["p75"] / 100), 2)]

    # ---- เช็กลิสต์มืออาชีพ
    hi52 = float(h.iloc[-252:].max())
    vol50 = float(v.iloc[-51:-1].mean() or 0)
    rs = None
    if spy is not None:
        rs = (C / float(c.iloc[-127]) - 1) * 100 - spy
    S200, S50, E20, R = float(sma200.iloc[-1]), float(sma50.iloc[-1]), float(ema20.iloc[-1]), float(rsi.iloc[-1])
    chk = [
        ("ราคาอยู่เหนือเส้น 200 วัน (แนวโน้มหลักขาขึ้น)", C > S200, "$%.2f vs $%.2f" % (C, S200)),
        ("เส้น 200 วันชี้ขึ้น", S200 > float(sma200.iloc[-22]), ""),
        ("ราคาอยู่เหนือเส้น 50 วัน (แนวโน้มกลางขาขึ้น)", C > S50, "$%.2f" % S50),
        ("เส้น 50 วัน อยู่เหนือ 200 วัน (Golden zone)", S50 > S200, ""),
        ("ราคาอยู่เหนือ EMA20 (แรงซื้อระยะสั้น)", C > E20, "$%.2f" % E20),
        ("RSI ไม่ร้อนเกิน (ต่ำกว่า 70)", R < 70, "RSI %.0f" % R),
        ("ใกล้จุดสูงสุด 52 สัปดาห์ (ห่างไม่เกิน 15%)", C >= hi52 * 0.85, "%.1f%% จากจุดสูงสุด" % ((C / hi52 - 1) * 100)),
        ("วอลุ่มวันล่าสุดไม่แผ่ว (≥ 80% ของค่าเฉลี่ย)", vol50 and float(v.iloc[-1]) >= vol50 * 0.8,
         "%.0f%% ของค่าเฉลี่ย 50 วัน" % (float(v.iloc[-1]) / vol50 * 100 if vol50 else 0)),
        ("อยู่ในช่องขาขึ้น / ยังไม่หลุดเส้นแนวโน้ม", slope20 > 0 and not any(l["type"] == "up" and l["broken"] for l in lines),
         chan["label"]),
        ("ไม่ติดแนวต้านใกล้เกินไป (ห่าง ≥ 3%)", not res or res[0]["price"] >= C * 1.03,
         ("แนวต้าน $%.2f" % res[0]["price"]) if res else "ไม่มีแนวต้านใกล้"),
    ]
    if rs is not None:
        chk.append(("แข็งกว่าตลาด (S&P 500) 6 เดือน", rs > 0, "%+.1f%%" % rs))
    if regime:
        chk.append(("ตลาดรวมเป็นขาขึ้น", regime.get("state") == "bull", regime.get("label", "")))
    f = fund or {}
    if f.get("revenueGrowth") is not None:
        chk.append(("รายได้โต ≥ 10%", (f["revenueGrowth"] or 0) >= 0.10, "%.1f%%" % ((f["revenueGrowth"] or 0) * 100)))
    if f.get("profitMargins") is not None:
        chk.append(("บริษัทมีกำไร", (f["profitMargins"] or 0) > 0, "อัตรากำไร %.1f%%" % ((f["profitMargins"] or 0) * 100)))
    checklist = [{"label": a, "ok": bool(b), "detail": c_} for a, b, c_ in chk]
    passed = sum(1 for x in checklist if x["ok"])

    return _clean({
        "symbol": sym, "name": f.get("shortName") or sym, "price": round(C, 2), "date": dates[-1],
        "change_pct": round((C / float(c.iloc[-2]) - 1) * 100, 2), "atr": _nz(atr.iloc[-1]), "rsi": round(R),
        "dates": dates, "o": [_nz(x) for x in o.iloc[s0:]], "h": [_nz(x) for x in h.iloc[s0:]],
        "l": [_nz(x) for x in lo.iloc[s0:]], "c": [_nz(x) for x in c.iloc[s0:]], "v": [int(x) for x in v.iloc[s0:].fillna(0)],
        "ema20": [_nz(x) for x in ema20.iloc[s0:]], "sma50": [_nz(x) for x in sma50.iloc[s0:]],
        "sma200": [_nz(x) for x in sma200.iloc[s0:]],
        "channel": chan, "lines": lines, "support": sup, "resistance": res, "ahead": AHEAD,
        "forecast": fc, "checklist": checklist, "score": "%d/%d" % (passed, len(checklist)),
        "plan": plan,
    })


def _clean(o):
    """แปลงชนิดข้อมูล numpy ให้เป็น JSON ได้"""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        o = o.item()
    if isinstance(o, float):
        return None if math.isnan(o) else round(o, 4)
    return o
