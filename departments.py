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
TH_MIN_VOL_M = {"low": 3, "mid": 1, "high": 0.5}
TH_USDT_MIN_VOL_M = {"low": 0.1, "mid": 0.02, "high": 0.01}  # Binance TH คู่ USDT (ล้าน USDT/วัน)  # Binance TH มีคู่บาทแค่ ~11 คู่ ตั้งต่ำไว้ ให้ตัวตรวจสภาพคล่องก่อนซื้อคุมแทน


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

    def analyze(self, k, risk):
        c = [float(x[4]) for x in k]
        h = [float(x[2]) for x in k]
        lo = [float(x[3]) for x in k]
        v = [float(x[5]) for x in k]
        n = len(c) - 1
        p = c[n]
        e20, e50 = ema(c, 20)[n], ema(c, 50)[n]
        s200 = sma(c, min(200, n))[n]
        r = rsi(c)[n] or 50
        mom = c[n] / c[max(0, n - 10)] - 1
        vol_avg = sum(v[n - 20:n]) / 20 or 1
        spike = v[n] / vol_avg
        hi20 = max(h[n - 20:n])
        trend_up = p > e20 > e50
        score, why = 0, []
        if risk == "low":
            if p > s200: score += 30; why.append("เหนือ MA200")
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
        return {"price": p, "rsi": r, "trend_up": trend_up, "mom": mom, "atr": atr_pct(h, lo, c),
                "score": max(0, min(100, score)), "why": why}

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

    def __init__(self, log):
        self.log = log

    def proposals(self, research, risk, held):
        cfg = RISK_PROFILES[risk]
        out = []
        for r in research:
            if r["score"] < cfg["min_score"] or r["symbol"] in held:
                continue
            # ถ้าเหรียญผันผวนกว่าปกติ ขยาย stop ตาม ATR แต่ไม่เกิน 2 เท่าของค่าตั้งต้น
            sl = min(max(cfg["sl"], r["atr"] * 1.5), cfg["sl"] * 2)
            tp = max(cfg["tp"], sl * 2)  # กำไรเป้าหมายอย่างน้อย 2 เท่าของความเสี่ยง
            out.append({"symbol": r["symbol"], "price": r["price"], "score": r["score"], "why": r["why"],
                        "sl_pct": sl, "tp_pct": tp, "rsi": r["rsi"], "mom": r["mom"], "chg24": r["chg24"]})
        if out:
            self.log(self.name, "เสนอซื้อ %d ตัว: %s" % (len(out), ", ".join(p["symbol"] for p in out[:8])))
        return out


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

    def review(self, proposals, regime):
        if not self.enabled or not proposals:
            return proposals
        _, _, over = self.budget_status()
        if over:
            self.log(self.name, "ถึงเพดาน (%s) หยุดเรียก AI อนุมัติตามกฎอย่างเดียว" % ", ".join(over))
            return proposals
        brief = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in p.items()} for p in proposals[:8]]
        prompt = (
            "คุณเป็นนักวิเคราะห์ความเสี่ยงของกองทุนคริปโต ภาวะตลาดรวมตอนนี้: %s\n"
            "ข้อเสนอซื้อจากระบบ (score 0-100, sl_pct/tp_pct เป็นสัดส่วน, mom = โมเมนตัม 10 แท่ง, chg24 = %% เปลี่ยน 24 ชม.):\n%s\n\n"
            "ตรวจแต่ละตัว ปฏิเสธตัวที่ดูไล่ราคา (chg24 สูงเกินไป), RSI ร้อนเกิน หรือขัดกับภาวะตลาด\n"
            "ตอบเป็น JSON อย่างเดียว รูปแบบ: {\"decisions\":[{\"symbol\":\"XXXUSDT\",\"approve\":true,\"reason\":\"สั้นๆ ภาษาไทย\"}]}"
        ) % (regime, json.dumps(brief, ensure_ascii=False))
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
                approved.append(p)
            else:
                self.log(self.name, "ปฏิเสธ %s: %s" % (p["symbol"], d.get("reason", "")))
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
        orders = []
        for p in proposals:
            if slots <= 0:
                break
            amt = capital * cfg["size"] * size_mult
            room = capital * (1 - self.reserve) - used
            amt = min(amt, room, usdt_free * 0.99)
            if amt < filters[p["symbol"]]["min_notional"] * 1.2:
                self.log(self.name, "ไม่อนุมัติ %s: เงินไม่พอขั้นต่ำ (%.2f)" % (p["symbol"], amt))
                break
            orders.append(dict(p, usdt=round(amt, 2)))
            used += amt
            usdt_free -= amt
            slots -= 1
        if orders:
            self.log(self.name, "อนุมัติ %d คำสั่ง (ตลาด %s, ใช้ทุนสูงสุด %.0f)" % (len(orders), regime, capital))
        return orders


