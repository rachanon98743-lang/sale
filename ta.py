"""
ta.py — ชุดเครื่องมือวิเคราะห์กราฟแบบนักเทรดมืออาชีพ (ไลบรารีมาตรฐาน Python อย่างเดียว)

หลักที่ใช้ (long-only สำหรับ spot):
  1. เทรดตามแนวโน้มใหญ่ (multi-timeframe)      : timeframe ใหญ่กว่า 1 ขั้นต้องเป็นขาขึ้น
  2. โครงสร้างตลาด (market structure)          : ยอดสูงขึ้น/ฐานสูงขึ้น (HH/HL)
  3. เส้นค่าเฉลี่ย + ความแรงของแนวโน้ม          : EMA20 > EMA50, ADX > 20, +DI > -DI
  4. โมเมนตัม                                  : MACD histogram เป็นบวกและเพิ่มขึ้น, RSI ในโซนแข็งแรงแต่ไม่ร้อนเกิน
  5. วอลุ่มยืนยัน                               : วอลุ่มสูงกว่าค่าเฉลี่ย หรือ OBV ขาขึ้น
  6. แข็งแรงกว่าตลาด (relative strength)        : ขึ้นดีกว่า BTC
  7. แนวรับ/แนวต้านจากจุดกลับตัว (swing pivots)  : มีที่ว่างถึงแนวต้านอย่างน้อย 2 เท่าของความเสี่ยง
  8. แท่งเทียนยืนยัน                            : bullish engulfing / hammer / ปิดแข็งแรง
  9. ไม่ไล่ราคา                                 : ห่าง EMA20 ไม่เกิน 3 ATR, RSI ไม่เกิน 75
 10. จุดตัดขาดทุนตามโครงสร้าง                   : ใต้แนวรับ/swing low ลบ 0.3 ATR (ไม่ใช่ % ตายตัว)

ทุกค่าคำนวณจากข้อมูลถึงแท่งที่ i เท่านั้น (ไม่แอบดูอนาคต) จึงใช้ทดสอบย้อนหลังด้วยกฎเดียวกับของจริงได้
"""

import math


