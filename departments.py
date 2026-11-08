"""
departments.py — แผนกต่างๆ ของบริษัท AI เทรดคริปโต

  1. ResearchDept   ฝ่ายวิจัยตลาด     สแกนทุกเหรียญ คำนวณอินดิเคเตอร์ ให้คะแนน + ดูภาวะตลาดรวม (BTC)
  2. StrategyDept   ฝ่ายกลยุทธ์        แปลงผลวิจัยเป็นข้อเสนอเทรด (จุดเข้า / ตัดขาดทุน / ทำกำไร)
  3. AIAnalystDept  ฝ่ายนักวิเคราะห์ AI  ให้ Claude ตรวจข้อเสนอก่อนอนุมัติ (ไม่บังคับ มีเพดานค่าใช้จ่าย)
  4. RiskDept       ฝ่ายบริหารความเสี่ยง  กฎเหล็ก: ขนาดไม้ จำนวนไม้ ขาดทุนสูงสุดต่อวัน ปุ่มหยุดฉุกเฉิน
  5. ExecutionDept  ฝ่ายเทรด            ส่งคำสั่งซื้อขายจริงไป Binance + ตั้ง stop loss บนกระดาน
  6. AccountingDept ฝ่ายบัญชี           บันทึกทุกการเทรดลง SQLite คำนวณกำไรขาดทุน
  7. CEO            ผู้บริหาร            สั่งงานทุกแผนกตามรอบ และเขียนรายงานประจำวัน
"""

import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta

TH = timezone(timedelta(hours=7))
STABLE = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD", "EUR", "EURI", "AEUR",
          "TRY", "BRL", "GBP", "USD1", "XUSD", "BFUSD", "RLUSD", "USDE", "WBTC", "WBETH", "BNSOL"}

RISK_PROFILES = {
    "low":  {"name": "ต่ำ",  "tf": "4h",  "min_vol_m": 150, "max_coins": 25, "size": 0.10, "sl": 0.03, "tp": 0.06, "max_pos": 3, "min_score": 70, "every_min": 30},
    "mid":  {"name": "กลาง", "tf": "1h",  "min_vol_m": 20,  "max_coins": 50, "size": 0.15, "sl": 0.05, "tp": 0.10, "max_pos": 5, "min_score": 70, "every_min": 15},
    "high": {"name": "สูง",  "tf": "15m", "min_vol_m": 3,   "max_coins": 80, "size": 0.20, "sl": 0.08, "tp": 0.20, "max_pos": 5, "min_score": 65, "every_min": 5},
}
# Binance TH: วอลุ่มซื้อขายขั้นต่ำต่อวัน (ล้านบาท) เหรียญที่ต่ำกว่านี้ไม่สแกนเลย (แก้ได้ด้วย th_min_vol_m ใน config.json)
TH_MIN_VOL_M = {"low": 3, "mid": 1, "high": 0.5}  # Binance TH มีคู่บาทแค่ ~11 คู่ ตั้งต่ำไว้ ให้ตัวตรวจสภาพคล่องก่อนซื้อคุมแทน
TH_USDT_MIN_VOL_M = {"low": 0.1, "mid": 0.02, "high": 0.01}  # Binance TH คู่ USDT (ล้าน USDT/วัน)


def liquidity_check(ex, symbol, amount, max_spread=0.005, max_slip=0.005, ticker=None):
    """ตรวจสภาพคล่องก่อนซื้อ คืน (ผ่านไหม, ข้อความ)
    - spread: ราคาเสนอซื้อกับเสนอขายห่างกันเกิน max_spread = ไม่ซื้อ
    - slippage: ถ้าซื้อจำนวนนี้จริง ราคาเฉลี่ยจะแพงกว่าราคาเสนอขายดีสุดเกิน max_slip = ไม่ซื้อ"""
    try:
        bids, asks = ex.order_book(symbol, 50)
    except Exception as e:
        bids = asks = None
        book_err = e
    if bids and asks:
        bid, ask = bids[0][0], asks[0][0]
        spread = ask / bid - 1 if bid > 0 else 1
        if spread > max_spread:
            return False, "ส่วนต่างราคาซื้อขาย %.2f%% เกิน %.2f%%" % (spread * 100, max_spread * 100)
        left, cost, qty = amount, 0.0, 0.0
        for px, q in asks:
            take = min(left, px * q)
            cost += take
            qty += take / px
            left -= take
            if left <= 1e-9:
                break
        if left > 1e-9:
            return False, "คำสั่งขายบนกระดานไม่พอให้ซื้อ %.2f (ขาด %.2f)" % (amount, left)
        slip = (cost / qty) / ask - 1
        if slip > max_slip:
            return False, "ซื้อจำนวนนี้จะแพงขึ้น %.2f%% เกิน %.2f%%" % (slip * 100, max_slip * 100)
        return True, "spread %.2f%% slippage %.2f%%" % (spread * 100, slip * 100)
    # ดึงสมุดคำสั่งไม่ได้: ใช้ราคาเสนอซื้อ/ขายจาก ticker แทน
    bid, ask = (float((ticker or {}).get(k) or 0) for k in ("bidPrice", "askPrice"))
    if bid > 0 and ask > 0:
        spread = ask / bid - 1
        if spread > max_spread:
            return False, "ส่วนต่างราคาซื้อขาย %.2f%% เกิน %.2f%%" % (spread * 100, max_spread * 100)
        return True, "spread %.2f%% (ไม่มีข้อมูลสมุดคำสั่ง)" % (spread * 100)
    return True, "ตรวจ spread ไม่ได้ (%s) ใช้ตัวกรองวอลุ่มอย่างเดียว" % (book_err if bids is None else "ไม่มีข้อมูล")


def now_th():
    return datetime.now(TH)


def th_hm(ts):
    """เวลาไทย HH:MM จาก timestamp (VPS มักตั้งเวลาเป็น UTC ห้ามใช้ time.localtime)"""
    return datetime.fromtimestamp(ts, TH).strftime("%H:%M")


# ===================================================================== indicators
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
        e = x if e is None else x * k + e * (1 - k)
        if i >= n - 1:
            out[i] = e
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


def atr_pct(h, lo, c, n=14):
    trs = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))]
    return (sum(trs[-n:]) / n) / c[-1] if len(trs) >= n else 0.0