# ===================================================================== 5. Execution
class ExecutionDept:
    name = "ฝ่ายเทรด"

    def __init__(self, ex, acct, log):
        self.ex, self.acct, self.log = ex, acct, log

    def buy(self, o, risk):
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
        pid = self.acct.open_position(s, qty, px, spent, sl, tp, oco, risk, "; ".join(o["why"]))
        self.log(self.name, "🟢 ซื้อ %s %.6g @ %.6g ใช้ %.2f | SL %.6g TP %.6g %s" % (
            s, qty, px, spent, sl, tp, "(OCO บนกระดาน)" if oco else "(เฝ้าเอง)"), line=True)
        return pid

    def monitor(self):
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
            reason = "ตัดขาดทุน" if px <= p["sl"] else "ทำกำไร" if px >= p["tp"] else None
            if reason:
                self.sell(p, reason)

    def sell(self, p, reason):
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
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, symbol TEXT, qty REAL, entry REAL, cost REAL,
            sl REAL, tp REAL, oco_id TEXT, risk TEXT, why TEXT, opened TEXT, status TEXT DEFAULT 'open',
            exit REAL, proceeds REAL, pnl REAL, reason TEXT, closed TEXT);
        CREATE TABLE IF NOT EXISTS equity(t TEXT, value REAL);
        CREATE TABLE IF NOT EXISTS ai_cost(day TEXT PRIMARY KEY, usd REAL);
        CREATE TABLE IF NOT EXISTS ai_calls(t TEXT, purpose TEXT, model TEXT, tin INTEGER, tout INTEGER, usd REAL, result TEXT);
        CREATE TABLE IF NOT EXISTS logs(t TEXT, dept TEXT, msg TEXT);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        """)
        self.db.commit()

    def open_position(self, symbol, qty, entry, cost, sl, tp, oco, risk, why):
        c = self.db.execute("INSERT INTO positions(symbol,qty,entry,cost,sl,tp,oco_id,risk,why,opened) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (symbol, qty, entry, cost, sl, tp, oco, risk, why, now_th().isoformat()))
        self.db.commit()
        return c.lastrowid

    def close_position(self, pid, exit_px, proceeds, reason):
        row = self.db.execute("SELECT cost FROM positions WHERE id=?", (pid,)).fetchone()
        self.db.execute("UPDATE positions SET status='closed', exit=?, proceeds=?, pnl=?, reason=?, closed=? WHERE id=?",
                        (exit_px, proceeds, proceeds - row["cost"], reason, now_th().isoformat(), pid))
        self.db.commit()

    def open_positions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE status='open'")]

    def closed_positions(self, limit=100):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,))]

    def record_equity(self, v):
        self.db.execute("INSERT INTO equity VALUES(?,?)", (now_th().isoformat(), v))
        self.db.commit()

    def add_ai_cost(self, usd):
        d = now_th().date().isoformat()
        self.db.execute("INSERT INTO ai_cost VALUES(?,?) ON CONFLICT(day) DO UPDATE SET usd=usd+excluded.usd", (d, usd))
        self.db.commit()

    def ai_cost_today(self):
        r = self.db.execute("SELECT usd FROM ai_cost WHERE day=?", (now_th().date().isoformat(),)).fetchone()
        return r["usd"] if r else 0.0

    def add_ai_call(self, purpose, model, tin, tout, usd, result):
        self.db.execute("INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?)",
                        (now_th().isoformat(timespec="seconds"), purpose, model, tin, tout, usd, result))
        self.db.commit()

    def ai_usage(self):
        d = now_th().date().isoformat()
        m = d[:7]
        r1 = self.db.execute("SELECT COALESCE(SUM(usd),0) u, COALESCE(SUM(tin+tout),0) t, COUNT(*) n FROM ai_calls WHERE t LIKE ?", (d + "%",)).fetchone()
        r2 = self.db.execute("SELECT COALESCE(SUM(usd),0) u, COALESCE(SUM(tin+tout),0) t FROM ai_calls WHERE t LIKE ?", (m + "%",)).fetchone()
        return {"day_usd": max(r1["u"], self.ai_cost_today()), "day_tokens": r1["t"], "day_calls": r1["n"],
                "month_usd": r2["u"], "month_tokens": r2["t"]}

    def recent_ai_calls(self, n=30):
        return [dict(r) for r in self.db.execute("SELECT * FROM ai_calls ORDER BY rowid DESC LIMIT ?", (n,))]

    def dept_activity(self):
        """การทำงานล่าสุดของแต่ละแผนก"""
        rows = self.db.execute("SELECT dept, MAX(t) t, COUNT(*) n FROM logs WHERE t >= ? GROUP BY dept",
                               (now_th().date().isoformat(),)).fetchall()
        out = {}
        for r in rows:
            last = self.db.execute("SELECT msg FROM logs WHERE dept=? ORDER BY rowid DESC LIMIT 1", (r["dept"],)).fetchone()
            out[r["dept"]] = {"t": r["t"], "today": r["n"], "last": last["msg"] if last else ""}
        return out

    def log(self, dept, msg):
        self.db.execute("INSERT INTO logs VALUES(?,?,?)", (now_th().isoformat(timespec="seconds"), dept, msg))
        self.db.commit()

    def recent_logs(self, n=80):
        return [dict(r) for r in self.db.execute("SELECT * FROM logs ORDER BY rowid DESC LIMIT ?", (n,))]

    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
        self.db.commit()

    def summary(self):
        closed = self.closed_positions(10000)
        wins = [p for p in closed if p["pnl"] > 0]
        total = sum(p["pnl"] for p in closed)
        return {"trades": len(closed), "wins": len(wins),
                "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
                "realized_pnl": round(total, 2),
                "avg_win": round(sum(p["pnl"] for p in wins) / len(wins), 2) if wins else 0,
                "avg_loss": round(sum(p["pnl"] for p in closed if p["pnl"] <= 0) / max(1, len(closed) - len(wins)), 2),
                "open": len(self.open_positions())}
