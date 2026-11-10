"""
departments.py — แผนกต่างๆ ของบริษัท AI เทรดคริปโต

  1. ResearchDept   ฝ่ายวิจัยตลาด     สแกนทุกเหรียญ คำนวณอินดิเคเตอร์ ให้คะแนน + ดูภาวะตลาดรวม (BTC)
  2. StrategyDept   ฝ่ายกลยุทธ์        แปลงผลวิจัยเป็นข้อเสนอเทรด (จุดเข้า / ตัดขาดทุน / ทำกำไร)
  3. AIAnalystDept  ฝ่ายนักวิเคราะห์ AI  ให้ Claude ตรวจข้อเสนอก่อนอนุมัติ (ไม่บังคับ มีเพดานค่าใช้จ่าย)
 3b. GeminiAnalystDept ฝ่ายวิเคราะห์ Gemini  ความเห็นที่สองจาก Gemini (ฟรี) ส่งเข้าที่ประชุมกับ Claude ก่อนตัดสิน
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
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

from ta import HTF, Chart  # ชุดเครื่องมือวิเคราะห์กราฟแบบนักเทรด

TH = timezone(timedelta(hours=7))
STABLE = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD", "EUR", "EURI", "AEUR",
          "TRY", "BRL", "GBP", "USD1", "XUSD", "BFUSD", "RLUSD", "USDE", "WBTC", "WBETH", "BNSOL"}

# risk = ยอมเสียได้กี่ % ของพอร์ตต่อไม้ (ถ้าโดนตัด) ขนาดไม้คำนวณจากระยะ stop จริง แต่ไม่เกิน size
RISK_PROFILES = {
    "low":  {"name": "ต่ำ",  "tf": "4h",  "min_vol_m": 150, "max_coins": 25, "size": 0.10, "sl": 0.03, "tp": 0.06, "max_pos": 3, "min_score": 75, "every_min": 30, "risk": 0.005},
    "mid":  {"name": "กลาง", "tf": "1h",  "min_vol_m": 20,  "max_coins": 50, "size": 0.15, "sl": 0.05, "tp": 0.10, "max_pos": 5, "min_score": 70, "every_min": 15, "risk": 0.01},
    "high": {"name": "สูง",  "tf": "15m", "min_vol_m": 3,   "max_coins": 80, "size": 0.20, "sl": 0.08, "tp": 0.20, "max_pos": 5, "min_score": 60, "every_min": 5, "risk": 0.015},
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
        "setup": r.get("setup", "-"),
        "htf": r.get("htf") or "-",
    }


FEATURE_TH = {"rsi": "RSI", "chg24": "ขึ้นใน 24 ชม.", "ext": "ห่าง EMA20", "regime": "ตลาด", "vol": "วอลุ่ม",
              "setup": "จังหวะ", "htf": "TF ใหญ่", "conv": "ที่ประชุม AI"}


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

    def analyze(self, k, risk, htf_k=None, btc_k=None):
        """วิเคราะห์แบบนักเทรด (ta.Chart) ณ แท่งล่าสุด + ทดสอบย้อนหลังด้วยกฎเดียวกันบนกราฟนี้"""
        cfg = RISK_PROFILES[risk]
        ch = Chart(k, htf_k, btc_k)
        n = len(ch.c) - 1
        ev = ch.evaluate(n, max_sl=cfg["sl"] * 2)
        if ev is None:
            raise ValueError("ข้อมูลกราฟไม่พอ")
        why = ["[%s]" % ev["setup"]] + [t for ok, t in ev["checklist"] if ok] + ["⚠️" + w for w in ev["warn"]]
        out = dict(ev, price=ch.c[n], why=why, trend_up=ev["structure"] == "up",
                   mom=ch.c[n] / ch.c[max(0, n - 10)] - 1, bt=self.backtest(ch, risk))
        return out

    def backtest(self, ch, risk):
        """ทดสอบย้อนหลังบนกราฟของเหรียญนี้เอง: ทุกแท่งที่เช็กลิสต์ผ่านเกณฑ์ ซื้อแล้วถือตาม stop/เป้าของแท่งนั้น
        ผลเป็นอย่างไร (ไม่นับไม้ซ้อนกัน) ใช้ประเมินโอกาสชนะและกำไรคาดหวังของสัญญาณตอนนี้"""
        cfg = RISK_PROFILES[risk]
        fee = float(self.cfg.get("fee", 0.001))
        horizon = int(self.cfg.get("backtest_horizon_bars", 48))
        c, h, lo = ch.c, ch.h, ch.l
        n = len(c) - 1
        wins = losses = timeouts = 0
        rets, sl_sum, tp_sum = [], 0.0, 0.0
        i = 60
        while i < n - 1:
            ev = ch.evaluate(i, max_sl=cfg["sl"] * 2)
            if not ev or ev["score"] < cfg["min_score"]:
                i += 1
                continue
            sl, tp = ev["sl_pct"], ev["tp_pct"]
            entry = c[i]
            stop, target = entry * (1 - sl), entry * (1 + tp)
            ret, j = None, i + 1
            while j <= min(n, i + horizon):
                if lo[j] <= stop:  # แท่งเดียวโดนทั้งคู่ นับว่าโดน SL ก่อน (มองแง่ร้ายไว้ก่อน)
                    ret = -sl; losses += 1
                    break
                if h[j] >= target:
                    ret = tp; wins += 1
                    break
                j += 1
            if ret is None:
                if n - i < horizon:
                    break  # ยังไม่จบ (ข้อมูลไม่พอ) ไม่นับ
                ret = c[i + horizon] / entry - 1; timeouts += 1
            rets.append(ret - 2 * fee)
            sl_sum += sl; tp_sum += tp
            i = j + 1
        trades = len(rets)
        k = 4  # หดผลเข้าหาค่ากลาง (ขาดทุนเท่าค่าธรรมเนียม) เมื่อตัวอย่างน้อย กันการมั่นใจเกินจริง
        ev = (sum(rets) - k * 2 * fee) / (trades + k)
        sl_avg = sl_sum / trades if trades else cfg["sl"]
        tp_avg = tp_sum / trades if trades else cfg["tp"]
        prior = sl_avg / (sl_avg + tp_avg)
        p_win = (wins + k * prior) / (trades + k)
        return {"trades": trades, "wins": wins, "losses": losses, "timeouts": timeouts,
                "p_win": round(p_win, 3), "ev": round(ev, 4)}

    def analyze_many(self, cands, risk, btc_k):
        """ดึงกราฟ + วิเคราะห์หลายเหรียญพร้อมกัน (cands = [(vol_m, symbol, chg24)])"""
        from concurrent.futures import ThreadPoolExecutor
        cfg = RISK_PROFILES[risk]

        def one(c):
            vol_m, s, chg = c
            try:
                k = self.ex.klines(s, cfg["tf"], 250)
                if len(k) < 60:
                    return None
                try:
                    htf_k = self.ex.klines(s, HTF.get(cfg["tf"], "1d"), 120)
                except Exception:
                    htf_k = None
                a = self.analyze(k, risk, htf_k, btc_k)
                a.update(symbol=s, vol_m=vol_m, chg24=chg)
                return a
            except Exception as e:
                self.log(self.name, "ข้าม %s: %s" % (s, e))
                return None
        with ThreadPoolExecutor(max_workers=int(self.cfg.get("scan_workers", 6))) as pool:
            return [r for r in pool.map(one, cands) if r]

    def rescan(self, symbols, risk):
        """ช่องทางด่วน: วิเคราะห์ใหม่เฉพาะเหรียญที่น่าจับตา (ไม่ต้องรอรอบสแกนเต็ม)"""
        cfg = RISK_PROFILES[risk]
        btc_k = None
        try:
            btc_k = self.ex.klines("BTC" + self.ex.quote, cfg["tf"], 250)
        except Exception:
            pass
        cands = []
        for s in symbols:
            t = self.tickers.get(s) or {}
            cands.append((float(t.get("quoteVolume") or 0) / 1e6, s, float(t.get("priceChangePercent") or 0)))
        return self.analyze_many(cands, risk, btc_k)

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
        btc_k = None
        try:  # เทียบความแข็งแรงกับ BTC ใน timeframe เดียวกัน
            btc_k = self.ex.klines("BTC" + self.ex.quote, cfg["tf"], 250)
        except Exception:
            pass
        results = self.analyze_many(cands, risk, btc_k)
        results.sort(key=lambda x: -x["score"])
        good = [r for r in results if r["score"] >= cfg["min_score"]]
        self.log(self.name, "สแกน %d เหรียญ ผ่านเช็กลิสต์ %d: %s" % (
            len(results), len(good), ", ".join("%s(%d %s)" % (r["symbol"], r["score"], r["setup"]) for r in good[:8]) or "-"))
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
        # ผลติดตามข้อเสนอที่ไม่ได้ซื้อ (ถ้าซื้อจะเป็นยังไง) ใช้สอนเพิ่ม น้ำหนักครึ่งหนึ่งของเทรดจริง
        try:
            since = (now_th() - timedelta(days=45)).isoformat()
            for r in self.acct._q("SELECT features, status, result FROM signals WHERE status IN ('win','loss','flat') "
                                  "AND stage != 'bought' AND t >= ?", (since,)):
                for k, val in (json.loads(r["features"] or "{}")).items():
                    st = stats.setdefault((k, val), [0, 0, 0.0])
                    st[0] += 0.5
                    st[1] += 0.5 if r["status"] == "win" else 0
                    st[2] += 0.5 * (r["result"] or 0)
        except Exception:
            pass
        self.learned = {key: {"n": n, "win": w / n, "avg": ret / n} for key, (n, w, ret) in stats.items() if n >= 5}
        return self.learned

    def cooldown(self, symbol):
        """พักเหรียญที่เพิ่งทำเราขาดทุน (กันเทรดแก้แค้น) และเหรียญที่แพ้ซ้ำๆ"""
        if not self.acct:
            return None
        h = float(self.cfg.get("cooldown_after_loss_h", 24))
        since = (now_th() - timedelta(hours=h)).isoformat()
        if h > 0 and self.acct._one("SELECT 1 FROM positions WHERE symbol=? AND status='closed' AND pnl<0 AND closed>=?",
                                    (symbol, since)):
            return "เพิ่งขาดทุนเหรียญนี้ภายใน %.0f ชม. พักก่อน กันเทรดแก้มือ" % h
        r = self._one_stats(symbol)
        if r and r["n"] >= 3 and r["w"] / r["n"] < 0.34 and r["tot"] < 0:
            return "30 วันเทรดเหรียญนี้ %d ไม้ ชนะ %d ขาดทุนรวม %.2f พัก 7 วัน" % (r["n"], r["w"], r["tot"])
        return None

    def _one_stats(self, symbol):
        last = self.acct._one("SELECT MAX(closed) c FROM positions WHERE symbol=? AND status='closed' AND pnl<0", (symbol,))
        if not last or not last["c"] or datetime.fromisoformat(last["c"]) < now_th() - timedelta(days=7):
            return None  # ไม่มีขาดทุนใน 7 วันล่าสุด = พ้นช่วงพักแล้ว
        since = (now_th() - timedelta(days=30)).isoformat()
        return self.acct._one("SELECT COUNT(*) n, COALESCE(SUM(pnl>0),0) w, COALESCE(SUM(pnl),0) tot FROM positions "
                              "WHERE symbol=? AND status='closed' AND closed>=?", (symbol, since))

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

    def proposals(self, research, risk, held, regime="neutral", extra_score=0):
        cfg = RISK_PROFILES[risk]
        self.learn()
        self.rejected = []  # (ข้อมูลเหรียญ, เหตุผล) ให้ฝ่ายพัฒนากลยุทธ์ติดตามผลต่อ
        min_score = cfg["min_score"] + extra_score
        min_ev = float(self.cfg.get("min_backtest_ev_pct", 0)) / 100
        min_bt = int(self.cfg.get("min_backtest_trades", 6))
        out = []
        for r in research:
            if r["symbol"] in held:
                continue
            feats = features_of(r, regime)
            adj, notes = self.adjust(feats)
            score = r["score"] + adj
            sl0, tp0 = r.get("sl_pct") or exit_levels(cfg, r["atr"])[0], r.get("tp_pct") or exit_levels(cfg, r["atr"])[1]
            if score < min_score:
                if r["score"] >= cfg["min_score"]:  # ผ่านเกณฑ์เดิม แต่สถิติของบอทเอง/โหมดป้องกันเงินต้นบอกว่ายังไม่พอ
                    why = "คะแนน %d → %.0f (เกณฑ์ %d)%s" % (r["score"], score, min_score,
                        (" หลังเรียนรู้จากผลเทรดเอง (%s)" % "; ".join(notes)) if notes else " โหมดป้องกันเงินต้นยกเกณฑ์")
                    self.reject(r["symbol"], why)
                    self.rejected.append((dict(r, features=feats, sl_pct=sl0, tp_pct=tp0), why))
                continue
            cd = self.cooldown(r["symbol"])
            if cd:
                self.reject(r["symbol"], cd)
                continue
            bt = r.get("bt") or {}
            if bt.get("trades", 0) >= min_bt and bt.get("ev", 0) < min_ev:
                why = "ทดสอบย้อนหลังสัญญาณนี้ %d ครั้ง ชนะ %d ได้เฉลี่ย %+.2f%%/ไม้ (ไม่คุ้ม)" % (
                    bt["trades"], bt["wins"], bt["ev"] * 100)
                self.reject(r["symbol"], why)
                self.rejected.append((dict(r, features=feats, sl_pct=sl0, tp_pct=tp0), why))
                continue
            sl, tp = sl0, tp0
            why = list(r["why"]) + (["เรียนรู้ %+.0f" % adj] if adj else [])
            out.append({"symbol": r["symbol"], "price": r["price"], "score": round(score), "why": why,
                        "sl_pct": sl, "tp_pct": tp, "rsi": r["rsi"], "mom": r["mom"], "chg24": r["chg24"],
                        "ext": r.get("ext", 0), "bt": bt, "features": feats, "learn_notes": notes,
                        "setup": r.get("setup"), "htf": r.get("htf"), "structure": r.get("structure"),
                        "adx": r.get("adx"), "rs_btc": r.get("rs"), "support": r.get("support"),
                        "resistance": r.get("resistance"), "warn": r.get("warn", []),
                        "checks": "%d/%d" % (sum(1 for ok, _ in r.get("checklist", []) if ok), len(r.get("checklist", [])))})
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

    GEMINI_TH = {"buy": "ควรซื้อ", "wait": "รอก่อน", "avoid": "ไม่ควรแตะ"}
    GEMINI_DEPT = "ฝ่ายวิเคราะห์ Gemini"

    def _gemini_only(self, proposals, views, why):
        """ไม่มี Claude (ปิด/เกินงบ/เรียกไม่ได้): ใช้ความเห็น Gemini ตัดสิน ซื้อเฉพาะตัวที่ Gemini ว่า 'ควรซื้อ'"""
        approved = []
        for p in proposals:
            v = views.get(p["symbol"])
            if v is not None and v["view"] != "buy":
                self.rejected_by[p["symbol"]] = "gemini"
            if v is None or v["view"] == "buy":
                p["ai_reason"] = ("Gemini: " + v["reason"]) if v else ""
                approved.append(p)
                self.acct.add_decision(self.GEMINI_DEPT, p["symbol"], True, p["ai_reason"] or "ผ่าน")
            else:
                self.acct.add_decision(self.GEMINI_DEPT, p["symbol"], False,
                                       "Gemini %s: %s" % (self.GEMINI_TH[v["view"]], v["reason"]))
        self.log(self.name, "%s → ใช้ความเห็น Gemini ตัดสิน อนุมัติ %d/%d" % (why, len(approved), len(proposals)))
        return approved

    def review(self, proposals, regime, track=None, gemini=None):
        """ประชุมตัดสิน: Gemini วิเคราะห์ก่อน (ถ้ามี) → Claude อ่านความเห็น Gemini แล้วตัดสินขั้นสุดท้าย
        Gemini ค้านแรง (ไม่ควรแตะ + มั่นใจ >= gemini_veto_conf) = ไม่ซื้อ แม้ Claude อนุมัติ"""
        views = (gemini or {}).get("views") or {}
        self.last_meeting = None
        self.rejected_by = {}  # symbol -> "claude" / "gemini" ให้ฝ่ายพัฒนากลยุทธ์ติดตามว่าใครปฏิเสธถูก
        if not proposals:
            return proposals
        if not self.enabled:
            return self._gemini_only(proposals, views, "Claude ปิดอยู่") if views else proposals
        _, _, over = self.budget_status()
        if over:
            if views:
                return self._gemini_only(proposals, views, "Claude ถึงเพดาน (%s)" % ", ".join(over))
            self.log(self.name, "ถึงเพดาน (%s) หยุดเรียก AI อนุมัติตามกฎอย่างเดียว" % ", ".join(over))
            return proposals
        keys = ("symbol", "price", "score", "setup", "why", "sl_pct", "tp_pct", "rsi", "mom", "chg24", "ext", "htf",
                "structure", "adx", "rs_btc", "support", "resistance", "warn", "bt")
        brief = [{k: (round(p[k], 4) if isinstance(p[k], float) else p[k]) for k in keys if k in p} for p in proposals[:8]]
        meeting = ""
        if views:
            meeting = ("\n\nที่ประชุม: นักวิเคราะห์ Gemini (คนละโมเดล วิเคราะห์อิสระมาก่อน) ให้ความเห็นดังนี้\n%s\n"
                       "มุมมองตลาดของ Gemini: %s\n"
                       "ชั่งน้ำหนักความเห็นนี้ด้วย ถ้าคุณเห็นต่างจาก Gemini ให้บอกในเหตุผลว่าทำไม"
                       ) % (json.dumps(views, ensure_ascii=False), (gemini or {}).get("market", "-"))
        prompt = (
            "คุณเป็นผู้จัดการความเสี่ยงของกองทุนคริปโต ประสบการณ์เทรด spot มา 15 ปี\n%s\n\n"
            "ภาวะตลาดรวมตอนนี้ (BTC รายวัน): %s\nสถิติผลเทรดจริงของบอท: %s\n"
            "ข้อเสนอซื้อจากระบบ (score 0-100, sl_pct/tp_pct เป็นสัดส่วน, mom = โมเมนตัม 10 แท่ง, chg24 = %% เปลี่ยน 24 ชม., "
            "ext = ระยะห่างจาก EMA20, htf = แนวโน้ม timeframe ใหญ่, structure = โครงสร้างยอด-ฐาน, rs_btc = แข็งกว่า BTC, "
            "support/resistance = แนวรับ/ต้านใกล้สุด, why = เช็กลิสต์ที่ผ่าน, warn = จุดอันตราย, "
            "bt = ผลทดสอบย้อนหลังสัญญาณเดียวกันบนเหรียญนี้: p_win โอกาสชนะ, ev กำไรคาดหวังต่อไม้):\n%s%s\n\n"
            "ตัดสินทีละตัว เหตุผลภาษาไทยสั้นๆ อ่านง่าย (ไม่เกิน 25 คำ) อ้างตัวเลขที่ใช้ตัดสิน\n"
            "confidence = ความมั่นใจว่าไม้นี้จะถึงเป้าก่อนโดนตัด 0-100 (ประเมินตามจริง ไม่ต้องเกรงใจ: 50 = เหรียญออกหัวก้อย) "
            "ระบบจะใช้ค่านี้รวมกับความมั่นใจของ Gemini กำหนดขนาดไม้: มั่นใจมาก = ไม้ใหญ่ มั่นใจน้อย = ไม้เล็กหรือไม่ซื้อ\n"
            "ตอบเป็น JSON อย่างเดียว รูปแบบ: {\"decisions\":[{\"symbol\":\"XXXUSDT\",\"approve\":true,\"confidence\":70,\"reason\":\"...\"}]}"
        ) % (self.PLAYBOOK, regime, json.dumps(track or {}, ensure_ascii=False), json.dumps(brief, ensure_ascii=False), meeting)
        try:
            text = self._call(prompt, purpose="ประชุมตรวจข้อเสนอซื้อ %d ตัว%s" % (len(brief), " (มีความเห็น Gemini)" if views else ""))
            j = json.loads(text[text.index("{"):text.rindex("}") + 1])
            dec = {d["symbol"]: d for d in j.get("decisions", [])}
        except Exception as e:
            if views:
                return self._gemini_only(proposals, views, "เรียก Claude ไม่สำเร็จ (%s)" % e)
            self.log(self.name, "เรียก Claude ไม่สำเร็จ (%s) อนุมัติตามกฎอย่างเดียว" % e)
            return proposals
        veto = int(self.setting("gemini_veto_conf", 80))
        min_conv = float(self.setting("min_conviction", 55))
        approved, minutes = [], []
        for p in proposals:
            s = p["symbol"]
            d, g = dec.get(s), views.get(s)
            ok = d is None or bool(d.get("approve"))
            reason = (d or {}).get("reason", "")
            vetoed = ok and g and g["view"] == "avoid" and g["confidence"] >= veto
            if vetoed:
                ok, reason = False, "Gemini ค้าน (มั่นใจ %d%%): %s · Claude เคยให้ผ่าน: %s" % (g["confidence"], g["reason"], reason)
            # ความมั่นใจรวมของที่ประชุม: เห็นตรงกัน = ไม้เต็ม, เห็นต่าง = ไม้เล็กลง, รวมแล้วต่ำ = ไม่ซื้อ
            conv = self.conviction(d, g)
            if ok and conv < min_conv:
                ok, low_conv = False, True
                reason = "ที่ประชุมมั่นใจรวมแค่ %.0f%% (ต้อง ≥%.0f%%) · Claude: %s%s" % (
                    conv, min_conv, reason, (" · Gemini %s: %s" % (self.GEMINI_TH[g["view"]], g["reason"])) if g else "")
            else:
                low_conv = False
            if ok:
                p["conviction"] = round(conv)
                p["size_mult"] = round(0.4 + 0.6 * max(0.0, min(1.0, (conv - min_conv) / max(1.0, 90 - min_conv))), 2)
            if g:
                minutes.append("%s · Gemini %s %d%% · Claude %s %s%% · รวม %.0f%% → %s" % (
                    s, self.GEMINI_TH[g["view"]], g["confidence"], "ผ่าน" if (d is None or d.get("approve")) else "ไม่ผ่าน",
                    (d or {}).get("confidence", "-"), conv,
                    ("✅ ซื้อ ไม้ x%.2g" % p["size_mult"]) if ok else "❌ ไม่ซื้อ"))
            if ok:
                p["ai_reason"] = reason
                if g:
                    p["gemini"] = "%s %d%%: %s" % (self.GEMINI_TH[g["view"]], g["confidence"], g["reason"])
                approved.append(p)
                self.acct.add_decision(self.name, s, True, reason or "ผ่าน")
                if reason:
                    self.log(self.name, "อนุมัติ %s: %s" % (s, reason))
            else:
                self.rejected_by[s] = "gemini" if vetoed or (low_conv and g and g["view"] != "buy") else "claude"
                self.log(self.name, "ปฏิเสธ %s: %s" % (s, reason))
                self.acct.add_decision(self.GEMINI_DEPT if vetoed else self.name, s, False, reason or "ไม่ระบุเหตุผล")
        if minutes:
            self.log("CEO", "สรุปประชุม AI: " + " | ".join(minutes))
            self.last_meeting = minutes
        self.log(self.name, "อนุมัติ %d/%d (ค่า AI วันนี้ $%.4f)" % (len(approved), len(proposals), self.acct.ai_cost_today()))
        return approved

    @staticmethod
    def conviction(d, g):
        """ความมั่นใจรวม 0-100 จาก Claude (d) และ Gemini (g)"""
        try:
            c = float((d or {}).get("confidence", 65))
        except (TypeError, ValueError):
            c = 65.0
        if not (d is None or d.get("approve")):
            c = min(c, 40.0)
        if not g:
            return c
        if g["view"] == "buy":
            return (c + g["confidence"]) / 2
        if g["view"] == "wait":
            return c - 10 - g["confidence"] * 0.1
        return c - 20 - g["confidence"] * 0.15  # avoid แต่ยังไม่ถึงเกณฑ์ค้านเด็ดขาด

    def daily_letter(self, summary):
        if not self.enabled or self.budget_status()[2]:
            return None
        try:
            return self._call(purpose="รายงานประจำวัน", prompt="เขียนรายงานผู้บริหารสั้นๆ ภาษาไทย 5 บรรทัด จากข้อมูลพอร์ตเทรดคริปโตนี้ "
                              "บอกสิ่งที่ได้ผล สิ่งที่ควรระวัง ห้ามแนะนำเพิ่มความเสี่ยง:\n" + json.dumps(summary, ensure_ascii=False), max_tokens=500)
        except Exception:
            return None


# ===================================================================== 3b. Gemini Analyst (ความเห็นที่สอง)
class GeminiAnalystDept:
    """นักวิเคราะห์คนที่ 2 (Google Gemini) วิเคราะห์ข้อเสนออิสระก่อน แล้วส่งความเห็นเข้าที่ประชุมกับ Claude
    ใช้ GEMINI_API_KEY (ฟรีจาก aistudio.google.com) ถ้าไม่มี key แผนกนี้จะข้ามไปเฉยๆ ไม่กระทบระบบเดิม"""
    name = "ฝ่ายวิเคราะห์ Gemini"
    FALLBACK = ("gemini-flash-latest", "gemini-2.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash-lite")
    URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"

    def __init__(self, cfg, acct, log):
        self.key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        self.cfg, self.acct, self.log = cfg, acct, log
        self.working = None  # ชื่อรุ่นที่เรียกได้จริงล่าสุด
        self.last_err = ""

    def setting(self, k, default):
        v = self.acct.get("set_" + k)
        return self.cfg.get(k, default) if v is None else v

    @property
    def model(self):
        return self.working or self.setting("gemini_model", "gemini-flash-latest")

    @property
    def enabled(self):
        return bool(self.key) and bool(self.setting("gemini_enabled", True))

    def calls_today(self):
        return self.acct.ai_usage(gemini=True)["day_calls"]

    def _call(self, prompt, purpose="", max_tokens=2048):
        lim = int(self.setting("gemini_daily_calls", 200))  # โควตาฟรีราว 250 ครั้ง/วัน
        if self.calls_today() >= lim:
            raise RuntimeError("ครบเพดาน %d ครั้ง/วัน" % lim)
        body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                           "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens,
                                                "responseMimeType": "application/json"}}).encode()
        tried, err = [], None
        for m in dict.fromkeys((self.model, self.setting("gemini_model", "gemini-flash-latest")) + self.FALLBACK):
            tried.append(m)
            req = urllib.request.Request(self.URL % m, data=body, method="POST",
                                         headers={"x-goog-api-key": self.key, "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                err = "HTTP %s %s" % (e.code, e.read()[:150].decode("utf-8", "ignore"))
                if e.code == 404:   # รุ่นนี้ไม่มี/เลิกใช้แล้ว ลองรุ่นถัดไป
                    continue
                raise RuntimeError(err)
            self.working = m
            us = data.get("usageMetadata", {})
            tin = us.get("promptTokenCount", 0)
            tout = us.get("candidatesTokenCount", 0) + us.get("thoughtsTokenCount", 0)
            parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            self.acct.add_ai_call(purpose, m, tin, tout, 0.0, text[:600])  # โควตาฟรี ไม่มีค่าใช้จ่าย
            return text
        raise RuntimeError("ไม่มีรุ่น Gemini ที่ใช้ได้ (%s): %s" % (", ".join(tried), err))

    def analyze(self, proposals, regime, track=None, research=None):
        """คืน {"views": {symbol: {"view": buy/wait/avoid, "confidence": 0-100, "reason": ...}}, "watch": [...], "market": "..."}"""
        if not self.enabled or not proposals:
            return None
        keys = ("symbol", "price", "score", "setup", "why", "sl_pct", "tp_pct", "rsi", "mom", "chg24", "ext", "htf",
                "structure", "adx", "rs_btc", "support", "resistance", "warn", "bt")
        rnd = lambda p: {k: (round(p[k], 4) if isinstance(p[k], float) else p[k]) for k in keys if k in p}
        brief = [rnd(p) for p in proposals[:8]]
        prop_syms = {p["symbol"] for p in proposals}
        others = [rnd(r) for r in (research or []) if r.get("symbol") not in prop_syms][:12]
        prompt = (
            "คุณเป็นนักวิเคราะห์คริปโตอาวุโสของกองทุน เทรด spot เท่านั้น (ไม่มี short ไม่มีเลเวอเรจ) "
            "หน้าที่: วิเคราะห์อิสระแล้วส่งความเห็นเข้าที่ประชุมกับผู้จัดการความเสี่ยงอีกคน\n%s\n\n"
            "ภาวะตลาดรวม (BTC รายวัน): %s\nสถิติผลเทรดจริงของบอท: %s\n"
            "ข้อเสนอซื้อจากฝ่ายกลยุทธ์ (score 0-100, sl_pct/tp_pct เป็นสัดส่วน, chg24 = %% เปลี่ยน 24 ชม., ext = ระยะห่างจาก EMA20, "
            "htf = แนวโน้ม timeframe ใหญ่, rs_btc = แข็งกว่า BTC, bt = ผลทดสอบย้อนหลัง p_win/ev):\n%s\n\n"
            "เหรียญอื่นที่ฝ่ายวิจัยสแกนเจอแต่ยังไม่ผ่านเกณฑ์เสนอซื้อ:\n%s\n\n"
            "1) ให้ความเห็นข้อเสนอทีละตัว view = buy (ควรซื้อ) / wait (รอจังหวะ) / avoid (ไม่ควรแตะ) "
            "confidence 0-100 และเหตุผลภาษาไทยสั้นๆ ไม่เกิน 25 คำ อ้างตัวเลข\n"
            "2) เลือกเหรียญน่าจับตาจากรายการอื่นไม่เกิน 3 ตัว (ถ้าไม่มีให้ว่าง) บอกว่ารอสัญญาณอะไร\n"
            "3) สรุปมุมมองตลาด 1 ประโยค\n"
            "ตอบ JSON อย่างเดียว: {\"views\":[{\"symbol\":\"XXXUSDT\",\"view\":\"buy\",\"confidence\":70,\"reason\":\"...\"}],"
            "\"watch\":[{\"symbol\":\"YYYUSDT\",\"reason\":\"...\"}],\"market\":\"...\"}"
        ) % (AIAnalystDept.PLAYBOOK, regime, json.dumps(track or {}, ensure_ascii=False),
             json.dumps(brief, ensure_ascii=False), json.dumps(others, ensure_ascii=False))
        try:
            text = self._call(prompt, purpose="วิเคราะห์ข้อเสนอ %d ตัว + หาเหรียญน่าจับตา" % len(brief))
            j = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except Exception as e:
            self.last_err = str(e)[:200]
            self.log(self.name, "วิเคราะห์ไม่สำเร็จ (%s) ที่ประชุมใช้ความเห็น Claude/กฎอย่างเดียว" % self.last_err)
            return None
        self.last_err = ""
        views = {}
        for v in j.get("views", []):
            if v.get("symbol") in prop_syms:
                view = str(v.get("view", "wait")).lower()
                views[v["symbol"]] = {"view": view if view in ("buy", "wait", "avoid") else "wait",
                                      "confidence": max(0, min(100, int(v.get("confidence") or 0))),
                                      "reason": str(v.get("reason", ""))[:200]}
        TH_VIEW = {"buy": "ควรซื้อ", "wait": "รอก่อน", "avoid": "ไม่ควรแตะ"}
        for s, v in views.items():
            self.log(self.name, "%s: %s (มั่นใจ %d%%) %s" % (s, TH_VIEW[v["view"]], v["confidence"], v["reason"]))
        watch = [{"symbol": w.get("symbol"), "reason": str(w.get("reason", ""))[:200]}
                 for w in j.get("watch", []) if w.get("symbol")][:3]
        if watch:
            self.log(self.name, "น่าจับตา: " + " · ".join("%s (%s)" % (w["symbol"], w["reason"]) for w in watch))
        market = str(j.get("market", ""))[:200]
        if market:
            self.log(self.name, "มุมมองตลาด: " + market)
        return {"views": views, "watch": watch, "market": market}


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
        # ความเสี่ยงรวมพอร์ตสูงสุด (% ถ้าทุกไม้โดนตัดพร้อมกัน) และจำนวนไม้ใหม่สูงสุดต่อรอบ ตามระดับความเสี่ยง
        self.cfg_heat = dict({"low": 2, "mid": 4, "high": 6}, **(cfg.get("max_portfolio_heat_pct") or {}))
        self.cfg_max_new = dict({"low": 1, "mid": 2, "high": 3}, **(cfg.get("max_new_per_round") or {}))

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

    GUARD = [  # ระดับป้องกันเงินต้น: (ชื่อ, คูณขนาดไม้, เพิ่มคะแนนผ่าน, ลดเพดานไม้)
        ("🟢 ปกติ", 1.0, 0, 0), ("🟡 ระวัง", 0.5, 5, 1), ("🟠 ป้องกัน", 0.25, 10, 2), ("🔴 พักเทรด", 0.0, 99, 99)]

    def guard(self, equity_now, equity_day_start):
        """ป้องกันเงินต้นอัตโนมัติ: ยิ่งพอร์ตย่อจากจุดสูงสุด/แพ้ติดกัน ยิ่งลดขนาดไม้และคัดเข้มขึ้น
        ได้กำไรวันนี้ถึงเป้า → ลดขนาดไม้ครึ่งหนึ่งที่เหลือของวัน (ล็อกกำไรรายวัน)"""
        dd_lv = [float(x) for x in self.cfg.get("guard_dd_pct", [3, 6, 10])]
        st_lv = [int(x) for x in self.cfg.get("guard_loss_streak", [3, 5])]
        reset = self.acct.get("guard_reset_t") or "0"
        since = max((now_th() - timedelta(days=30)).isoformat(), reset)
        r = self.acct._one("SELECT MAX(value) m FROM equity WHERE t >= ?", (since,))
        peak = max((r or {}).get("m") or 0, equity_now)
        dd = (1 - equity_now / peak) * 100 if peak else 0
        streak = 0
        for p in self.acct._q("SELECT pnl FROM positions WHERE status='closed' AND closed >= ? ORDER BY closed DESC LIMIT 10", (since,)):
            if p["pnl"] > 0:
                break
            streak += 1
        lv, why = 0, []
        if dd >= dd_lv[0] or streak >= st_lv[0]:
            lv = 1
        if dd >= dd_lv[1] or streak >= st_lv[1]:
            lv = 2
        if dd >= dd_lv[2]:
            lv = 3
        if dd >= 0.5:
            why.append("พอร์ตย่อจากจุดสูงสุด %.1f%%" % dd)
        if streak:
            why.append("แพ้ติดกัน %d ไม้" % streak)
        if lv == 3:  # พัก 48 ชม. แล้วกลับมาเทรดในระดับป้องกัน (นับจุดสูงสุดใหม่)
            t0 = self.acct.get("guard_halt_t")
            if not t0:
                self.acct.set("guard_halt_t", now_th().isoformat())
                self.log(self.name, "🔴 พอร์ตย่อ %.1f%% จากจุดสูงสุด พักเปิดไม้ใหม่ 48 ชม. รักษาเงินต้น (ไม้เดิมยังมี stop คุม)" % dd,
                         line=True, key="guard_halt", every=3600 * 12, urgent=True)
            elif datetime.fromisoformat(t0) < now_th() - timedelta(hours=48):
                self.acct.set("guard_reset_t", now_th().isoformat())
                self.acct.set("guard_halt_t", None)
                self.log(self.name, "ครบ 48 ชม. กลับมาเปิดไม้แบบระดับป้องกัน (ไม้เล็ก คัดเข้ม)", line=True, key="guard_resume", every=3600)
                lv = 2
        elif self.acct.get("guard_halt_t"):
            self.acct.set("guard_halt_t", None)
        name, mult, add, cut = self.GUARD[lv]
        lock = float(self.cfg.get("daily_profit_lock_pct", 2))
        day = (equity_now / equity_day_start - 1) * 100 if equity_day_start else 0
        if lv < 3 and lock > 0 and day >= lock:
            mult = min(mult, 0.5)
            why.append("กำไรวันนี้ %+.1f%% ถึงเป้า %.0f%% ลดขนาดไม้ ล็อกกำไรของวัน" % (day, lock))
        g = {"level": lv, "name": name, "size": mult, "score_add": add, "pos_cut": cut, "dd": round(dd, 2),
             "streak": streak, "day_pct": round(day, 2), "why": why}
        prev = self.acct.get("guard_level")
        if prev is not None and prev != lv:
            self.log(self.name, "ระดับป้องกันเงินต้น: %s → %s (%s)" % (self.GUARD[prev][0], name, ", ".join(why) or "ฟื้นแล้ว"),
                     line=lv > prev, key="guard_%d" % lv, every=3600)
        self.acct.set("guard_level", lv)
        self.acct.set("guard_state", g)
        return g

    def approve(self, proposals, risk, regime, open_pos, usdt_free, equity_now, filters, guard=None):
        cfg = RISK_PROFILES[risk]
        guard = guard or {"size": 1.0, "pos_cut": 0, "name": ""}
        slots = max(1, cfg["max_pos"] - guard["pos_cut"]) - len(open_pos) if guard["size"] > 0 else 0
        if regime == "bear":
            slots = min(slots, 1)  # ตลาดขาลง: เปิดได้แค่ทีละไม้
            size_mult = 0.5
        else:
            size_mult = 1.0
        size_mult *= guard["size"]
        capital = min(equity_now, self.max_capital)  # ใช้เงินไม่เกินเพดานที่ตั้งไว้
        used = sum(p["cost"] for p in open_pos)
        orders, full, poor, hot, many = [], [], [], [], []
        self.last_full = set()
        heat_max = float(self.cfg_heat.get(risk, 4)) / 100
        heat = sum(max(0.0, p["qty"] * (p["entry"] - p["sl"])) for p in open_pos)  # stop ที่เลื่อนมาเหนือทุนแล้ว = ไม่มีความเสี่ยง
        max_new = int(self.cfg_max_new.get(risk, 2))
        new_cnt = 0
        for p in proposals:
            if slots <= 0:
                full.append(p["symbol"])
                continue
            # ขนาดไม้ตามความเสี่ยง: ถ้าโดนตัดจะเสียไม่เกิน risk% ของพอร์ต (stop กว้าง = ไม้เล็กลง)
            amt = capital * min(cfg["size"], cfg.get("risk", 1) / max(p.get("sl_pct") or cfg["sl"], 1e-4)) * size_mult
            amt *= float(p.get("size_mult", 1.0))  # ขนาดตามความมั่นใจของที่ประชุม AI
            # ความเสี่ยงรวมทั้งพอร์ต (portfolio heat): ถ้าทุกไม้โดนตัดพร้อมกัน ต้องเสียไม่เกิน heat% ของพอร์ต
            # เหรียญคริปโตมักร่วงพร้อมกันตาม BTC จึงคุมรวม ไม่ใช่แค่ทีละไม้
            heat_room = capital * heat_max - heat
            sl_pct = max(p.get("sl_pct") or cfg["sl"], 1e-4)
            if heat_room <= 0:
                hot.append(p["symbol"])
                continue
            amt = min(amt, heat_room / sl_pct)
            if new_cnt >= max_new:
                many.append(p["symbol"])
                continue
            room = capital * (1 - self.reserve) - used
            amt = min(amt, room, usdt_free * 0.99)
            if amt < filters[p["symbol"]]["min_notional"] * 1.2:
                poor.append(p["symbol"])
                continue
            orders.append(dict(p, usdt=round(amt, 2)))
            used += amt
            usdt_free -= amt
            slots -= 1
            heat += amt * sl_pct
            new_cnt += 1
        if full:  # รวมเป็นบรรทัดเดียว ไม่ให้ log/LINE รก
            self.deny(full, ("ถือครบ %d ไม้แล้ว (เพดานความเสี่ยง%s%s) รอให้มีไม้ปิดก่อน" % (
                max(1, cfg["max_pos"] - guard["pos_cut"]), cfg["name"], (" · " + guard["name"]) if guard["pos_cut"] else ""))
                      if regime != "bear" else "ตลาดขาลง เปิดได้ทีละ 1 ไม้")
        self.last_full = set(full)
        if poor:
            self.deny(poor, "เงินที่เหลือไม่ถึงขั้นต่ำต่อไม้ (เก็บเงินสำรองไว้ %.0f%% ไม่ใช้)" % (self.reserve * 100))
        if hot:
            self.deny(hot, "ความเสี่ยงรวมพอร์ตเต็มแล้ว: ถ้าทุกไม้โดนตัดพร้อมกันจะเสีย %.1f%% ของพอร์ต (เพดาน %.0f%%) รอไม้เดิมล็อกกำไร/ปิดก่อน" % (
                heat / capital * 100 if capital else 0, heat_max * 100))
        if many:
            self.deny(many, "เปิดไม้ใหม่ครบ %d ไม้ในรอบนี้แล้ว (ไม่ทุ่มพร้อมกัน เหรียญมักร่วงตามกัน) รอรอบถัดไป" % max_new)
        if orders:
            self.log(self.name, "อนุมัติ %d คำสั่ง (ตลาด %s, ใช้ทุนสูงสุด %.0f): %s" % (
                len(orders), regime, capital, ", ".join("%s %.2f" % (o["symbol"], o["usdt"]) for o in orders)))
        return orders

    def deny(self, symbols, why):
        names = ", ".join(symbols)
        self.log(self.name, "ไม่อนุมัติ %s: %s" % (names, why))
        self.acct.add_decision(self.name, names if len(names) <= 120 else names[:117] + "...", False, why)

    def slots_left(self, risk, regime, n_open, guard=None):
        cut = (guard or {}).get("pos_cut", 0)
        s = max(1, RISK_PROFILES[risk]["max_pos"] - cut) - n_open
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
        feats = dict(o.get("features") or {})
        if o.get("conviction") is not None:  # เก็บไว้ดูทีหลังว่าไม้ที่ที่ประชุมมั่นใจสูงชนะบ่อยกว่าจริงไหม
            feats["conv"] = "มั่นใจสูง" if o["conviction"] >= 75 else "มั่นใจกลาง" if o["conviction"] >= 62 else "มั่นใจต่ำ"
        meta = {"features": feats, "conviction": o.get("conviction"), "size_mult": o.get("size_mult"), "p_win": bt.get("p_win"), "ev": bt.get("ev"),
                "sl_pct": o["sl_pct"], "peak": px,
                "bt_trades": bt.get("trades"), "ai_reason": o.get("ai_reason", ""), "score": o.get("score"),
                "gemini": o.get("gemini", "")}
        pid = self.acct.open_position(s, qty, px, spent, sl, tp, oco, risk, "; ".join(o["why"]), meta)
        fc = ""
        if bt.get("p_win") is not None:
            fc = "\n📐 คาดการณ์: โอกาสถึงเป้า ~%.0f%% · ถ้าถึงเป้า +%.2f · ถ้าโดนตัด -%.2f · คาดหวัง %+.2f (%+.1f%%)" % (
                bt["p_win"] * 100, spent * o["tp_pct"], spent * o["sl_pct"], spent * bt.get("ev", 0), bt.get("ev", 0) * 100)
        why = ("\n🎯 จังหวะ: %s · ผ่านเช็กลิสต์ %s · คะแนน %s" % (o["setup"], o.get("checks", "-"), o.get("score"))) if o.get("setup") else ""
        why += ("\n🧠 AI: " + o["ai_reason"]) if o.get("ai_reason") else ""
        if o.get("conviction") is not None:
            why += "\n🤝 ที่ประชุมมั่นใจรวม %d%% → ขนาดไม้ %d%% ของปกติ" % (o["conviction"], round(o.get("size_mult", 1) * 100))
        why += ("\n♊ Gemini: " + o["gemini"]) if o.get("gemini") else ""
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
                reason = "ทำกำไร" if px >= p["tp"] else self.time_stop(p, px)
            if reason:
                self.sell(p, reason)

    TIME_STOP_H = {"low": 96, "mid": 48, "high": 12}

    def time_stop(self, p, px):
        """ถือนานเกินกำหนดแต่ยังไม่ถึง 1R: ถ้ามีกำไรพอหักค่าธรรมเนียม ขายเก็บไว้ก่อน ปล่อยเงินไปหาไม้ใหม่
        (ไม้ที่ขาดทุนอยู่ไม่ขาย ปล่อยให้ stop เดิมคุม)"""
        if not self.cfg.get("time_stop", True):
            return None
        hrs = self.cfg.get("time_stop_hours") or self.TIME_STOP_H
        h = float(hrs.get(p.get("risk") or "mid", 48)) if isinstance(hrs, dict) else float(hrs)
        try:
            age = (now_th() - datetime.fromisoformat(p["opened"])).total_seconds() / 3600
        except (TypeError, ValueError):
            return None
        r = (p.get("meta_d") or {}).get("sl_pct") or 0.05
        gain = px / p["entry"] - 1
        if age >= h and 4 * self.ex.fee + 0.002 <= gain < r:
            return "หมดเวลา เก็บกำไร"
        return None

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
        CREATE TABLE IF NOT EXISTS signals(t TEXT, symbol TEXT, stage TEXT, price REAL, sl_pct REAL, tp_pct REAL,
            features TEXT, reason TEXT, source TEXT, status TEXT, result REAL, closed TEXT);
        CREATE INDEX IF NOT EXISTS signals_status ON signals(status, symbol);
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

    def ai_usage(self, gemini=False):
        """gemini=False = นับเฉพาะ Claude (งบที่เสียเงิน) · gemini=True = นับเฉพาะ Gemini (โควตาฟรี)"""
        d = now_th().date().isoformat()
        w = "AND model %s LIKE 'gemini%%'" % ("" if gemini else "NOT")
        r1 = self._one("SELECT COALESCE(SUM(usd),0) u, COALESCE(SUM(tin+tout),0) t, COUNT(*) n FROM ai_calls WHERE t >= ? " + w, (d,))
        r2 = self._one("SELECT COALESCE(SUM(usd),0) u, COALESCE(SUM(tin+tout),0) t FROM ai_calls WHERE t >= ? " + w, (d[:7] + "-01",))
        if gemini:
            return {"day_usd": 0.0, "day_tokens": r1["t"], "day_calls": r1["n"], "month_usd": 0.0, "month_tokens": r2["t"]}
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