# ===================================================================== indicators (คืนลิสต์ยาวเท่าข้อมูล)
def sma(a, n):
    out, s = [None] * len(a), 0.0
    for i, x in enumerate(a):
        s += x
        if i >= n:
            s -= a[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(a, n):
    out, e, k = [None] * len(a), None, 2 / (n + 1)
    for i, x in enumerate(a):
        if x is None:
            continue
        e = x if e is None else x * k + e * (1 - k)
        out[i] = e
    for i in range(min(len(a), n - 1)):
        out[i] = None
    return out


def rsi(a, n=14):
    out, g, l = [None] * len(a), 0.0, 0.0
    for i in range(1, len(a)):
        d = a[i] - a[i - 1]
        up, dn = max(d, 0), max(-d, 0)
        if i <= n:
            g += up / n
            l += dn / n
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
        if i >= n:
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(h, l, c, n=14):
    out, a = [None] * len(c), None
    for i in range(1, len(c)):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        if i >= n:
            out[i] = a
    return out


def macd(c, fast=12, slow=26, sig=9):
    ef, es = ema(c, fast), ema(c, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    first = next((i for i, x in enumerate(line) if x is not None), len(line))
    sg = [None] * first + ema([x for x in line[first:]], sig)
    hist = [x - y if x is not None and y is not None else None for x, y in zip(line, sg)]
    return line, sg, hist


def bollinger(c, n=20, k=2.0):
    mid, up, lo, width = [None] * len(c), [None] * len(c), [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
        width[i] = (4 * k * sd / 2) / m if m else 0
    return mid, up, lo, width


def adx(h, l, c, n=14):
    """คืน (adx, +DI, -DI) แบบ Wilder"""
    L = len(c)
    out, pdi, mdi = [None] * L, [None] * L, [None] * L
    tr_s = p_s = m_s = None
    dx_s = None
    for i in range(1, L):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm = up if up > dn and up > 0 else 0.0
        mdm = dn if dn > up and dn > 0 else 0.0
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if tr_s is None:
            tr_s, p_s, m_s = tr, pdm, mdm
        else:
            tr_s = tr_s - tr_s / n + tr
            p_s = p_s - p_s / n + pdm
            m_s = m_s - m_s / n + mdm
        if i < n or tr_s <= 0:
            continue
        pdi[i], mdi[i] = 100 * p_s / tr_s, 100 * m_s / tr_s
        s = pdi[i] + mdi[i]
        dx = 100 * abs(pdi[i] - mdi[i]) / s if s else 0
        dx_s = dx if dx_s is None else (dx_s * (n - 1) + dx) / n
        if i >= 2 * n:
            out[i] = dx_s
    return out, pdi, mdi


def obv(c, v):
    out, x = [0.0] * len(c), 0.0
    for i in range(1, len(c)):
        x += v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0
        out[i] = x
    return out


def pivots(h, l, left=3, right=3):
    """จุดกลับตัว: คืน (ยอด, ฐาน) เป็นลิสต์ (แท่งที่ยืนยันแล้ว, แท่งที่เกิด, ราคา)
    ยืนยันเมื่อผ่านไปอีก right แท่ง จึงไม่แอบดูอนาคตตอนทดสอบย้อนหลัง"""
    highs, lows = [], []
    for i in range(left, len(h) - right):
        if h[i] == max(h[i - left:i + right + 1]):
            highs.append((i + right, i, h[i]))
        if l[i] == min(l[i - left:i + right + 1]):
            lows.append((i + right, i, l[i]))
    return highs, lows


def levels_at(i, piv_h, piv_l, price, atr_v, lookback=200):
    """แนวรับ/แนวต้าน ณ แท่ง i: รวมจุดกลับตัวที่ราคาใกล้กัน (ไม่เกิน 0.6 ATR) เป็นโซนเดียว
    โซนที่ถูกทดสอบหลายครั้ง = แข็งแรง คืน (แนวรับใต้ราคา, แนวต้านเหนือราคา) เรียงจากใกล้สุด"""
    pts = [p for (conf, at, p) in piv_h + piv_l if conf <= i and at >= i - lookback]
    if not pts or not atr_v:
        return [], []
    pts.sort()
    zones, cur = [], [pts[0]]
    for p in pts[1:]:
        if p - cur[-1] <= atr_v * 0.6:
            cur.append(p)
        else:
            zones.append(cur)
            cur = [p]
    zones.append(cur)
    lv = [(sum(z) / len(z), len(z)) for z in zones]
    sup = sorted([x for x in lv if x[0] < price], key=lambda x: -x[0])
    res = sorted([x for x in lv if x[0] > price], key=lambda x: x[0])
    return sup, res


def structure_at(i, piv_h, piv_l):
    """โครงสร้างตลาดจากจุดกลับตัว 2 จุดล่าสุดที่ยืนยันแล้ว: up (HH+HL) / down (LH+LL) / range"""
    hs = [p for (conf, at, p) in piv_h if conf <= i][-2:]
    ls = [p for (conf, at, p) in piv_l if conf <= i][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return "range"
    if hs[1] > hs[0] and ls[1] > ls[0]:
        return "up"
    if hs[1] < hs[0] and ls[1] < ls[0]:
        return "down"
    return "range"


def candle_at(i, o, h, l, c):
    """รูปแบบแท่งเทียนที่แท่ง i: คืน (ชื่อ, ฝั่ง) ฝั่ง = +1 กระทิง / -1 หมี / 0"""
    rng = h[i] - l[i]
    if rng <= 0 or i < 1:
        return "", 0
    body = abs(c[i] - o[i])
    lower = min(o[i], c[i]) - l[i]
    upper = h[i] - max(o[i], c[i])
    if c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1]:
        return "Bullish Engulfing", 1
    if c[i] < o[i] and c[i - 1] > o[i - 1] and o[i] >= c[i - 1] and c[i] <= o[i - 1]:
        return "Bearish Engulfing", -1
    if lower >= 2 * body and upper <= body and body > 0.05 * rng:
        return "Hammer", 1
    if upper >= 2 * body and lower <= body and body > 0.05 * rng and c[i] < o[i]:
        return "Shooting Star", -1
    if c[i] > o[i] and (c[i] - l[i]) / rng >= 0.75 and body >= 0.5 * rng:
        return "แท่งเขียวปิดแข็ง", 1
    return "", 0


HTF = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": "1w"}


# ===================================================================== การวิเคราะห์ทั้งกราฟ
class Chart:
    """คำนวณอินดิเคเตอร์ทั้งหมดครั้งเดียว แล้วถามผล ณ แท่งไหนก็ได้ (ใช้ทั้งสแกนจริงและทดสอบย้อนหลัง)"""

    def __init__(self, k, htf_k=None, btc_k=None):
        self.t = [int(x[0]) for x in k]
        self.o = [float(x[1]) for x in k]
        self.h = [float(x[2]) for x in k]
        self.l = [float(x[3]) for x in k]
        self.c = [float(x[4]) for x in k]
        self.v = [float(x[5]) for x in k]
        c, h, l = self.c, self.h, self.l
        self.e20, self.e50, self.e200 = ema(c, 20), ema(c, 50), ema(c, 200)
        self.rsi = rsi(c)
        self.atr = atr(h, l, c)
        _, _, self.hist = macd(c)
        self.bb_mid, self.bb_up, self.bb_lo, self.bb_w = bollinger(c)
        self.adx, self.pdi, self.mdi = adx(h, l, c)
        self.obv = obv(c, self.v)
        self.obv_e = ema(self.obv, 20)
        self.vol_avg = sma(self.v, 20)
        self.piv_h, self.piv_l = pivots(h, l)
        # timeframe ใหญ่: แท่ง HTF ที่ปิดแล้วก่อนเวลาของแท่ง i
        self.htf = None
        if htf_k and len(htf_k) >= 60:
            hc = [float(x[4]) for x in htf_k]
            self.htf = {"t": [int(x[6]) if len(x) > 6 else int(x[0]) for x in htf_k], "c": hc,
                        "e20": ema(hc, 20), "e50": ema(hc, 50)}
        self.btc = None
        if btc_k:
            self.btc = {int(x[0]): float(x[4]) for x in btc_k}

    def htf_trend(self, i):
        if not self.htf:
            return None
        t = self.t[i]
        j = None
        for idx in range(len(self.htf["t"]) - 1, -1, -1):  # แท่ง HTF ล่าสุดที่ปิดก่อน t
            if self.htf["t"][idx] <= t:
                j = idx
                break
        if j is None or self.htf["e50"][j] is None:
            return None
        c, e20, e50 = self.htf["c"][j], self.htf["e20"][j], self.htf["e50"][j]
        return "up" if c > e50 and e20 > e50 else "down" if c < e50 and e20 < e50 else "flat"

    def rs_btc(self, i, n=20):
        """ผลตอบแทน n แท่งของเหรียญ ลบด้วยของ BTC (บวก = แข็งแรงกว่าตลาด)"""
        if not self.btc or i < n:
            return None
        b1, b0 = self.btc.get(self.t[i]), self.btc.get(self.t[i - n])
        if not b1 or not b0:
            return None
        return (self.c[i] / self.c[i - n] - 1) - (b1 / b0 - 1)

    def evaluate(self, i, max_sl=0.10, min_rr=2.0):
        """ประเมินจุดเข้าซื้อ ณ แท่ง i ตามเช็กลิสต์นักเทรด คืน dict: score, checklist, setup, sl_pct, tp_pct ..."""
        c, h, l = self.c, self.h, self.l
        p, a = c[i], self.atr[i]
        if a is None or self.e50[i] is None or self.rsi[i] is None or i < 30:
            return None
        e20, e50, r = self.e20[i], self.e50[i], self.rsi[i]
        sup, res = levels_at(i, self.piv_h, self.piv_l, p, a)
        st = structure_at(i, self.piv_h, self.piv_l)
        htf = self.htf_trend(i)
        rs = self.rs_btc(i)
        cname, cside = candle_at(i, self.o, h, l, c)
        hist, hist1 = self.hist[i], self.hist[i - 1]
        ad, pdi, mdi = self.adx[i], self.pdi[i], self.mdi[i]
        vavg = self.vol_avg[i - 1] if self.vol_avg[i - 1] else None
        spike = self.v[i] / vavg if vavg else 1.0
        ext = (p - e20) / a if a else 0  # ห่าง EMA20 กี่ ATR

        # ---- setup: รูปแบบการเข้าซื้อ
        prev_res = levels_at(i - 1, self.piv_h, self.piv_l, c[i - 1], self.atr[i - 1] or a)[1]
        setup = ""
        if prev_res and c[i - 1] <= prev_res[0][0] < p and spike >= 1.5:
            setup = "เบรกแนวต้าน"
        elif e20 > e50 and (abs(l[i] - e20) <= 0.5 * a or (sup and l[i] - sup[0][0] <= 0.5 * a)) and c[i] > self.o[i]:
            setup = "ย่อแล้วเด้ง"
        elif e20 > e50 and p > e20:
            setup = "ตามแนวโน้ม"

        # ---- จุดตัดขาดทุนตามโครงสร้าง: ใต้แนวรับ/ฐานล่าสุด ลบ 0.3 ATR
        swing_lows = [pp for (conf, at, pp) in self.piv_l if conf <= i and at >= i - 30 and pp < p]
        base = max([s[0] for s in sup[:1]] + swing_lows[-1:] or [p - 1.5 * a])
        if setup == "เบรกแนวต้าน" and prev_res:
            base = max(base, prev_res[0][0] - 0.5 * a)  # แนวต้านเดิมกลายเป็นแนวรับ
        stop = base - 0.3 * a
        stop = min(stop, p - 1.0 * a)                         # ห่างอย่างน้อย 1 ATR กันโดนสะบัด
        stop = max(stop, p - 3.0 * a, p * (1 - max_sl))       # ไม่ห่างเกินไป
        sl_pct = max(0.004, (p - stop) / p)
        # ---- เป้าหมาย: 2.5R หรือแนวต้านถ้าอยู่ระหว่าง 2R–2.5R
        room = (res[0][0] - p) / p if res else None
        tp_pct = 2.5 * sl_pct
        if room is not None and min_rr * sl_pct <= room < tp_pct:
            tp_pct = room * 0.997
        rr_ok = room is None or room >= min_rr * sl_pct

        # ---- เช็กลิสต์ (น้ำหนักรวม 100)
        hz = {"down": "ขาลง", "flat": "ออกข้าง", None: "ไม่มีข้อมูล"}
        chk = [  # (น้ำหนัก, ผ่านไหม, ข้อความตอนผ่าน, ข้อความตอนไม่ผ่าน)
            (15, htf == "up", "TF ใหญ่ขาขึ้น", "TF ใหญ่%s" % hz.get(htf, "")),
            (10, st == "up", "ยอด-ฐานสูงขึ้น (HH/HL)", "โครงสร้าง%s" % {"down": "ยอด-ฐานต่ำลง", "range": "ไซด์เวย์"}.get(st, "")),
            (10, p > e50 and e20 > e50, "เหนือ EMA50, EMA20>EMA50", "ยังไม่อยู่เหนือ EMA50/EMA20<EMA50"),
            (10, bool(ad and ad > 20 and pdi and mdi and pdi > mdi), "ADX %.0f แนวโน้มมีแรง" % (ad or 0), "ADX %.0f แนวโน้มยังอ่อน" % (ad or 0)),
            (10, bool(hist is not None and hist1 is not None and hist > 0 and hist >= hist1), "MACD บวกและเร่งขึ้น", "MACD ยังไม่ยืนยัน"),
            (10, 45 <= r <= 68, "RSI %.0f แข็งแรงไม่ร้อน" % r, "RSI %.0f %s" % (r, "อ่อน" if r < 45 else "ค่อนข้างร้อน")),
            (10, spike >= 1.2 or bool(self.obv_e[i] and self.obv[i] > self.obv_e[i]), "วอลุ่ม/OBV ยืนยัน", "วอลุ่มไม่ยืนยัน"),
            (10, bool(rs is not None and rs > 0), "แข็งกว่า BTC %+.1f%%" % ((rs or 0) * 100),
             "อ่อนกว่า BTC %+.1f%%" % ((rs or 0) * 100) if rs is not None else "เทียบ BTC ไม่ได้"),
            (10, rr_ok, ("ห่างแนวต้าน %.1fR" % (room / sl_pct)) if room is not None else "ไม่มีแนวต้านใกล้",
             "ติดแนวต้าน (%.1fR)" % (room / sl_pct) if room is not None else ""),
            (5, cside > 0, cname or "แท่งเทียนยืนยัน", cname or "ไม่มีแท่งเทียนยืนยัน"),
        ]
        score = sum(c_[0] for c_ in chk if c_[1])
        warn = []
        if ext > 3:
            score -= 15; warn.append("ห่าง EMA20 %.1f ATR (ไล่ราคา)" % ext)
        if r > 75:
            score -= 15; warn.append("RSI %.0f ร้อนเกิน" % r)
        if cside < 0:
            score -= 10; warn.append(cname)
        if not rr_ok:
            score = min(score, 40); warn.append("ติดแนวต้านใกล้ เป้าไม่คุ้มความเสี่ยง")
        if not setup:
            score = min(score, 45)
        return {"score": max(0, min(100, score)), "setup": setup or "ไม่มีจังหวะ", "sl_pct": sl_pct, "tp_pct": tp_pct,
                "checklist": [(ok, t_ok if ok else t_no) for w, ok, t_ok, t_no in chk], "warn": warn, "rsi": r, "atr": a / p,
                "ext": (p - e20) / e20, "spike": spike, "htf": htf, "structure": st, "rs": rs, "adx": ad,
                "support": sup[0][0] if sup else None, "resistance": res[0][0] if res else None, "candle": cname}

    def overlay(self, n=100):
        """ข้อมูลวาดกราฟ: EMA20/50, แนวรับแนวต้าน, กรอบราคาคาดการณ์ (ATR) 10 แท่งข้างหน้า"""
        i = len(self.c) - 1
        a = self.atr[i] or 0
        sup, res = levels_at(i, self.piv_h, self.piv_l, self.c[i], a)
        cone = [[k, self.c[i] + a * math.sqrt(k), self.c[i] - a * math.sqrt(k)] for k in range(1, 11)]
        s = max(0, len(self.c) - n)
        return {"e20": self.e20[s:], "e50": self.e50[s:],
                "sup": [round(x[0], 10) for x in sup[:3]], "res": [round(x[0], 10) for x in res[:3]], "cone": cone}
