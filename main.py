#!/usr/bin/env python3
"""
main.py — CEO ของบริษัท AI เทรดคริปโต: สั่งงานทุกแผนกเป็นรอบๆ + เปิดหน้าแดชบอร์ด

รัน:
    python main.py                 # ใช้ค่าใน config.json (เริ่มต้น = paper เงินจำลอง)
    python main.py --mode testnet  # เทรดจริงบน Binance Testnet (เงินปลอม)
    python main.py --mode live     # เงินจริง (ต้องยืนยันก่อน)
แดชบอร์ด: เปิด http://localhost:8080 ในเบราว์เซอร์
หยุดฉุกเฉิน: สร้างไฟล์ชื่อ STOP ในโฟลเดอร์นี้ (หยุดเปิดไม้ใหม่) หรือกด Ctrl+C
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback

from binance_client import Binance, BinanceError
from departments import (RISK_PROFILES, AccountingDept, AIAnalystDept, ExecutionDept, ResearchDept,
                         RiskDept, StrategyDept, liquidity_check, now_th)
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
        self.exchange = cfg.get("exchange", "binance")
        tag = self.mode if self.exchange == "binance" else "%s_%s" % (self.exchange, self.mode)
        self.acct = AccountingDept(os.path.join(HERE, "company_%s.db" % tag))
        th = self.exchange == "binance_th"
        self.ex = Binance(self.mode, os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", ""),
                          fee=cfg.get("fee", 0.001), exchange=self.exchange, quote=cfg.get("quote"),
                          paper_balance=cfg.get("paper_balance_th", 30000) if th else cfg.get("paper_balance", 1000))
        if self.mode == "paper":
            saved = self.acct.get("paper_wallet")
            if saved and self.ex.quote in saved:
                self.ex.paper = saved
        self.research = ResearchDept(self.ex, self.log, cfg)
        self.strategy = StrategyDept(self.log)
        self.ai = AIAnalystDept(cfg, self.acct, self.log)
        self.riskd = RiskDept(cfg, self.acct, self.log)
        self.exe = ExecutionDept(self.ex, self.acct, self.log)
        self.regime = "neutral"
        self.last_scan = 0
        self.last_research = []
        self.status = "starting"
        self.line = LineNotifier(cfg)
        self.last_ok = time.time()
        self.sell_queue = []  # คำสั่งขายจาก LINE รอทำในลูปหลัก

    def log(self, dept, msg, line=False, key=None, every=0, urgent=False):
        """line=True = ส่งข้อความนี้เข้า LINE ด้วย"""
        text = "[%s] %-20s %s" % (now_th().strftime("%H:%M:%S"), dept, msg)
        print(text, flush=True)
        self.acct.log(dept, msg)
        if line:
            self.line.send("%s: %s" % (dept, msg), key=key, every_sec=every, urgent=urgent)

    def mode_label(self):
        return {"paper": "เงินจำลอง", "testnet": "testnet", "live": "เงินจริง"}.get(self.mode, self.mode)

    def equity(self):
        pos = self.acct.open_positions()
        px = self.ex.prices([p["symbol"] for p in pos]) if pos else {}
        return self.ex.usdt_free() + sum(p["qty"] * px.get(p["symbol"], p["entry"]) for p in pos)

    def day_start_equity(self, eq):
        key = "day_start_" + now_th().date().isoformat()
        v = self.acct.get(key)
        if v is None:
            self.acct.set(key, eq)
            v = eq
        return v

    def save_paper(self):
        if self.mode == "paper":
            self.acct.set("paper_wallet", self.ex.paper)

    def start(self):
        self.log("CEO", "เปิดบริษัท กระดาน %s (%s) โหมด %s ความเสี่ยง%s" % (
            "Binance TH" if self.ex.th else "Binance.com", self.ex.quote, self.mode.upper(), RISK_PROFILES[self.risk]["name"]))
        self.ex.sync_time()
        self.ex.load_filters()
        self.log("CEO", "โหลดข้อมูลเหรียญคู่ %s ที่เทรดได้ %d คู่" % (self.ex.quote, len(self.ex.filters)))
        if not self.ex.filters:
            raise BinanceError("ไม่พบเหรียญคู่ %s บนกระดานนี้ ลองตั้ง quote ใน config.json" % self.ex.quote)
        if self.mode != "paper":
            self.log("CEO", "เงิน %s ในบัญชี: %.2f" % (self.ex.quote, self.ex.usdt_free()))
        self.status = "running"
        # กันสแปมตอนบอทรีสตาร์ตซ้ำๆ: แจ้งว่าเริ่มงานไม่เกินครึ่งชั่วโมงละครั้ง
        self.line.send("✅ บอทเริ่มทำงานแล้ว\nโหมด: %s | ความเสี่ยง: %s\nเงินในบัญชี: %.2f %s\nถือไม้อยู่: %d" % (
            self.mode_label(), RISK_PROFILES[self.risk]["name"], self.ex.usdt_free(), self.ex.quote,
            len(self.acct.open_positions())), key="start", every_sec=1800)

    def cycle(self):
        """1 รอบการทำงาน: เฝ้าสถานะ → (ถึงเวลา) สแกน → กลยุทธ์ → AI → ความเสี่ยง → ส่งคำสั่ง"""
        while self.sell_queue:  # ทำคำสั่งขายที่สั่งมาจาก LINE ก่อน
            sym = self.sell_queue.pop(0)
            for p in self.acct.open_positions():
                if p["symbol"] == sym:
                    try:
                        self.exe.sell(p, "ขายตามคำสั่ง LINE")
                    except Exception as e:
                        self.log("ฝ่ายเทรด", "ขาย %s ตามคำสั่ง LINE ไม่สำเร็จ: %s" % (sym, e), line=True)
        self.exe.monitor()
        eq = self.equity()
        self.acct.record_equity(eq)
        cfg = RISK_PROFILES[self.risk]
        if time.time() - self.last_scan < cfg["every_min"] * 60:
            self.save_paper()
            return
        self.last_scan = time.time()
        day0 = self.day_start_equity(eq)
        if not self.riskd.can_trade(eq, day0):
            return
        self.regime = self.research.market_regime()
        if self.regime == "bear" and self.cfg.get("skip_bear_market", True) and self.risk != "high":
            self.log("CEO", "ตลาดขาลง: งดเปิดไม้ใหม่รอบนี้ (ตั้ง skip_bear_market=false เพื่อปิด)")
            return
        held = {p["symbol"] for p in self.acct.open_positions()}
        self.last_research = self.research.scan(self.risk)
        props = self.strategy.proposals(self.last_research, self.risk, held)
        props = self.ai.review(props, self.regime)
        orders = self.riskd.approve(props, self.risk, self.regime, self.acct.open_positions(),
                                    self.ex.usdt_free(), eq, self.ex.filters)
        max_spread = float(self.cfg.get("max_spread_pct", 0.5)) / 100
        max_slip = float(self.cfg.get("max_slippage_pct", 0.5)) / 100
        for o in orders:
            ok, why = liquidity_check(self.ex, o["symbol"], o["usdt"], max_spread, max_slip,
                                      self.research.tickers.get(o["symbol"]))
            if not ok:
                self.log("ฝ่ายบริหารความเสี่ยง", "ไม่ซื้อ %s: %s" % (o["symbol"], why))
                continue
            self.log("ฝ่ายบริหารความเสี่ยง", "สภาพคล่อง %s ผ่าน (%s)" % (o["symbol"], why))
            try:
                self.exe.buy(o, self.risk)
            except Exception as e:
                self.log("ฝ่ายเทรด", "ซื้อ %s ไม่สำเร็จ: %s" % (o["symbol"], e))
        self.save_paper()
        self.daily_report()

    def daily_report(self):
        today = now_th().date().isoformat()
        if self.acct.get("report_day") == today or now_th().hour < 21:
            return
        s = self.acct.summary()
        s["equity"] = round(self.equity(), 2)
        s["mode"], s["risk"], s["regime"] = self.mode, self.risk, self.regime
        letter = self.ai.daily_letter(s)
        self.log("CEO", "รายงานประจำวัน: " + json.dumps(s, ensure_ascii=False))
        if letter:
            self.log("CEO", "บทสรุปจาก AI: " + letter.replace("\n", " "))
        self.acct.set("report_day", today)
        day0 = self.acct.get("day_start_" + today) or s["equity"]
        chg = (s["equity"] / day0 - 1) * 100 if day0 else 0
        self.line.send("📊 สรุปประจำวัน %s (%s)\nมูลค่าพอร์ต: %.2f %s (วันนี้ %+.2f%%)\nกำไร/ขาดทุนสะสม: %+.2f\n"
                       "เทรดแล้ว %d ไม้ ชนะ %s%% | ถืออยู่ %d ไม้\nตลาด: %s\n(ได้ข้อความนี้ทุกคืน = บอทยังทำงานอยู่)%s" % (
                           today, self.mode_label(), s["equity"], self.ex.quote, chg, s["realized_pnl"],
                           s["trades"], s["win_rate"], s["open"], self.regime,
                           ("\n\n" + letter[:1500]) if letter else ""), key="report", every_sec=3600)

    def unguarded_note(self):
        try:
            n = len([p for p in self.acct.open_positions() if not p["oco_id"]])
        except Exception:
            return ""
        return ("\n⚠️ มี %d ไม้ที่บอทต้องเฝ้า stop loss เอง ถ้าแก้ไม่ได้ให้เข้าแอป Binance ขายเอง" % n) if n else ""

    def watchdog(self):
        """ถ้าลูปค้างนานเกิน 15 นาที (เช่นเน็ตค้าง) แจ้ง LINE แล้วปิดโปรแกรม ให้ systemd เปิดใหม่"""
        limit = max(900, self.cfg.get("loop_seconds", 30) * 10)
        while True:
            time.sleep(60)
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
                self.log("CEO", ("Binance ผิดพลาด: %s" if isinstance(e, BinanceError) else "ผิดพลาด: %s") % e)
                if not isinstance(e, BinanceError):
                    traceback.print_exc()
                if fails == 3:  # พลาด 3 รอบติด (ไม่ใช่สะดุดครั้งเดียว) ค่อยแจ้ง
                    self.line.send("⚠️ บอทมีปัญหาติดกัน 3 รอบ: %s%s" % (e, self.unguarded_note()),
                                   key="err", every_sec=3600, urgent=True)
                time.sleep(30)
            self.last_ok = time.time()
            time.sleep(self.cfg.get("loop_seconds", 30))


def confirm_live(cfg):
    print("\n" + "=" * 60)
    print("⚠️  โหมด LIVE: บอทจะซื้อขายด้วยเงินจริงในบัญชี Binance ของคุณ")
    print("   ใช้ทุนไม่เกิน %.0f (สกุลเงินที่เทรด), ขาดทุนต่อวันเกิน %s%% จะหยุดเปิดไม้ใหม่" % (
        cfg.get("max_capital_usdt", 100), cfg.get("max_daily_loss_pct", 5)))
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
    ap.add_argument("--mode", choices=["paper", "testnet", "live"])
    ap.add_argument("--risk", choices=list(RISK_PROFILES))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--check", action="store_true", help="ตรวจการเชื่อมต่อกระดานและยอดเงิน โดยไม่ซื้อขาย")
    ap.add_argument("--exchange", choices=["binance", "binance_th"])
    ap.add_argument("--markets", action="store_true", help="แสดงคู่เหรียญทั้งหมดที่ API เปิดให้")
    ap.add_argument("--test-line", action="store_true", help="ส่งข้อความทดสอบเข้า LINE")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = เปิดให้เครื่องอื่นเข้าได้ (ต้องตั้ง DASHBOARD_PASSWORD)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    os.chdir(HERE)  # ไฟล์ STOP และฐานข้อมูลอยู่ในโฟลเดอร์โปรแกรมเสมอ
    if a.mode:
        cfg["mode"] = a.mode
    if a.risk:
        cfg["risk"] = a.risk
    if a.exchange:
        cfg["exchange"] = a.exchange
    if a.markets:
        ex = Binance("paper", exchange=cfg.get("exchange", "binance"), quote=cfg.get("quote"))
        for k, v in ex.list_markets().items():
            print("%s => %s" % (k, v))
        return
    if a.test_line:
        n = LineNotifier(cfg)
        if not n.enabled:
            print("❌ ยังไม่ได้ตั้ง LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID")
        elif n.send("🔔 ทดสอบแจ้งเตือน: ถ้าเห็นข้อความนี้ แปลว่าตั้งค่า LINE ถูกต้องแล้ว"):
            print("✅ ส่งแล้ว ดูใน LINE ได้เลย")
        else:
            print("❌ ส่งไม่สำเร็จ: ตรวจ token / user ID และต้องกดเพิ่มเพื่อน LINE OA ของบอทก่อน")
        return
    if a.check:
        try:
            ex = Binance(cfg["mode"], os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", ""),
                         exchange=cfg.get("exchange", "binance"), quote=cfg.get("quote"))
            print(json.dumps(ex.check(), ensure_ascii=False, indent=2))
            print("✅ เชื่อมต่อได้")
        except Exception as e:
            print("❌ ผิดพลาด:", e)
        return
    if cfg["mode"] == "live":
        confirm_live(cfg)
    co = Company(cfg)
    threading.Thread(target=dashboard.serve, args=(co, a.port, a.host), daemon=True).start()
    line_bot.serve(co, cfg.get("line_webhook_port", 8090))
    print("แดชบอร์ด: http://localhost:%d" % a.port)
    try:
        co.run_forever()
    except KeyboardInterrupt:
        co.save_paper()
        print("\nปิดบริษัทแล้ว (สถานะที่ถืออยู่ยังอยู่ ถ้าตั้ง OCO ไว้ คำสั่งขายยังรออยู่บน Binance)")


if __name__ == "__main__":
    main()
