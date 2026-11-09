#!/usr/bin/env python3
"""
main.py — CEO ของบริษัท AI เทรดหุ้นสหรัฐฯ ผ่าน Webull: สั่งงานทุกแผนกตามเวลาตลาด + เปิดหน้าแดชบอร์ด

รัน:
    python main.py                 # ใช้ค่าใน config.json (เริ่มต้น = paper เงินจำลอง ใช้ราคาจริงจาก Webull)
    python main.py --check         # ตรวจการเชื่อมต่อ Webull และยอดเงิน (ไม่ซื้อขาย)
    python main.py --check-order   # ให้ Webull ตรวจคำสั่งซื้อทดลอง (preview ไม่ซื้อจริง)
    python main.py --mode live     # เงินจริง (ต้องยืนยันก่อน)
แดชบอร์ด: เปิด http://localhost:8081 ในเบราว์เซอร์ (8080 เป็นของบอทคริปโต)
หยุดฉุกเฉิน: สร้างไฟล์ชื่อ STOP ในโฟลเดอร์นี้ (หยุดเปิดไม้ใหม่) หรือกด Ctrl+C
"""

import argparse
import json
import os
import signal
import sys
import urllib.request
import threading
import time
import traceback

from webull_client import Webull, WebullError as BinanceError, market_clock, ny_now
from departments import (RISK_PROFILES, AccountingDept, AIAnalystDept, ExecutionDept, ResearchDept,
                         RiskDept, StrategyDept, liquidity_check, now_th, th_hm)