def exit_levels(cfg, atr):
    """ระยะตัดขาดทุน/ทำกำไร (สัดส่วน) ใช้ร่วมกันทั้งฝ่ายวิจัย (ทดสอบย้อนหลัง) และฝ่ายกลยุทธ์"""
    sl = min(max(cfg["sl"], atr * 1.5), cfg["sl"] * 2)  # ผันผวนกว่าปกติ ขยาย stop ตาม ATR แต่ไม่เกิน 2 เท่า
    tp = max(cfg["tp"], sl * 2)  # กำไรเป้าหมายอย่างน้อย 2 เท่าของความเสี่ยง
    return sl, tp


def _bucket(v, edges, names):
    for e, n in zip(edges, names):
        if v < e:
            return n
    return names[-1]


def features_of(r, regime):
    """ลักษณะของสัญญาณตอนเข้าซื้อ เก็บไว้ให้ระบบเรียนรู้ว่าแบบไหนชนะ/แพ้บ่อย"""
    return {
        "rsi": _bucket(r.get("rsi", 50), [45, 55, 65, 75], ["<45", "45-55", "55-65", "65-75", "75+"]),
        "chg24": _bucket(r.get("chg24", 0), [0, 3, 8, 15], ["ลบ", "0-3%", "3-8%", "8-15%", "15%+"]),
        "ext": _bucket(r.get("ext", 0), [0.01, 0.03, 0.06], ["ชิดEMA20", "1-3%", "3-6%", "6%+"]),
        "regime": regime,
        "vol": "วอลุ่มพุ่ง" if r.get("spike", 1) > 1.5 else "วอลุ่มปกติ",
    }


FEATURE_TH = {"rsi": "RSI", "chg24": "ขึ้นใน 24 ชม.", "ext": "ห่าง EMA20", "regime": "ตลาด", "vol": "วอลุ่ม"}


# ===================================================================== 1. Research
class ResearchDept:
    name = "ฝ่ายวิจัยตลาด"

    def __init__(self, ex, log, cfg=None):
        self.ex, self.log, self.cfg = ex, log, cfg or {}
        self.tickers = {}

    def market_regime(self):
        """ดูภาวะตลาดรวมจาก BTC รายวัน: bull / neutral / bear"""
        k = self.ex.klines("BTC" + self.ex.quote, "1d", 220)
        c = [float(x[4]) for x in k]
        m50, m200 = sma(c, 50)[-1], sma(c, 200)[-1]
        if c[-1] > m50 > m200:
            regime = "bull"
        elif c[-1] < m200 and m50 < m200:
            regime = "bear"
        else:
            regime = "neutral"
        self.log(self.name, "ภาวะตลาด (BTC รายวัน): %s ราคา %.0f MA50 %.0f MA200 %.0f" % (regime, c[-1], m50, m200))
        return regime

    @staticmethod
    def _score_at(i, risk, c, h, v, e20, e50, s200, r_arr, atr):
        """คะแนนสัญญาณ ณ แท่งที่ i (ใช้ทั้งตอนสแกนจริงและตอนทดสอบย้อนหลัง ให้กฎตรงกัน)"""
        p = c[i]
        r = r_arr[i] or 50
        mom = c[i] / c[max(0, i - 10)] - 1
        vol_avg = sum(v[i - 20:i]) / 20 or 1
        spike = v[i] / vol_avg
        hi20 = max(h[i - 20:i])
        trend_up = e20[i] is not None and e50[i] is not None and p > e20[i] > e50[i]
        ext = p / e20[i] - 1 if e20[i] else 0
        score, why = 0, []
        if risk == "low":
            if s200[i] and p > s200[i]: score += 30; why.append("เหนือ MA200")
            if trend_up: score += 30; why.append("EMA20>EMA50")
            if 40 <= r <= 65: score += 25; why.append("RSI %.0f ไม่ร้อนเกิน" % r)
            elif r > 75: score -= 20
            if mom > 0: score += 15
        elif risk == "mid":
            if trend_up: score += 35; why.append("แนวโน้มขึ้น")
            if 50 <= r <= 70: score += 25; why.append("RSI %.0f แข็งแรง" % r)
            elif r > 78: score -= 20
            if mom > 0.02: score += 25; why.append("โมเมนตัม +%.1f%%" % (mom * 100))
            elif mom > 0: score += 10
            if spike > 1.3: score += 15; why.append("วอลุ่ม x%.1f" % spike)
        else:
            if p > hi20: score += 40; why.append("ทะลุจุดสูง 20 แท่ง")
            if spike > 2: score += 30; why.append("วอลุ่ม x%.1f" % spike)
            elif spike > 1.4: score += 15
            if trend_up: score += 20
            if r < 85: score += 10
            else: score -= 25
        # กฎนักเทรดมืออาชีพ: ไม่ไล่ราคาที่วิ่งห่างเส้นค่าเฉลี่ยเกิน 3 เท่าของความผันผวน (เสี่ยงซื้อยอดดอย)
        if atr > 0 and ext > max(0.03, atr * 3):
            score -= 15
            why.append("ห่าง EMA20 %.1f%% (เสี่ยงไล่ราคา)" % (ext * 100))
        return max(0, min(100, score)), why, {"rsi": r, "mom": mom, "spike": spike, "trend_up": trend_up, "ext": ext}

    def analyze(self, k, risk):
        c = [float(x[4]) for x in k]
        h = [float(x[2]) for x in k]
        lo = [float(x[3]) for x in k]
        v = [float(x[5]) for x in k]
        n = len(c) - 1
        e20, e50 = ema(c, 20), ema(c, 50)
        s200 = sma(c, min(200, n))
        r_arr = rsi(c)
        atr = atr_pct(h, lo, c)
        score, why, f = self._score_at(n, risk, c, h, v, e20, e50, s200, r_arr, atr)
        bt = self.backtest(risk, c, h, lo, v, e20, e50, s200, r_arr, atr)
        return {"price": c[n], "rsi": f["rsi"], "trend_up": f["trend_up"], "mom": f["mom"], "atr": atr,
                "ext": f["ext"], "spike": f["spike"], "score": score, "why": why, "bt": bt}

    def backtest(self, risk, c, h, lo, v, e20, e50, s200, r_arr, atr):
        """ทดสอบย้อนหลังบนกราฟของเหรียญนี้เอง: ทุกครั้งที่สัญญาณแบบเดียวกันเกิด แล้วซื้อตามกฎ SL/TP เดียวกัน
        ผลเป็นอย่างไร (ไม่นับไม้ซ้อนกัน) ใช้ประเมินโอกาสชนะและกำไรคาดหวังของสัญญาณตอนนี้"""
        cfg = RISK_PROFILES[risk]
        sl, tp = exit_levels(cfg, atr)
        fee = float(self.cfg.get("fee", 0.001))
        horizon = int(self.cfg.get("backtest_horizon_bars", 48))
        n = len(c) - 1
        wins = losses = timeouts = 0
        rets = []
        i = 60
        while i < n - 1:
            sc = self._score_at(i, risk, c, h, v, e20, e50, s200, r_arr, atr)[0]
            if sc < cfg["min_score"]:
                i += 1
                continue
            entry = c[i]
            stop, target = entry * (1 - sl), entry * (1 + tp)
            ret, j = None, i + 1
            while j <= min(n, i + horizon):
                if lo[j] <= stop:  # ถ้าแท่งเดียวโดนทั้งคู่ นับว่าโดน SL ก่อน (มองแง่ร้ายไว้ก่อน)
                    ret = -sl; losses += 1
                    break
                if h[j] >= target:
                    ret = tp; wins += 1
                    break
                j += 1
            if ret is None:
                if j - 1 >= n and n - i < horizon:
                    break  # ยังไม่จบ (ข้อมูลไม่พอ) ไม่นับ
                ret = c[min(n, i + horizon)] / entry - 1; timeouts += 1
            rets.append(ret - 2 * fee)
            i = j + 1
        trades = len(rets)
        k = 4  # หดผลเข้าหาค่ากลาง (ขาดทุนเท่าค่าธรรมเนียม) เมื่อจำนวนตัวอย่างน้อย กันการมั่นใจเกินจริง
        ev = (sum(rets) - k * 2 * fee) / (trades + k)
        prior = sl / (sl + tp)
        p_win = (wins + k * prior) / (trades + k)
        return {"trades": trades, "wins": wins, "losses": losses, "timeouts": timeouts,
                "p_win": round(p_win, 3), "ev": round(ev, 4), "sl": sl, "tp": tp}

    def scan(self, risk):
        cfg = RISK_PROFILES[risk]
        tick = self.ex.tickers_24h()
        # Binance TH วอลุ่มเล็กกว่า binance.com มาก: ใช้การจัดอันดับวอลุ่มแทนเกณฑ์ขั้นต่ำ
        th = getattr(self.ex, "th", False) and self.ex.quote == "THB"
        if th:
            over = self.cfg.get("th_min_vol_m")
            min_vol = float(over[risk] if isinstance(over, dict) else over) if over is not None else TH_MIN_VOL_M[risk]
        elif getattr(self.ex, "th", False):  # Binance TH คู่ USDT (ล้าน USDT ต่อวัน)
            over = self.cfg.get("th_usdt_min_vol_m")
            min_vol = float(over[risk] if isinstance(over, dict) else over) if over is not None else TH_USDT_MIN_VOL_M[risk]
        else:
            min_vol = cfg["min_vol_m"]
        max_coins = {"low": 10, "mid": 25, "high": 50}[risk] if th else cfg["max_coins"]
        max_spread = float(self.cfg.get("max_spread_pct", 0.5)) / 100
        cands, low_vol, wide = [], 0, 0
        self.tickers = {}
        for t in tick:
            s = t["symbol"]
            if s not in self.ex.filters:
                continue
            base = self.ex.filters[s]["base"]
            if base in STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            vol_m = float(t["quoteVolume"]) / 1e6
            if float(t["lastPrice"]) <= 0:
                continue
            if vol_m < min_vol:
                low_vol += 1
                continue
            bid, ask = float(t.get("bidPrice") or 0), float(t.get("askPrice") or 0)
            if bid > 0 and ask > 0 and ask / bid - 1 > max_spread:
                wide += 1
                continue
            self.tickers[s] = t
            cands.append((vol_m, s, float(t["priceChangePercent"])))
        cands.sort(reverse=True)
        cands = cands[:max_coins]
        if getattr(self.ex, "th", False):  # Binance TH: แสดงวอลุ่ม 30 อันดับแรก ไว้ปรับเกณฑ์
            vols = sorted(((float(t["quoteVolume"]) / 1e6, t["symbol"]) for t in tick if t["symbol"] in self.ex.filters),
                          reverse=True)[:30]
            self.log(self.name, "วอลุ่ม 24 ชม. (ล้าน %s) 30 อันดับแรก: " % self.ex.quote + ", ".join(
                "%s %.2f" % (sym[:-len(self.ex.quote)], v) for v, sym in vols))
        if low_vol or wide:
            self.log(self.name, "ตัดทิ้ง: วอลุ่มต่ำกว่า %g ล้าน%s %d เหรียญ, spread กว้างเกิน %.2f%% %d เหรียญ" % (
                min_vol, "บาท" if th else " " + self.ex.quote, low_vol, max_spread * 100, wide))
        results = []
        for vol_m, s, chg in cands:
            try:
                k = self.ex.klines(s, cfg["tf"], 250)
                if len(k) < 60:
                    continue
                a = self.analyze(k, risk)
                a.update(symbol=s, vol_m=vol_m, chg24=chg)
                results.append(a)
            except Exception as e:
                self.log(self.name, "ข้าม %s: %s" % (s, e))
            time.sleep(0.1)
        results.sort(key=lambda x: -x["score"])
        good = [r for r in results if r["score"] >= cfg["min_score"]]
        self.log(self.name, "สแกน %d เหรียญ ผ่านเกณฑ์ %d: %s" % (
            len(results), len(good), ", ".join("%s(%d)" % (r["symbol"], r["score"]) for r in good[:8]) or "-"))
        return results