import dashboard
from notifier import LineNotifier
import line_bot

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Company:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.risk = cfg.get("risk", "mid")
        if self.risk not in RISK_PROFILES:
            sys.exit("risk ต้องเป็น low / mid / high")
        self.exchange = "webull"
        self.acct = AccountingDept(os.path.join(HERE, "company_webull_%s.db" % self.mode))
        self.ex = make_client(cfg)
        if self.mode == "paper":
            saved = self.acct.get("paper_wallet")
            if saved and self.ex.quote in saved:
                self.ex.paper = saved
        self.research = ResearchDept(self.ex, self.log, cfg)
        self.strategy = StrategyDept(self.log, self.acct, cfg, self.research)
        self.ai = AIAnalystDept(cfg, self.acct, self.log)
        self.riskd = RiskDept(cfg, self.acct, self.log)
        self.exe = ExecutionDept(self.ex, self.acct, self.log, cfg)
        self.regime = "neutral"
        self.last_scan = 0
        self.last_research = []
        self.status = "starting"
        self.line = LineNotifier(cfg, prefix="📈 บอทหุ้น Webull")
        self.last_ok = time.time()
        self.sell_queue = []  # คำสั่งขายจาก LINE รอทำในลูปหลัก
        self.started_at = time.time()
        self.last_eq = None       # มูลค่าพอร์ตล่าสุด (แดชบอร์ด/LINE อ่านค่านี้ ไม่ต้องยิงถาม Webull ทุกครั้ง)
        self.last_eq_t = 0
        self.px_cache = {}        # symbol -> (เวลา, ราคา)
        self.threads = {}         # ชื่อ -> (thread, ฟังก์ชันเปิดใหม่)
        self.last_hc = 0
        self.last_acct_log = 0
        self.downtime_note = None
        self.clock = {"open": False}

    def log(self, dept, msg, line=False, key=None, every=0, urgent=False):
        """line=True = ส่งข้อความนี้เข้า LINE ด้วย"""
        text = "[%s] %-20s %s" % (now_th().strftime("%H:%M:%S"), dept, msg)
        print(text, flush=True)
        self.acct.log(dept, msg)
        if line:
            self.line.send("%s: %s" % (dept, msg), key=key, every_sec=every, urgent=urgent)

    def mode_label(self):
        return {"paper": "เงินจำลอง", "live": "เงินจริง"}.get(self.mode, self.mode)

    def prices(self, symbols, max_age=20):
        """ราคาล่าสุดแบบแคช: ถามกระดานเฉพาะหุ้นที่ข้อมูลเก่ากว่า max_age วินาที"""
        now = time.time()
        need = [s for s in symbols if now - self.px_cache.get(s, (0, 0))[0] > max_age]
        if need:
            for s, px in self.ex.prices(need).items():
                self.px_cache[s] = (now, px)
        return {s: self.px_cache[s][1] for s in symbols if s in self.px_cache}

    def equity(self, max_age=20):
        pos = self.acct.open_positions()
        px = self.prices([p["symbol"] for p in pos], max_age) if pos else {}
        eq = self.ex.usdt_free() + self.ex.unsettled() + sum(p["qty"] * px.get(p["symbol"], p["entry"]) for p in pos)
        self.last_eq, self.last_eq_t = eq, time.time()
        return eq

    def equity_cached(self):
        """สำหรับหน้าเว็บ/LINE: ใช้ค่าที่ลูปหลักคำนวณไว้ ไม่ต้องรอ Webull"""
        if self.last_eq is None or time.time() - self.last_eq_t > 120:
            try:
                return self.equity(60)
            except Exception:
                return self.last_eq or 0.0
        return self.last_eq

    def forecast(self):
        """คาดการณ์ไม้ที่ถืออยู่จากสถิติตอนเข้าซื้อ (ทดสอบย้อนหลัง) — เป็นการประเมินความน่าจะเป็น ไม่ใช่การรับประกัน"""
        pos = self.acct.open_positions()
        px = {}
        try:
            px = self.prices([p["symbol"] for p in pos], 60) if pos else {}
        except Exception:
            pass
        rows, exp_total, up_total, down_total = [], 0.0, 0.0, 0.0
        tr = self.acct.summary()
        base_p = (tr["wins"] / tr["trades"]) if tr["trades"] >= 10 else None
        for p in pos:
            m = p.get("meta_d") or {}
            now = px.get(p["symbol"], p["entry"])
            cur = p["qty"] * now - p["cost"]
            win_amt = p["qty"] * p["tp"] * (1 - self.ex.fee) - p["cost"]
            loss_amt = p["qty"] * p["sl"] * (1 - self.ex.fee) - p["cost"]
            pw = m.get("p_win")
            if pw is None:
                pw = base_p if base_p is not None else (p["entry"] - p["sl"]) / max(1e-12, p["tp"] - p["sl"])
            # ราคาเดินไปแล้ว: ปรับโอกาสตามตำแหน่งราคาปัจจุบันระหว่าง SL กับ TP
            pos_frac = min(1, max(0, (now - p["sl"]) / max(1e-12, p["tp"] - p["sl"])))
            pw_now = min(0.95, max(0.05, 0.5 * pw + 0.5 * pos_frac))
            exp = pw_now * win_amt + (1 - pw_now) * loss_amt
            exp_total += exp
            up_total += win_amt
            down_total += loss_amt
            rows.append({"symbol": p["symbol"], "now": now, "entry": p["entry"], "sl": p["sl"], "tp": p["tp"],
                         "pnl_now": round(cur, 4), "if_tp": round(win_amt, 4), "if_sl": round(loss_amt, 4),
                         "p_win": round(pw_now, 3), "expected": round(exp, 4), "bt_trades": m.get("bt_trades")})
        return {"positions": rows, "expected": round(exp_total, 4), "best": round(up_total, 4), "worst": round(down_total, 4),
                "win_rate": tr["win_rate"], "trades": tr["trades"]}

    def day_start_equity(self, eq):
        key = "day_start_" + now_th().date().isoformat()  # วันตามเวลาไทย (ตลาดสหรัฐฯ เปิด 20:30/21:30 - ปิด 03:00/04:00)
        v = self.acct.get(key)
        if v is None:
            self.acct.set(key, eq)
            v = eq
        return v

    def save_paper(self):
        if self.mode == "paper":
            self.acct.set("paper_wallet", self.ex.paper)

    def start(self):
        self.log("CEO", "เปิดบริษัทเทรดหุ้น Webull Thailand โหมด %s ความเสี่ยง%s (%s)" % (
            self.mode.upper(), RISK_PROFILES[self.risk]["name"], RISK_PROFILES[self.risk]["style"]))
        self.ex.load_filters()
        self.log("CEO", "รายชื่อหุ้น/ETF ที่สแกน %d ตัว (แก้ได้ใน universe.py)" % len(self.ex.filters))
        if self.mode != "paper":
            self.log("CEO", "บัญชี %s เงิน USD ที่ซื้อได้: %.2f" % (self.ex.ensure_account(), self.ex.usdt_free()))
            self.reconcile()
        self.status = "running"
        # กันสแปมตอนบอทรีสตาร์ตซ้ำๆ: แจ้งว่าเริ่มงานไม่เกินครึ่งชั่วโมงละครั้ง
        self.line.send("✅ บอทเริ่มทำงานแล้ว\nโหมด: %s | ความเสี่ยง: %s\nเงินในบัญชี: %.2f %s\nถือไม้อยู่: %d" % (
            self.mode_label(), RISK_PROFILES[self.risk]["name"], self.ex.usdt_free(), self.ex.quote,
            len(self.acct.open_positions())), key="start", every_sec=1800)

    def reconcile(self):
        """เทียบไม้ในบัญชีบอทกับหุ้นในพอร์ต Webull จริง (เผื่อขายเองในแอป) — แจ้งเตือน ไม่ขายเอง"""
        try:
            have = self.ex.broker_positions()
        except Exception as e:
            self.log("ฝ่ายบัญชี", "อ่านพอร์ต Webull ไม่ได้: %s" % e)
            return
        for p in self.acct.open_positions():
            if have.get(p["symbol"], 0) < p["qty"] * 0.98:
                self.log("ฝ่ายบัญชี", "⚠️ บอทบันทึกว่าถือ %s %.5g หุ้น แต่ในพอร์ตมี %.5g (ขายเองในแอป?) ปิดไม้นี้ในบัญชีบอท" % (
                    p["symbol"], p["qty"], have.get(p["symbol"], 0)), line=True)
                px = self.prices([p["symbol"]], 60).get(p["symbol"], p["entry"])
                self.acct.close_position(p["id"], px, p["qty"] * px, "ขายนอกบอท")

    def eod_exit(self):
        """ระดับเสี่ยงสูง (เทรดระยะสั้น): ขายหมดก่อนตลาดปิด ไม่ถือข้ามคืน กันราคาเปิดกระโดด (gap)"""
        mins = float(self.cfg.get("eod_exit_minutes", 10))
        if not (self.clock.get("open") and self.clock.get("mins_to_close", 999) <= mins):
            return
        for p in self.acct.open_positions():
            if p.get("risk") == "high" or self.cfg.get("eod_exit_all"):
                try:
                    self.exe.sell(p, "ขายก่อนตลาดปิด (ไม่ถือข้ามคืน)")
                except Exception as e:
                    self.log("ฝ่ายเทรด", "ขาย %s ก่อนตลาดปิดไม่สำเร็จ: %s" % (p["symbol"], e), line=True)

    def cycle(self):
        """1 รอบการทำงาน: เวลาตลาด → เฝ้าสถานะ → (ถึงเวลา) สแกน → กลยุทธ์ → AI → ความเสี่ยง → ส่งคำสั่ง"""
        self.clock = market_clock(self.cfg.get("extra_holidays", ()))
        if not self.clock["open"]:  # ตลาดปิด: ราคาไม่ขยับ ขายไม่ได้ แค่บันทึกสถานะ
            self.sell_queue.clear()
            self.heartbeat()
            if time.time() - self.last_eq_t > 1800:
                self.acct.record_equity(self.equity(600))
            if self.status != "market_closed":
                nxt = self.clock.get("next_open")
                self.log("CEO", "ตลาดหุ้นสหรัฐฯ ปิด รอเปิด%s (ไม้ที่ถืออยู่ %d ไม้)" % (
                    (" " + nxt.astimezone(now_th().tzinfo).strftime("%a %d/%m %H:%M น.")) if nxt else "",
                    len(self.acct.open_positions())))
                self.status = "market_closed"
            self.save_paper()
            self.daily_report()
            return
        if self.status == "market_closed":
            self.status = "running"
            self.log("CEO", "ตลาดเปิดแล้ว เริ่มงาน", line=bool(self.cfg.get("line_market_open", False)))
        self.cfg["_day_frac"] = min(1.0, max(0.15, self.clock["mins_since_open"] / 390))
        self.eod_exit()
        while self.sell_queue:  # ทำคำสั่งขายที่สั่งมาจาก LINE ก่อน
            sym = self.sell_queue.pop(0)
            for p in self.acct.open_positions():
                if p["symbol"] == sym:
                    try:
                        self.exe.sell(p, "ขายตามคำสั่ง LINE")
                    except Exception as e:
                        self.log("ฝ่ายเทรด", "ขาย %s ตามคำสั่ง LINE ไม่สำเร็จ: %s" % (sym, e), line=True)
        self.exe.monitor()  # เฝ้าซ้ำในลูปหลักด้วย (ปกติ thread เฝ้าราคาทำทุก 10 วินาทีอยู่แล้ว)
        eq = self.equity()
        self.acct.record_equity(eq)
        self.heartbeat()
        if time.time() - self.last_acct_log > 3600:
            self.last_acct_log = time.time()
            day0 = self.day_start_equity(eq)
            self.log("ฝ่ายบัญชี", "มูลค่าพอร์ต %.2f %s (วันนี้ %+.2f%%) ถือ %d ไม้ · เงินสด %.2f" % (
                eq, self.ex.quote, (eq / day0 - 1) * 100 if day0 else 0, len(self.acct.open_positions()), self.ex.usdt_free()))
        cfg = RISK_PROFILES[self.risk]
        if time.time() - self.last_scan < cfg["every_min"] * 60:
            self.save_paper()
            return
        self.last_scan = time.time()
        first = float(self.cfg.get("skip_first_minutes", 15))
        last = float(self.cfg.get("no_entry_last_minutes", 20 if not cfg["eod_exit"] else 30))
        if self.clock["mins_since_open"] < first:
            self.last_scan = time.time() - cfg["every_min"] * 60 + 120  # ลองใหม่ใน 2 นาที
            self.log("CEO", "ตลาดเพิ่งเปิด %.0f นาที: รอให้ราคานิ่งก่อน (%g นาทีแรกผันผวนสูง ไม่เปิดไม้ใหม่)" % (
                self.clock["mins_since_open"], first))
            return
        if self.clock["mins_to_close"] < last:
            self.log("CEO", "ใกล้ตลาดปิด (เหลือ %.0f นาที) ไม่เปิดไม้ใหม่" % self.clock["mins_to_close"])
            return
        day0 = self.day_start_equity(eq)
        if not self.riskd.can_trade(eq, day0):
            self.log("CEO", "สั่งงานรอบนี้: งดเปิดไม้ใหม่ (หยุดไว้/ขาดทุนถึงเพดาน) เฝ้าไม้ที่ถือ %d ไม้ต่อ" % len(self.acct.open_positions()))
            return
        self.regime = self.research.gold_regime() if self.risk == "gold" else self.research.market_regime()
        if self.regime == "bear" and self.cfg.get("skip_bear_market", True) and self.risk != "high":
            self.log("CEO", "สั่งงานรอบนี้: %s งดเปิดไม้ใหม่ (ตั้ง skip_bear_market=false เพื่อปิด)" % (
                "ทองขาลง (GLD ต่ำกว่า MA200)" if self.risk == "gold" else "ตลาดขาลง (SPY ต่ำกว่า MA200)"))
            return
        held = {p["symbol"] for p in self.acct.open_positions()}
        self.last_research = self.research.scan(self.risk)
        props = self.strategy.proposals(self.last_research, self.risk, held, self.regime)
        n_prop = len(props)
        if props and self.riskd.slots_left(self.risk, self.regime, len(held)) > 0:
            props = self.ai.review(props, self.regime, self.acct.track_record())
        # ถือเต็มแล้ว: ไม่ต้องเสียค่า AI ตรวจ ให้ฝ่ายบริหารความเสี่ยงแจ้งเหตุผลเลย
        orders = self.riskd.approve(props, self.risk, self.regime, self.acct.open_positions(),
                                    self.ex.usdt_free(), eq, self.ex.filters)
        n_ok = len(props)
        bought = []
        max_spread = float(self.cfg.get("max_spread_pct", 0.5)) / 100
        max_slip = float(self.cfg.get("max_slippage_pct", 0.5)) / 100
        for o in orders:
            ok, why = liquidity_check(self.ex, o["symbol"], o["usdt"], max_spread, max_slip,
                                      self.research.tickers.get(o["symbol"]))
            if not ok:
                self.log("ฝ่ายบริหารความเสี่ยง", "ไม่ซื้อ %s: %s" % (o["symbol"], why))
                self.acct.add_decision("ฝ่ายบริหารความเสี่ยง", o["symbol"], False, "สภาพคล่องไม่ผ่าน: " + why)
                continue
            self.log("ฝ่ายบริหารความเสี่ยง", "สภาพคล่อง %s ผ่าน (%s)" % (o["symbol"], why))
            try:
                self.exe.buy(o, self.risk)
                bought.append(o["symbol"])
            except Exception as e:
                self.log("ฝ่ายเทรด", "ซื้อ %s ไม่สำเร็จ: %s" % (o["symbol"], e))
                self.acct.add_decision("ฝ่ายเทรด", o["symbol"], False, "ส่งคำสั่งซื้อไม่สำเร็จ: %s" % e)
        # CEO สรุปงานทุกรอบ: เห็นว่าบอททำงานอยู่ แม้ไม่มีใครเปิดแดชบอร์ด
        self.log("CEO", "สั่งงานรอบนี้: วิจัย %d ตัว → กลยุทธ์เสนอ %d → ผ่าน AI %d → ซื้อ %d%s · ถือ %d/%d ไม้ · ตลาด %s" % (
            len(self.last_research), n_prop, n_ok, len(bought), (" (%s)" % ", ".join(bought)) if bought else "",
            len(self.acct.open_positions()), RISK_PROFILES[self.risk]["max_pos"], self.regime))
        self.save_paper()
        self.daily_report()

    def daily_report(self):
        """สรุปหลังตลาดสหรัฐฯ ปิด (ราว 04:30-05:30 น. เวลาไทย) วันละครั้ง"""
        ny = ny_now()
        if ny.weekday() >= 5 or ny.hour < 16 or (ny.hour == 16 and ny.minute < 15):
            return
        today = now_th().date().isoformat()
        if self.acct.get("report_day") == today:
            return
        s = self.acct.summary()
        s["equity"] = round(self.equity(), 2)
        s["mode"], s["risk"], s["regime"] = self.mode, self.risk, self.regime
        letter = self.ai.daily_letter(s)
        self.log("CEO", "รายงานประจำวัน: " + json.dumps(s, ensure_ascii=False))
        if letter:
            self.log("CEO", "บทสรุปจาก AI: " + letter.replace("\n", " "))
        self.acct.set("report_day", today)
        self.acct.prune()
        day0 = self.acct.get("day_start_" + today) or s["equity"]
        chg = (s["equity"] / day0 - 1) * 100 if day0 else 0
        self.line.send("📊 [หุ้น Webull] สรุปประจำวัน %s (%s)\nมูลค่าพอร์ต: %.2f %s (วันนี้ %+.2f%%)\nกำไร/ขาดทุนสะสม: %+.2f\n"
                       "เทรดแล้ว %d ไม้ ชนะ %s%% | ถืออยู่ %d ไม้\nตลาด: %s\n(ได้ข้อความนี้ทุกคืน = บอทยังทำงานอยู่)%s" % (
                           today, self.mode_label(), s["equity"], self.ex.quote, chg, s["realized_pnl"],
                           s["trades"], s["win_rate"], s["open"], self.regime,
                           ("\n\n" + letter[:1500]) if letter else ""), key="report", every_sec=3600)

    def unguarded_note(self):
        try:
            n = len(self.acct.open_positions())
        except Exception:
            return ""
        return ("\n⚠️ มี %d ไม้ที่บอทเฝ้า stop loss เอง ถ้าบอทยังไม่กลับมา ให้เข้าแอป Webull ตั้ง stop หรือขายเอง" % n) if n else ""

    # ------------------------------------------------------------ ทำงาน 24 ชม.
    HB = os.path.join(HERE, "heartbeat.json")

    def heartbeat(self, clean=False):
        try:
            with open(self.HB, "w") as f:
                json.dump({"t": time.time(), "clean": clean}, f)
        except Exception:
            pass
        self.ping_healthcheck()

    def check_downtime(self):
        """เปิดขึ้นมาแล้วดูว่าก่อนหน้านี้ดับไปนานแค่ไหน (เครื่องรีบูต/โปรแกรมล่ม/เน็ตหลุด) แจ้ง LINE ให้รู้"""
        try:
            with open(self.HB) as f:
                hb = json.load(f)
        except Exception:
            return
        gap = time.time() - hb.get("t", time.time())
        if gap < 180 and hb.get("clean"):
            return
        if gap >= 180 or not hb.get("clean"):
            since = th_hm(hb["t"])
            mins = int(gap // 60)
            self.downtime_note = "บอทหยุดทำงานไป %d นาที (ตั้งแต่ %s)%s" % (
                mins, since, "" if hb.get("clean") else " โดยไม่ได้สั่งปิด")
            if gap >= 180 or (not hb.get("clean") and gap >= 45):
                self.line.send("⚠️ %s\nตอนนี้เปิดกลับมาเองแล้ว ✅ เฝ้าไม้ที่ถือต่อตามปกติ%s" % (
                    self.downtime_note, self.unguarded_note()), key="downtime", every_sec=600, urgent=True)
            self.log("CEO", self.downtime_note + " ตอนนี้กลับมาแล้ว")

    def ping_healthcheck(self):
        """ถ้าตั้ง HEALTHCHECK_URL (เช่น healthchecks.io ฟรี) จะส่งสัญญาณชีพทุกนาที ถ้าเงียบเกินกำหนด เว็บนั้นแจ้งเตือนให้
        ใช้จับกรณีที่บอทแจ้งเองไม่ได้ เช่น VPS ดับทั้งเครื่อง"""
        url = os.environ.get("HEALTHCHECK_URL", "").strip()
        if not url or time.time() - self.last_hc < 60:
            return
        self.last_hc = time.time()

        def go():
            try:
                urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tradingbot"}), timeout=10).read()
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    def monitor_loop(self):
        """เฝ้าไม้ที่ถือทุก 10 วินาที แยกจากการสแกนตลาด: ระหว่างสแกนหลายสิบตัว (นานหลายนาที)
        ราคาถึง stop / เป้า / ล็อกกำไร ก็ขายได้ทันที"""
        every = max(3, int(self.cfg.get("monitor_seconds", 10)))
        while True:
            time.sleep(every)
            if self.status != "running" or not self.clock.get("open"):
                continue
            try:
                self.exe.monitor()
                self.save_paper()
            except Exception as e:
                now = time.time()
                if now - getattr(self, "_mon_err", 0) > 600:
                    self._mon_err = now
                    self.log("ฝ่ายเทรด", "เฝ้าราคาไม่สำเร็จ (จะลองใหม่): %s" % e)

    def keep_thread(self, name, target, *args):
        """เปิด thread งานเสริม (แดชบอร์ด, LINE) และจำไว้ ถ้าตายจะถูกเปิดใหม่โดย watchdog"""
        def start():
            t = threading.Thread(target=target, args=args, daemon=True, name=name)
            t.start()
            self.threads[name] = (t, start)
        start()

    def watchdog(self):
        """ถ้าลูปค้างนานเกิน 15 นาที (เช่นเน็ตค้าง) แจ้ง LINE แล้วปิดโปรแกรม ให้ systemd เปิดใหม่"""
        limit = max(900, self.cfg.get("loop_seconds", 30) * 10)
        while True:
            time.sleep(60)
            for name, (t, start) in list(self.threads.items()):
                if not t.is_alive():
                    self.log("CEO", "ระบบ%s หยุดไป กำลังเปิดใหม่" % name)
                    try:
                        start()
                    except Exception as e:
                        self.log("CEO", "เปิด%sใหม่ไม่ได้: %s" % (name, e))
            if time.time() - self.last_ok > limit:
                self.line.send("🔄 บอทค้างเกิน %d นาที กำลังรีสตาร์ตตัวเอง%s" % (limit // 60, self.unguarded_note()),
                               key="hang", every_sec=3600, urgent=True)
                os._exit(1)

    def run_forever(self):
        try:
            self.start()
        except Exception as e:
            self.line.send("❌ บอทเริ่มทำงานไม่ได้: %s\nระบบจะลองใหม่เองทุก 30 วินาที%s" % (e, self.unguarded_note()),
                           key="start_err", every_sec=3600, urgent=True)
            raise
        self.last_ok = time.time()
        self.check_downtime()
        self.heartbeat()
        self.keep_thread("เฝ้าราคา", self.monitor_loop)
        threading.Thread(target=self.watchdog, daemon=True).start()
        fails = 0
        while True:
            try:
                self.cycle()
                if fails >= 3:
                    self.line.send("✅ บอทกลับมาทำงานปกติแล้ว", key="recovered", every_sec=600)
                fails = 0
            except Exception as e:
                fails += 1
                self.log("CEO", ("Webull ผิดพลาด: %s" if isinstance(e, BinanceError) else "ผิดพลาด: %s") % e)
                if not isinstance(e, BinanceError):
                    traceback.print_exc()
                if fails == 3:  # พลาด 3 รอบติด (ไม่ใช่สะดุดครั้งเดียว) ค่อยแจ้ง
                    self.line.send("⚠️ บอทมีปัญหาติดกัน 3 รอบ: %s%s" % (e, self.unguarded_note()),
                                   key="err", every_sec=3600, urgent=True)
                time.sleep(30)
            self.last_ok = time.time()
            time.sleep(self.cfg.get("loop_seconds", 30))


def make_client(cfg, mode=None):
    return Webull(mode or cfg["mode"], os.environ.get("WEBULL_APP_KEY", ""), os.environ.get("WEBULL_APP_SECRET", ""),
                  fee=cfg.get("fee", 0.001), paper_balance=cfg.get("paper_balance_usd", 1000),
                  region=cfg.get("region", "th"), account_id=os.environ.get("WEBULL_ACCOUNT_ID", cfg.get("account_id", "")),
                  fractional=cfg.get("fractional", True), extra_holidays=cfg.get("extra_holidays", ()))


def confirm_live(cfg):
    print("\n" + "=" * 60)
    print("⚠️  โหมด LIVE: บอทจะซื้อขายหุ้นด้วยเงินจริงในบัญชี Webull ของคุณ")
    print("   ใช้ทุนไม่เกิน $%.0f, ขาดทุนต่อวันเกิน %s%% จะหยุดเปิดไม้ใหม่" % (
        cfg.get("max_capital_usd", 100), cfg.get("max_daily_loss_pct", 3)))
    print("   ขาดทุนได้จริง ไม่มีอะไรรับประกันกำไร")
    print("=" * 60)
    # รันเป็น service บน VPS ไม่มีคนพิมพ์ยืนยัน: ให้ใส่ "live_confirmed": "ยืนยันเงินจริง" ใน config.json แทน
    if cfg.get("live_confirmed") == "ยืนยันเงินจริง":
        print("ยืนยันเงินจริงจาก config.json แล้ว")
        return
    try:
        ans = input('พิมพ์ "ยืนยันเงินจริง" เพื่อเริ่ม: ').strip()
    except EOFError:
        sys.exit('ไม่มีคนพิมพ์ยืนยัน: ใส่ "live_confirmed": "ยืนยันเงินจริง" ใน config.json')
    if ans != "ยืนยันเงินจริง":
        sys.exit("ยกเลิก")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--mode", choices=["paper", "live"])
    ap.add_argument("--risk", choices=list(RISK_PROFILES))
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--check", action="store_true", help="ตรวจการเชื่อมต่อ Webull และยอดเงิน โดยไม่ซื้อขาย")
    ap.add_argument("--check-order", action="store_true", help="ให้ Webull ตรวจคำสั่งซื้อ SPY $5 แบบ preview (ไม่ซื้อจริง)")
    ap.add_argument("--test-line", action="store_true", help="ส่งข้อความทดสอบเข้า LINE")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = เปิดให้เครื่องอื่นเข้าได้ (ต้องตั้ง DASHBOARD_PASSWORD)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    os.chdir(HERE)  # ไฟล์ STOP และฐานข้อมูลอยู่ในโฟลเดอร์โปรแกรมเสมอ
    if a.mode:
        cfg["mode"] = a.mode
    if a.risk:
        cfg["risk"] = a.risk
    for s, sec in (cfg.get("extra_symbols") or {}).items():
        import universe
        universe.UNIVERSE[s.upper()] = (sec, "stock")
    if a.test_line:
        n = LineNotifier(cfg)
        if not n.enabled:
            print("❌ ยังไม่ได้ตั้ง LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID")
        elif n.send("🔔 ทดสอบแจ้งเตือน: ถ้าเห็นข้อความนี้ แปลว่าตั้งค่า LINE ถูกต้องแล้ว"):
            print("✅ ส่งแล้ว ดูใน LINE ได้เลย")
        else:
            print("❌ ส่งไม่สำเร็จ: ตรวจ token / user ID และต้องกดเพิ่มเพื่อน LINE OA ของบอทก่อน")
        return
    if a.check or a.check_order:
        try:
            ex = make_client(cfg, "live")
            if a.check:
                print(json.dumps(ex.check(), ensure_ascii=False, indent=2, default=str))
                print("✅ เชื่อมต่อ Webull ได้")
            if a.check_order:
                print(json.dumps(ex.check_order(), ensure_ascii=False, indent=2, default=str))
                print("✅ Webull ตรวจคำสั่งผ่าน (ไม่ได้ซื้อจริง)")
        except Exception as e:
            print("❌ ผิดพลาด:", e)
        return
    if cfg["mode"] == "live":
        confirm_live(cfg)
    co = Company(cfg)
    co.keep_thread("แดชบอร์ด", dashboard.serve, co, a.port, a.host)
    if cfg.get("line_commands", False):  # LINE OA 1 บัญชีมี webhook ได้ที่เดียว (ปกติเป็นของบอทคริปโต)
        line_bot.serve(co, cfg.get("line_webhook_port", 8091))
    print("แดชบอร์ด: http://localhost:%d" % a.port)

    def stop(*_):  # systemctl stop/restart = ปิดแบบตั้งใจ ไม่ต้องแจ้งว่าดับ
        co.save_paper()
        co.heartbeat(clean=True)
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        co.run_forever()
    except KeyboardInterrupt:
        co.save_paper()
        co.heartbeat(clean=True)
        print("\nปิดบริษัทแล้ว (หุ้นที่ถืออยู่ยังอยู่ในพอร์ต Webull แต่ไม่มีใครเฝ้า stop loss จนกว่าจะเปิดบอทใหม่)")


if __name__ == "__main__":
    main()