# ===================================================================== 2. Strategy
class StrategyDept:
    name = "ฝ่ายกลยุทธ์"

    def __init__(self, log, acct=None, cfg=None):
        self.log, self.acct, self.cfg = log, acct, cfg or {}
        self.learned = {}

    def learn(self):
        """เรียนรู้จากผลเทรดจริงของบอทเอง: ลักษณะสัญญาณแบบไหนชนะ/แพ้บ่อย (ต้องมีอย่างน้อย 5 ไม้ต่อแบบ)"""
        if not self.acct:
            return {}
        stats = {}
        for p in self.acct.closed_positions(2000):
            meta = p.get("meta_d") or {}
            for k, val in (meta.get("features") or {}).items():
                st = stats.setdefault((k, val), [0, 0, 0.0])
                st[0] += 1
                st[1] += 1 if p["pnl"] > 0 else 0
                st[2] += p["pnl"] / p["cost"] if p["cost"] else 0
        self.learned = {key: {"n": n, "win": w / n, "avg": ret / n} for key, (n, w, ret) in stats.items() if n >= 5}
        return self.learned

    def adjust(self, feats):
        """ปรับคะแนนจากสิ่งที่เรียนรู้ ±15 สูงสุด คืน (ค่าปรับ, คำอธิบาย)"""
        adj, notes = 0.0, []
        for k, val in feats.items():
            st = self.learned.get((k, val))
            if not st:
                continue
            a = max(-8, min(8, (st["win"] - 0.5) * 20 + st["avg"] * 100))
            if abs(a) >= 2:
                adj += a
                notes.append("%s %s ชนะ %.0f%% จาก %d ไม้" % (FEATURE_TH.get(k, k), val, st["win"] * 100, st["n"]))
        return max(-15, min(15, adj)), notes

    def proposals(self, research, risk, held, regime="neutral"):
        cfg = RISK_PROFILES[risk]
        self.learn()
        min_ev = float(self.cfg.get("min_backtest_ev_pct", 0)) / 100
        min_bt = int(self.cfg.get("min_backtest_trades", 6))
        out = []
        for r in research:
            if r["symbol"] in held:
                continue
            feats = features_of(r, regime)
            adj, notes = self.adjust(feats)
            score = r["score"] + adj
            if score < cfg["min_score"]:
                if r["score"] >= cfg["min_score"]:  # ผ่านเกณฑ์เดิม แต่สถิติของบอทเองบอกว่าแบบนี้แพ้บ่อย
                    self.reject(r["symbol"], "คะแนน %d → %.0f หลังเรียนรู้จากผลเทรดเอง (%s)" % (
                        r["score"], score, "; ".join(notes)))
                continue
            bt = r.get("bt") or {}
            if bt.get("trades", 0) >= min_bt and bt.get("ev", 0) < min_ev:
                self.reject(r["symbol"], "ทดสอบย้อนหลังสัญญาณนี้ %d ครั้ง ชนะ %d ได้เฉลี่ย %+.2f%%/ไม้ (ไม่คุ้ม)" % (
                    bt["trades"], bt["wins"], bt["ev"] * 100))
                continue
            sl, tp = exit_levels(cfg, r["atr"])
            why = list(r["why"]) + (["เรียนรู้ %+.0f" % adj] if adj else [])
            out.append({"symbol": r["symbol"], "price": r["price"], "score": round(score), "why": why,
                        "sl_pct": sl, "tp_pct": tp, "rsi": r["rsi"], "mom": r["mom"], "chg24": r["chg24"],
                        "ext": r.get("ext", 0), "bt": bt, "features": feats, "learn_notes": notes})
        if out:
            self.log(self.name, "เสนอซื้อ %d ตัว: %s" % (len(out), ", ".join(
                "%s(ย้อนหลังชนะ %.0f%% คาด %+.1f%%)" % (p["symbol"], p["bt"].get("p_win", 0) * 100, p["bt"].get("ev", 0) * 100)
                for p in out[:6])))
        return out

    def reject(self, symbol, reason):
        self.log(self.name, "ไม่เสนอ %s: %s" % (symbol, reason))
        if self.acct:
            self.acct.add_decision(self.name, symbol, False, reason)


# ===================================================================== 3. AI Analyst (Claude)
class AIAnalystDept:
    """ส่งข้อเสนอให้ Claude ตรวจ ถ้าไม่มี ANTHROPIC_API_KEY จะอนุมัติตามกฎอย่างเดียว"""
    name = "ฝ่ายนักวิเคราะห์ AI"
    PRICES = {  # USD ต่อ 1 ล้าน token (input, output)
        "claude-haiku-5-5": (0.10, 0.50),
        "claude-sonnet-5-5": (2.0, 10.0),
        "claude-opus-5-5": (4.0, 20.0),
    }

    def __init__(self, cfg, acct, log):
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.cfg = cfg
        self.acct, self.log = acct, log

    # ค่าตั้งงบ: แก้ได้จากแดชบอร์ด (เก็บในฐานข้อมูล) ถ้าไม่ตั้ง ใช้ค่าใน config.json
    def setting(self, k, default):
        v = self.acct.get("set_" + k)
        return self.cfg.get(k, default) if v is None else v

    @property
    def model(self):
        return self.setting("ai_model", "claude-haiku-5-5")

    @property
    def enabled(self):
        return bool(self.key) and bool(self.setting("ai_enabled", True))

    def budget_status(self):
        u = self.acct.ai_usage()
        lim = {"day_usd": float(self.setting("ai_daily_budget_usd", 0.5)),
               "month_usd": float(self.setting("ai_monthly_budget_usd", 5)),
               "day_tokens": int(self.setting("ai_daily_token_limit", 200000)),
               "max_tokens_per_call": int(self.setting("ai_max_tokens_per_call", 800))}
        over = []
        if u["day_usd"] >= lim["day_usd"]: over.append("งบรายวัน $%.2f" % lim["day_usd"])
        if u["month_usd"] >= lim["month_usd"]: over.append("งบรายเดือน $%.2f" % lim["month_usd"])
        if u["day_tokens"] >= lim["day_tokens"]: over.append("token รายวัน %d" % lim["day_tokens"])
        return u, lim, over

    def _call(self, prompt, max_tokens=800, purpose=""):
        u, lim, over = self.budget_status()
        if over:
            raise RuntimeError("เกิน" + ", ".join(over))
        max_tokens = min(max_tokens, lim["max_tokens_per_call"])
        body = json.dumps({"model": self.model, "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        us = data.get("usage", {})
        tin, tout = us.get("input_tokens", 0), us.get("output_tokens", 0)
        pin, pout = self.PRICES.get(self.model, (4.0, 20.0))
        cost = tin / 1e6 * pin + tout / 1e6 * pout
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        self.acct.add_ai_cost(cost)
        self.acct.add_ai_call(purpose, self.model, tin, tout, cost, text[:600])
        return text

    PLAYBOOK = """หลักของนักเทรดมืออาชีพที่ต้องใช้ตัดสิน:
1) เทรดตามแนวโน้มใหญ่ ไม่สวนตลาด: ตลาดรวม bear = เข้มงวดขึ้นมาก
2) ไม่ไล่ราคา: chg24 สูงมาก หรือ ext (ระยะห่างจาก EMA20) สูง = รอให้ย่อก่อน
3) RSI > 70 = ร้อนเกิน เสี่ยงพักตัว ยกเว้นแนวโน้มแรงและวอลุ่มยืนยัน
4) ต้องคุ้มความเสี่ยง: tp_pct อย่างน้อย 2 เท่าของ sl_pct และ bt.ev (กำไรคาดหวังจากการทดสอบย้อนหลังบนเหรียญนี้) ควรเป็นบวก
5) bt.trades น้อย (<6) = สถิติไม่น่าเชื่อถือ ให้น้ำหนักกับหลักข้อ 1-3 มากกว่า
6) ใช้สถิติผลเทรดจริงของบอท (track_record) ถ้าแบบไหนแพ้บ่อย ให้ระวัง
7) รักษาเงินต้นสำคัญกว่าทำกำไร: ถ้าไม่แน่ใจ ให้ปฏิเสธ"""

    def review(self, proposals, regime, track=None):
        if not self.enabled or not proposals:
            return proposals
        _, _, over = self.budget_status()
        if over:
            self.log(self.name, "ถึงเพดาน (%s) หยุดเรียก AI อนุมัติตามกฎอย่างเดียว" % ", ".join(over))
            return proposals
        keys = ("symbol", "price", "score", "why", "sl_pct", "tp_pct", "rsi", "mom", "chg24", "ext", "bt")
        brief = [{k: (round(p[k], 4) if isinstance(p[k], float) else p[k]) for k in keys if k in p} for p in proposals[:8]]
        prompt = (
            "คุณเป็นผู้จัดการความเสี่ยงของกองทุนคริปโต ประสบการณ์เทรด spot มา 15 ปี\n%s\n\n"
            "ภาวะตลาดรวมตอนนี้ (BTC รายวัน): %s\nสถิติผลเทรดจริงของบอท: %s\n"
            "ข้อเสนอซื้อจากระบบ (score 0-100, sl_pct/tp_pct เป็นสัดส่วน, mom = โมเมนตัม 10 แท่ง, chg24 = %% เปลี่ยน 24 ชม., "
            "ext = ระยะห่างจาก EMA20, bt = ผลทดสอบย้อนหลังสัญญาณเดียวกันบนเหรียญนี้: p_win โอกาสชนะ, ev กำไรคาดหวังต่อไม้):\n%s\n\n"
            "ตัดสินทีละตัว เหตุผลภาษาไทยสั้นๆ อ่านง่าย (ไม่เกิน 25 คำ) อ้างตัวเลขที่ใช้ตัดสิน\n"
            "ตอบเป็น JSON อย่างเดียว รูปแบบ: {\"decisions\":[{\"symbol\":\"XXXUSDT\",\"approve\":true,\"reason\":\"...\"}]}"
        ) % (self.PLAYBOOK, regime, json.dumps(track or {}, ensure_ascii=False), json.dumps(brief, ensure_ascii=False))
        try:
            text = self._call(prompt, purpose="ตรวจข้อเสนอซื้อ %d ตัว" % len(brief))
            j = json.loads(text[text.index("{"):text.rindex("}") + 1])
            dec = {d["symbol"]: d for d in j.get("decisions", [])}
        except Exception as e:
            self.log(self.name, "เรียก Claude ไม่สำเร็จ (%s) อนุมัติตามกฎอย่างเดียว" % e)
            return proposals
        approved = []
        for p in proposals:
            d = dec.get(p["symbol"])
            if d is None or d.get("approve"):
                p["ai_reason"] = (d or {}).get("reason", "")
                approved.append(p)
                self.acct.add_decision(self.name, p["symbol"], True, p["ai_reason"] or "ผ่าน")
                if p["ai_reason"]:
                    self.log(self.name, "อนุมัติ %s: %s" % (p["symbol"], p["ai_reason"]))
            else:
                self.log(self.name, "ปฏิเสธ %s: %s" % (p["symbol"], d.get("reason", "")))
                self.acct.add_decision(self.name, p["symbol"], False, d.get("reason", "") or "ไม่ระบุเหตุผล")
        self.log(self.name, "อนุมัติ %d/%d (ค่า AI วันนี้ $%.4f)" % (len(approved), len(proposals), self.acct.ai_cost_today()))
        return approved

    def daily_letter(self, summary):
        if not self.enabled or self.budget_status()[2]:
            return None
        try:
            return self._call(purpose="รายงานประจำวัน", prompt="เขียนรายงานผู้บริหารสั้นๆ ภาษาไทย 5 บรรทัด จากข้อมูลพอร์ตเทรดคริปโตนี้ "
                              "บอกสิ่งที่ได้ผล สิ่งที่ควรระวัง ห้ามแนะนำเพิ่มความเสี่ยง:\n" + json.dumps(summary, ensure_ascii=False), max_tokens=500)
        except Exception:
            return None


# ===================================================================== 4. Risk
class RiskDept:
    name = "ฝ่ายบริหารความเสี่ยง"

    def __init__(self, cfg, acct, log):
        self.cfg, self.acct, self.log = cfg, acct, log
        self.max_daily_loss = float(cfg.get("max_daily_loss_pct", 5)) / 100
        # เพดานทุนใช้กับเงินจริง/testnet เท่านั้น โหมด paper ใช้เงินจำลองทั้งก้อน
        self.max_capital = float("inf") if cfg.get("mode") == "paper" else float(cfg.get("max_capital_usdt", 100))
        self.reserve = float(cfg.get("cash_reserve_pct", 10)) / 100
        self.halted = False

    def kill_switch_file(self):
        return os.path.exists("STOP")  # สร้างไฟล์ชื่อ STOP ในโฟลเดอร์ = หยุดเปิดไม้ใหม่ทันที

    def can_trade(self, equity_now, equity_day_start):
        if self.kill_switch_file():
            self.log(self.name, "พบไฟล์ STOP: หยุดเปิดไม้ใหม่")
            return False
        if equity_day_start > 0 and equity_now / equity_day_start - 1 <= -self.max_daily_loss:
            if not self.halted:
                self.log(self.name, "🛑 ขาดทุนวันนี้เกิน %.0f%% หยุดเปิดไม้ใหม่ถึงพรุ่งนี้ (ไม้เดิมยังมี stop loss คุม)" % (
                    self.max_daily_loss * 100), line=True, key="daily_halt", every=3600 * 6, urgent=True)
            self.halted = True
            return False
        self.halted = False
        return True

    def approve(self, proposals, risk, regime, open_pos, usdt_free, equity_now, filters):
        cfg = RISK_PROFILES[risk]
        slots = cfg["max_pos"] - len(open_pos)
        if regime == "bear":
            slots = min(slots, 1)  # ตลาดขาลง: เปิดได้แค่ทีละไม้
            size_mult = 0.5
        else:
            size_mult = 1.0
        capital = min(equity_now, self.max_capital)  # ใช้เงินไม่เกินเพดานที่ตั้งไว้
        used = sum(p["cost"] for p in open_pos)
        orders, full, poor = [], [], []
        for p in proposals:
            if slots <= 0:
                full.append(p["symbol"])
                continue
            amt = capital * cfg["size"] * size_mult
            room = capital * (1 - self.reserve) - used
            amt = min(amt, room, usdt_free * 0.99)
            if amt < filters[p["symbol"]]["min_notional"] * 1.2:
                poor.append(p["symbol"])
                continue
            orders.append(dict(p, usdt=round(amt, 2)))
            used += amt
            usdt_free -= amt
            slots -= 1
        if full:  # รวมเป็นบรรทัดเดียว ไม่ให้ log/LINE รก
            self.deny(full, "ถือครบ %d ไม้แล้ว (เพดานความเสี่ยง%s) รอให้มีไม้ปิดก่อน" % (cfg["max_pos"], cfg["name"])
                      if regime != "bear" else "ตลาดขาลง เปิดได้ทีละ 1 ไม้")
        if poor:
            self.deny(poor, "เงินที่เหลือไม่ถึงขั้นต่ำต่อไม้ (เก็บเงินสำรองไว้ %.0f%% ไม่ใช้)" % (self.reserve * 100))
        if orders:
            self.log(self.name, "อนุมัติ %d คำสั่ง (ตลาด %s, ใช้ทุนสูงสุด %.0f): %s" % (
                len(orders), regime, capital, ", ".join("%s %.2f" % (o["symbol"], o["usdt"]) for o in orders)))
        return orders

    def deny(self, symbols, why):
        names = ", ".join(symbols)
        self.log(self.name, "ไม่อนุมัติ %s: %s" % (names, why))
        self.acct.add_decision(self.name, names if len(names) <= 120 else names[:117] + "...", False, why)

    def slots_left(self, risk, regime, n_open):
        s = RISK_PROFILES[risk]["max_pos"] - n_open
        return min(s, 1) if regime == "bear" else s


# ===================================================================== 5. Execution
class ExecutionDept:
    name = "ฝ่ายเทรด"

    def __init__(self, ex, acct, log, cfg=None):
        self.ex, self.acct, self.log = ex, acct, log
        self.cfg = cfg or {}
        self.lock = threading.RLock()  # เฝ้าราคา (ทุก 10 วิ) กับซื้อ/ขายในลูปหลัก ห้ามทำพร้อมกัน

    def trail(self, p, px):
        """ล็อกกำไร (trailing stop) สำหรับไม้ที่บอทเฝ้าเอง:
        - กำไรถึง 1 เท่าของระยะ stop → ย้าย stop ขึ้นมาที่ทุน (ไม่ขาดทุนแล้ว)
        - กำไรถึง 1.5 เท่า → stop ไล่ตามราคาสูงสุด ห่างลงมา 75% ของระยะ stop เดิม
        คืน stop ใหม่ (หรือ None ถ้าไม่เปลี่ยน)"""
        if not self.cfg.get("trailing_stop", True) or p["oco_id"]:
            return None
        m = p.get("meta_d") or {}
        r = m.get("sl_pct") or (1 - p["sl"] / p["entry"] if p["sl"] < p["entry"] else None)
        if not r or r <= 0:
            return None
        peak = max(m.get("peak", p["entry"]), px)
        new = p["sl"]
        if peak >= p["entry"] * (1 + r):
            new = max(new, p["entry"] * (1 + 2.5 * self.ex.fee))
        if peak >= p["entry"] * (1 + 1.5 * r):
            new = max(new, peak * (1 - 0.75 * r))
        changed_peak = peak > m.get("peak", 0) * 1.001
        if new > p["sl"] * 1.002 or changed_peak:
            m.update(peak=peak, sl_pct=r)
            self.acct.update_stop(p["id"], max(new, p["sl"]), m)
        if new > p["sl"] * 1.002:
            self.log(self.name, "🔒 ล็อกกำไร %s: ย้าย stop %.6g → %.6g (ราคาสูงสุด %.6g, ล็อกไว้ %+.1f%%)" % (
                p["symbol"], p["sl"], new, peak, (new / p["entry"] - 1) * 100))
            return new
        return None

    def buy(self, o, risk):
        with self.lock:
            return self._buy(o, risk)

    def _buy(self, o, risk):
        s = o["symbol"]
        qty, px, oid, spent = self.ex.market_buy(s, o["usdt"])
        if qty <= 0:
            raise RuntimeError("ซื้อได้ 0 เหรียญ")
        sl, tp = px * (1 - o["sl_pct"]), px * (1 + o["tp_pct"])
        oco = None
        try:
            oco = self.ex.place_oco_exit(s, qty, tp, sl)
        except Exception as e:
            self.log(self.name, "ตั้ง OCO บนกระดานไม่สำเร็จ (%s) จะเฝ้าราคาเองแทน" % e)
        bt = o.get("bt") or {}
        meta = {"features": o.get("features") or {}, "p_win": bt.get("p_win"), "ev": bt.get("ev"),
                "sl_pct": o["sl_pct"], "peak": px,
                "bt_trades": bt.get("trades"), "ai_reason": o.get("ai_reason", ""), "score": o.get("score")}
        pid = self.acct.open_position(s, qty, px, spent, sl, tp, oco, risk, "; ".join(o["why"]), meta)
        fc = ""
        if bt.get("p_win") is not None:
            fc = "\n📐 คาดการณ์: โอกาสถึงเป้า ~%.0f%% · ถ้าถึงเป้า +%.2f · ถ้าโดนตัด -%.2f · คาดหวัง %+.2f (%+.1f%%)" % (
                bt["p_win"] * 100, spent * o["tp_pct"], spent * o["sl_pct"], spent * bt.get("ev", 0), bt.get("ev", 0) * 100)
        why = ("\n🧠 AI: " + o["ai_reason"]) if o.get("ai_reason") else ""
        self.log(self.name, "🟢 ซื้อ %s %.6g @ %.6g ใช้ %.2f | SL %.6g TP %.6g %s%s%s" % (
            s, qty, px, spent, sl, tp, "(OCO บนกระดาน)" if oco else "(เฝ้าเอง)", fc, why), line=True)
        return pid

    def monitor(self):
        with self.lock:
            self._monitor()

    def _monitor(self):
        pos = self.acct.open_positions()
        if not pos:
            return
        prices = self.ex.prices([p["symbol"] for p in pos])
        for p in pos:
            s, px = p["symbol"], prices.get(p["symbol"])
            if p["oco_id"]:
                try:
                    if self.ex.oco_status(p["oco_id"]) == "done":
                        got, exit_px, typ = self.ex.oco_fill(p["oco_id"])
                        reason = "ทำกำไร" if "LIMIT_MAKER" in typ else "ตัดขาดทุน"
                        self.acct.close_position(p["id"], exit_px, got, reason)
                        self.log(self.name, self.exit_msg(reason, s, exit_px, got, p["cost"]), line=True)
                except Exception as e:
                    self.log(self.name, "เช็ก OCO %s ไม่ได้: %s" % (s, e))
                continue
            if px is None:
                continue
            new = self.trail(p, px)
            if new:
                p["sl"] = new
            if px <= p["sl"]:
                reason = "ล็อกกำไร" if p["sl"] > p["entry"] else "ตัดขาดทุน"
            else:
                reason = "ทำกำไร" if px >= p["tp"] else None
            if reason:
                self.sell(p, reason)

    def sell(self, p, reason):
        with self.lock:
            if not self.acct._one("SELECT 1 FROM positions WHERE id=? AND status='open'", (p["id"],)):
                return  # อีก thread ขายไปแล้ว
            self._sell(p, reason)

    def _sell(self, p, reason):
        if p["oco_id"]:
            try:
                self.ex.cancel_oco(p["symbol"], p["oco_id"])
            except Exception as e:
                self.log(self.name, "ยกเลิก OCO ไม่ได้ (%s)" % e)
        got, px = self.ex.market_sell(p["symbol"], p["qty"])
        self.acct.close_position(p["id"], px, got, reason)
        self.log(self.name, self.exit_msg(reason, p["symbol"], px, got, p["cost"]), line=True)

    @staticmethod
    def exit_msg(reason, symbol, px, got, cost):
        pnl = got - cost
        return "%s %s %s @ %.6g ได้ %.2f (%+.2f, %+.1f%%)" % (
            "💰" if pnl > 0 else "🔻", reason, symbol, px, got, pnl, pnl / cost * 100 if cost else 0)


# ===================================================================== 6. Accounting
class AccountingDept:
    name = "ฝ่ายบัญชี"

    def __init__(self, path):
        self.lock = threading.RLock()  # แดชบอร์ด, LINE และลูปหลักใช้ฐานข้อมูลพร้อมกันได้อย่างปลอดภัย
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, symbol TEXT, qty REAL, entry REAL, cost REAL,
            sl REAL, tp REAL, oco_id TEXT, risk TEXT, why TEXT, opened TEXT, status TEXT DEFAULT 'open',
            exit REAL, proceeds REAL, pnl REAL, reason TEXT, closed TEXT);
        CREATE TABLE IF NOT EXISTS equity(t TEXT, value REAL);
        CREATE TABLE IF NOT EXISTS ai_cost(day TEXT PRIMARY KEY, usd REAL);
        CREATE TABLE IF NOT EXISTS ai_calls(t TEXT, purpose TEXT, model TEXT, tin INTEGER, tout INTEGER, usd REAL, result TEXT);
        CREATE TABLE IF NOT EXISTS logs(t TEXT, dept TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS decisions(t TEXT, dept TEXT, symbol TEXT, ok INTEGER, reason TEXT);
        CREATE INDEX IF NOT EXISTS logs_t ON logs(t);
        CREATE INDEX IF NOT EXISTS logs_dept ON logs(dept, t);
        CREATE INDEX IF NOT EXISTS equity_t ON equity(t);
        CREATE INDEX IF NOT EXISTS ai_calls_t ON ai_calls(t);
        CREATE INDEX IF NOT EXISTS decisions_t ON decisions(t);
        CREATE INDEX IF NOT EXISTS pos_status ON positions(status);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(positions)")}
        if "meta" not in cols:
            self.db.execute("ALTER TABLE positions ADD COLUMN meta TEXT")
        self.db.commit()
        self.prune()

    # ---- ตัวช่วยใช้ฐานข้อมูลแบบล็อก
    def _q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
            return dict(r) if r else None

    def _x(self, sql, args=()):
        with self.lock:
            c = self.db.execute(sql, args)
            self.db.commit()
            return c.lastrowid

    def prune(self):
        """เก็บ log 30 วัน, มูลค่าพอร์ตละเอียด 3 วัน (เก่ากว่านั้นเก็บชั่วโมงละจุด) กันฐานข้อมูลบวมจนแดชบอร์ดช้า"""
        old = (now_th() - timedelta(days=30)).isoformat()
        d3 = (now_th() - timedelta(days=3)).isoformat()
        with self.lock:
            self.db.execute("DELETE FROM logs WHERE t < ?", (old,))
            self.db.execute("DELETE FROM decisions WHERE t < ?", (old,))
            self.db.execute("DELETE FROM equity WHERE t < ? AND rowid NOT IN (SELECT MIN(rowid) FROM equity WHERE t < ? GROUP BY substr(t,1,13))", (d3, d3))
            self.db.commit()

    @staticmethod
    def _pos(r):
        try:
            r["meta_d"] = json.loads(r.get("meta") or "{}")
        except ValueError:
            r["meta_d"] = {}
        return r

    def open_position(self, symbol, qty, entry, cost, sl, tp, oco, risk, why, meta=None):
        return self._x("INSERT INTO positions(symbol,qty,entry,cost,sl,tp,oco_id,risk,why,opened,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (symbol, qty, entry, cost, sl, tp, oco, risk, why, now_th().isoformat(), json.dumps(meta or {}, ensure_ascii=False)))

    def close_position(self, pid, exit_px, proceeds, reason):
        row = self._one("SELECT symbol, cost FROM positions WHERE id=?", (pid,))
        self._x("UPDATE positions SET status='closed', exit=?, proceeds=?, pnl=?, reason=?, closed=? WHERE id=?",
                (exit_px, proceeds, proceeds - row["cost"], reason, now_th().isoformat(), pid))
        self.log(self.name, "ลงบัญชีปิดไม้ %s: %s กำไร/ขาดทุน %+.2f" % (row["symbol"], reason, proceeds - row["cost"]))

    def update_stop(self, pid, sl, meta):
        self._x("UPDATE positions SET sl=?, meta=? WHERE id=?", (sl, json.dumps(meta, ensure_ascii=False), pid))

    def open_positions(self):
        return [self._pos(r) for r in self._q("SELECT * FROM positions WHERE status='open'")]

    def closed_positions(self, limit=100):
        return [self._pos(r) for r in self._q("SELECT * FROM positions WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,))]

    def record_equity(self, v):
        self._x("INSERT INTO equity VALUES(?,?)", (now_th().isoformat(timespec="seconds"), v))

    def equity_series(self, days=7, points=240):
        rows = self._q("SELECT t, value FROM equity WHERE t >= ? ORDER BY t", ((now_th() - timedelta(days=days)).isoformat(),))
        step = max(1, len(rows) // points)
        out = rows[::step]
        if rows and out[-1] is not rows[-1]:
            out.append(rows[-1])
        return [[r["t"][:16], round(r["value"], 4)] for r in out]

    def add_ai_cost(self, usd):
        self._x("INSERT INTO ai_cost VALUES(?,?) ON CONFLICT(day) DO UPDATE SET usd=usd+excluded.usd", (now_th().date().isoformat(), usd))

    def ai_cost_today(self):
        r = self._one("SELECT usd FROM ai_cost WHERE day=?", (now_th().date().isoformat(),))
        return r["usd"] if r else 0.0

    def add_ai_call(self, purpose, model, tin, tout, usd, result):
        self._x("INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?)", (now_th().isoformat(timespec="seconds"), purpose, model, tin, tout, usd, result))

    def ai_usage(self):
        d = now_th().date().isoformat()
        r1 = self._one("SELECT COALESCE(SUM(usd),0) u, COALESCE(SUM(tin+tout),0) t, COUNT(*) n FROM ai_calls WHERE t >= ?", (d,))
        r2 = self._one("SELECT COALESCE(SUM(usd),0) u, COALESCE(SUM(tin+tout),0) t FROM ai_calls WHERE t >= ?", (d[:7] + "-01",))
        return {"day_usd": max(r1["u"], self.ai_cost_today()), "day_tokens": r1["t"], "day_calls": r1["n"],
                "month_usd": r2["u"], "month_tokens": r2["t"]}

    def recent_ai_calls(self, n=30):
        return self._q("SELECT * FROM ai_calls ORDER BY rowid DESC LIMIT ?", (n,))

    def add_decision(self, dept, symbol, ok, reason):
        self._x("INSERT INTO decisions VALUES(?,?,?,?,?)", (now_th().isoformat(timespec="seconds"), dept, symbol, 1 if ok else 0, reason))

    def recent_decisions(self, n=30, only_rejected=False):
        return self._q("SELECT * FROM decisions %s ORDER BY rowid DESC LIMIT ?" % ("WHERE ok=0" if only_rejected else ""), (n,))

    def dept_activity(self):
        """การทำงานล่าสุดของแต่ละแผนก (ใช้ index ไม่ต้องอ่านทั้งตาราง)"""
        out = {}
        for r in self._q("SELECT dept, MAX(t) t, COUNT(*) n FROM logs WHERE t >= ? GROUP BY dept", (now_th().date().isoformat(),)):
            last = self._one("SELECT msg FROM logs WHERE dept=? AND t=? ORDER BY rowid DESC LIMIT 1", (r["dept"], r["t"]))
            out[r["dept"]] = {"t": r["t"], "today": r["n"], "last": last["msg"] if last else ""}
        return out

    def dept_logs(self, dept, n=12):
        return self._q("SELECT * FROM logs WHERE dept=? ORDER BY t DESC, rowid DESC LIMIT ?", (dept, n))

    def log(self, dept, msg):
        self._x("INSERT INTO logs VALUES(?,?,?)", (now_th().isoformat(timespec="seconds"), dept, msg))

    def recent_logs(self, n=80):
        return self._q("SELECT * FROM logs ORDER BY rowid DESC LIMIT ?", (n,))

    def get(self, k, default=None):
        r = self._one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self._x("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def summary(self):
        r = self._one("""SELECT COUNT(*) n, COALESCE(SUM(pnl>0),0) w, COALESCE(SUM(pnl),0) tot,
            COALESCE(AVG(CASE WHEN pnl>0 THEN pnl END),0) aw, COALESCE(AVG(CASE WHEN pnl<=0 THEN pnl END),0) al
            FROM positions WHERE status='closed'""")
        n_open = self._one("SELECT COUNT(*) n FROM positions WHERE status='open'")["n"]
        return {"trades": r["n"], "wins": r["w"], "win_rate": round(r["w"] / r["n"] * 100, 1) if r["n"] else 0,
                "realized_pnl": round(r["tot"], 2), "avg_win": round(r["aw"], 2), "avg_loss": round(r["al"], 2), "open": n_open}

    def track_record(self):
        """สถิติย่อให้ AI ใช้ประกอบการตัดสิน"""
        s = self.summary()
        last = self.closed_positions(10)
        return {"trades": s["trades"], "win_rate_pct": s["win_rate"], "avg_win": s["avg_win"], "avg_loss": s["avg_loss"],
                "last10": [{"symbol": p["symbol"], "pnl_pct": round(p["pnl"] / p["cost"] * 100, 1) if p["cost"] else 0,
                            "reason": p["reason"]} for p in last]}
