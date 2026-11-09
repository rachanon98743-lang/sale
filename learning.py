"""
learning.py — ฝ่ายพัฒนากลยุทธ์ (เรียนรู้จากการวิเคราะห์และการเทรดที่ผ่านมา)

แนวคิด: เน้นกำไรสม่ำเสมอ + รักษาเงินต้น
  1. ติดตามผล "ถ้าซื้อจะเป็นยังไง" (shadow) ของทุกข้อเสนอ ทั้งที่ซื้อและที่ถูกปฏิเสธ
     → รู้ว่าด่านไหน (กลยุทธ์ / Claude / Gemini / ความเสี่ยง) กันขาดทุนได้จริง หรือปัดกำไรทิ้ง
  2. ผลที่ติดตามได้ใช้สอนฝ่ายกลยุทธ์เพิ่ม (น้ำหนักครึ่งหนึ่งของเทรดจริง) เรียนรู้ได้เร็วขึ้นมาก
  3. ปรับเกณฑ์ค้านของ Gemini เองแบบมีขอบเขต (70-95%) เมื่อสถิติพอ
  4. สรุปรีวิวแบบผู้จัดการกองทุน: expectancy, profit factor, drawdown, จังหวะ/เหตุผลปิดไม้, พร้อมใช้เงินจริงไหม
ผลติดตามใช้ stop/เป้าคงที่ (ไม่มี trailing) จึงเป็นค่าประมาณ ใช้เทียบกันระหว่างด่าน ไม่ใช่กำไรจริง
"""

import json
import threading
import time
from datetime import datetime, timedelta

from departments import FEATURE_TH, RISK_PROFILES, now_th

STAGE_TH = {"bought": "ซื้อจริง", "strategy": "ฝ่ายกลยุทธ์คัดออก", "claude": "Claude ปฏิเสธ", "gemini": "Gemini ค้าน",
            "full": "ถือครบเพดาน", "risk": "ฝ่ายความเสี่ยงไม่อนุมัติ", "liquidity": "สภาพคล่องไม่ผ่าน"}
DEPT_STAGE = {"ฝ่ายกลยุทธ์": "strategy", "ฝ่ายนักวิเคราะห์ AI": "claude", "ฝ่ายวิเคราะห์ Gemini": "gemini",
              "ฝ่ายบริหารความเสี่ยง": "risk"}


def _ms(t):
    return datetime.fromisoformat(t).timestamp() * 1000


def outcome(bars, t0_ms, price, sl_pct, tp_pct, fee, expire_ms):
    """ไล่แท่ง 1 ชม. หลังเวลาสัญญาณ: โดน stop ก่อน = แพ้, ถึงเป้าก่อน = ชนะ, แท่งเดียวโดนทั้งคู่ นับแพ้ (มองแง่ร้าย)
    คืน (status, result) หรือ None ถ้ายังไม่จบ"""
    sl, tp, cost = price * (1 - sl_pct), price * (1 + tp_pct), 2 * fee
    last = None
    for b in bars:
        if b[0] < t0_ms:
            continue
        hi, lo, last = float(b[2]), float(b[3]), float(b[4])
        if lo <= sl:
            return "loss", -sl_pct - cost
        if hi >= tp:
            return "win", tp_pct - cost
        if b[0] >= expire_ms:
            break
    if last is not None and bars and bars[-1][0] >= expire_ms:
        r = last / price - 1 - cost
        return ("win" if r > 0 else "loss") if abs(r) > 0.004 else "flat", r
    return None


class LearningDept:
    name = "ฝ่ายพัฒนากลยุทธ์"

    def __init__(self, ex, acct, log, cfg=None):
        self.ex, self.acct, self.log, self.cfg = ex, acct, log, cfg or {}
        self.last_resolve = 0
        self.lock = threading.Lock()

    @property
    def hours(self):
        return float(self.cfg.get("shadow_hours", 72))

    # ------------------------------------------------------------ บันทึกสัญญาณ
    def _has_open(self, symbol):
        return self.acct._one("SELECT 1 FROM signals WHERE symbol=? AND status='open'", (symbol,)) is not None

    def add(self, symbol, stage, price, sl_pct, tp_pct, features=None, reason="", source="live", t=None):
        if not price or not sl_pct or not tp_pct or self._has_open(symbol):
            return
        self.acct._x("INSERT INTO signals(t,symbol,stage,price,sl_pct,tp_pct,features,reason,source,status) "
                     "VALUES(?,?,?,?,?,?,?,?,?,'open')",
                     (t or now_th().isoformat(timespec="seconds"), symbol, stage, float(price), float(sl_pct), float(tp_pct),
                      json.dumps(features or {}, ensure_ascii=False), (reason or "")[:200], source))

    def record_round(self, rejected_by_strategy, props, ai_rejected, orders, bought, liquidity_failed, full):
        """บันทึกทุกข้อเสนอของรอบนี้ พร้อมด่านที่ทำให้ไม่ซื้อ"""
        try:
            for r, why in rejected_by_strategy:
                self.add(r["symbol"], "strategy", r.get("price"), r.get("sl_pct"), r.get("tp_pct"),
                         r.get("features"), why)
            order_syms = {o["symbol"] for o in orders}
            for p in props:
                s = p["symbol"]
                if s in bought:
                    stage = "bought"
                elif s in ai_rejected:
                    stage = ai_rejected[s]
                elif s in liquidity_failed:
                    stage = "liquidity"
                elif s in full:
                    stage = "full"
                elif s not in order_syms:
                    stage = "risk"
                else:
                    continue  # ส่งคำสั่งแล้วแต่ซื้อไม่สำเร็จ
                self.add(s, stage, p.get("price"), p.get("sl_pct"), p.get("tp_pct"), p.get("features"),
                         p.get("ai_reason", ""))
        except Exception as e:
            self.log(self.name, "บันทึกสัญญาณไม่สำเร็จ: %s" % e)

    # ------------------------------------------------------------ ติดตามผล
    def resolve(self, force=False):
        if not force and time.time() - self.last_resolve < 1800:
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.last_resolve = time.time()
            rows = self.acct._q("SELECT rowid, * FROM signals WHERE status='open'")
            by_sym = {}
            for r in rows:
                by_sym.setdefault(r["symbol"], []).append(r)
            done = []
            for sym, rs in by_sym.items():
                t0 = min(_ms(r["t"]) for r in rs)
                n = int(min(500, (time.time() * 1000 - t0) / 3600000 + 3))
                try:
                    bars = self.ex.klines(sym, "1h", max(n, 3))
                except Exception:
                    continue
                for r in rs:
                    t = _ms(r["t"])
                    o = outcome(bars, t, r["price"], r["sl_pct"], r["tp_pct"], self.ex.fee, t + self.hours * 3600000)
                    if o:
                        self.acct._x("UPDATE signals SET status=?, result=?, closed=? WHERE rowid=?",
                                     (o[0], o[1], now_th().isoformat(timespec="seconds"), r["rowid"]))
                        done.append((r, o))
            if done:
                self.log(self.name, "ติดตามผลสัญญาณจบ %d ตัว: %s" % (len(done), ", ".join(
                    "%s(%s %s %+.1f%%)" % (r["symbol"].replace(self.ex.quote, ""), STAGE_TH.get(r["stage"], r["stage"]),
                                          {"win": "ชนะ", "loss": "แพ้", "flat": "เสมอ"}[o[0]], o[1] * 100)
                    for r, o in done[:8])))
                self.tune_gemini()
        finally:
            self.lock.release()

    def backfill(self, risk):
        """ครั้งแรก: ย้อนเอาข้อเสนอที่เคยถูกปฏิเสธในอดีต (ตาราง decisions 8 วันล่าสุด) มาดูว่าถ้าซื้อจะเป็นยังไง"""
        if self.acct.get("shadow_backfill_done"):
            return
        try:
            since = (now_th() - timedelta(days=8)).isoformat()
            rows = self.acct._q("SELECT t, dept, symbol, reason FROM decisions WHERE ok=0 AND t >= ? ORDER BY t", (since,))
            prof = RISK_PROFILES[risk]
            last_t, n = {}, 0
            for d in rows:
                stage = DEPT_STAGE.get(d["dept"])
                if not stage:
                    continue
                if stage == "risk" and "ถือครบ" in (d["reason"] or ""):
                    stage = "full"
                for sym in [s.strip() for s in (d["symbol"] or "").split(",")]:
                    if not sym.endswith(self.ex.quote) or sym not in self.ex.filters:
                        continue
                    t = _ms(d["t"])
                    if t - last_t.get(sym, 0) < 6 * 3600000:  # เหรียญเดิมนับครั้งเดียวต่อ 6 ชม.
                        continue
                    last_t[sym] = t
                    self.acct._x("INSERT INTO signals(t,symbol,stage,price,sl_pct,tp_pct,features,reason,source,status) "
                                 "VALUES(?,?,?,0,?,?,'{}',?,'history','pending')",
                                 (d["t"], sym, stage, prof["sl"], prof["tp"], (d["reason"] or "")[:200]))
                    n += 1
                    if n >= 300:
                        break
            # หาราคา ณ ตอนนั้น แล้วตัดสินผลจากกราฟทันที (ดึงกราฟครั้งเดียวต่อเหรียญ)
            pend = self.acct._q("SELECT rowid, * FROM signals WHERE status='pending'")
            by_sym = {}
            for r in pend:
                by_sym.setdefault(r["symbol"], []).append(r)
            for sym, rs in by_sym.items():
                try:
                    bars = self.ex.klines(sym, "1h", 250)
                except Exception:
                    continue
                for r in rs:
                    t = _ms(r["t"])
                    bar = next((b for b in bars if b[0] <= t < b[0] + 3600000), None)
                    if not bar:
                        self.acct._x("DELETE FROM signals WHERE rowid=?", (r["rowid"],))
                        continue
                    px = float(bar[4])
                    o = outcome(bars, bar[0] + 3600000, px, r["sl_pct"], r["tp_pct"], self.ex.fee, t + self.hours * 3600000)
                    if o:
                        self.acct._x("UPDATE signals SET price=?, status=?, result=?, closed=? WHERE rowid=?",
                                     (px, o[0], o[1], now_th().isoformat(timespec="seconds"), r["rowid"]))
                    else:
                        self.acct._x("UPDATE signals SET price=?, status='open' WHERE rowid=?", (px, r["rowid"]))
            self.acct._x("DELETE FROM signals WHERE status='pending'")
            self.acct.set("shadow_backfill_done", now_th().isoformat(timespec="seconds"))
            got = self.acct._one("SELECT COUNT(*) n FROM signals WHERE source='history'")["n"]
            self.log(self.name, "ย้อนดูข้อเสนอในอดีต 8 วัน ได้ %d สัญญาณ มาเรียนรู้ว่าด่านไหนกันขาดทุนได้จริง" % got)
            self.tune_gemini()
        except Exception as e:
            self.log(self.name, "ย้อนดูข้อมูลเก่าไม่สำเร็จ: %s" % e)

    # ------------------------------------------------------------ สถิติ
    def stage_stats(self, days=30):
        since = (now_th() - timedelta(days=days)).isoformat()
        rows = self.acct._q("SELECT stage, status, result FROM signals WHERE status IN ('win','loss','flat') AND t >= ?", (since,))
        out = {}
        for r in rows:
            s = out.setdefault(r["stage"], {"n": 0, "win": 0, "sum": 0.0})
            s["n"] += 1
            s["win"] += r["status"] == "win"
            s["sum"] += r["result"] or 0
        for s in out.values():
            s["win_pct"] = round(s["win"] / s["n"] * 100) if s["n"] else 0
            s["avg_pct"] = round(s["sum"] / s["n"] * 100, 2) if s["n"] else 0
        return out

    def tune_gemini(self):
        """Gemini ค้านแล้วเหรียญนั้นขึ้นจริงบ่อย = ค้านเข้มไป → ขยับเกณฑ์ค้านขึ้น / ค้านถูกบ่อย = ขยับลง (70-95%)"""
        if not self.cfg.get("gemini_veto_auto", True):
            return
        today = now_th().date().isoformat()
        if self.acct.get("veto_tuned_day") == today:
            return
        g = self.stage_stats(30).get("gemini")
        if not g or g["n"] < 10:
            return
        cur = self.acct.get("set_gemini_veto_conf")
        cur = int(self.cfg.get("gemini_veto_conf", 80) if cur is None else cur)
        new = cur
        if g["avg_pct"] > 0.3 and g["win_pct"] >= 55:
            new = min(95, cur + 5)
        elif g["avg_pct"] < 0 and g["win_pct"] <= 40:
            new = max(70, cur - 5)
        if new != cur:
            self.acct.set("set_gemini_veto_conf", new)
            self.log(self.name, "ปรับเกณฑ์ Gemini ค้าน %d%% → %d%% (เหรียญที่ Gemini ค้าน %d ตัว ถ้าซื้อจะชนะ %d%% เฉลี่ย %+.2f%%)" % (
                cur, new, g["n"], g["win_pct"], g["avg_pct"]), line=True, key="veto_tune", every=86400)
        self.acct.set("veto_tuned_day", today)

    def trade_stats(self, days=30):
        since = (now_th() - timedelta(days=days)).isoformat()
        rows = [self.acct._pos(r) for r in self.acct._q(
            "SELECT * FROM positions WHERE status='closed' AND closed >= ? ORDER BY closed", (since,))]
        n = len(rows)
        res = [r["pnl"] / r["cost"] for r in rows if r["cost"]]
        wins = [x for x in res if x > 0]
        losses = [x for x in res if x <= 0]
        gp, gl = sum(r["pnl"] for r in rows if r["pnl"] > 0), -sum(r["pnl"] for r in rows if r["pnl"] <= 0)
        by = {"setup": {}, "reason": {}, "symbol": {}}
        for r, x in zip(rows, res):
            for k, v in (("setup", (r["meta_d"].get("features") or {}).get("setup", "-")), ("reason", r["reason"] or "-"),
                         ("symbol", r["symbol"].replace(self.ex.quote, ""))):
                b = by[k].setdefault(v, {"n": 0, "win": 0, "sum": 0.0})
                b["n"] += 1
                b["win"] += x > 0
                b["sum"] += x
        streak = 0
        for x in reversed(res):
            if x > 0:
                break
            streak += 1
        first = self.acct._one("SELECT MIN(t) t FROM equity")
        days_run = (now_th() - datetime.fromisoformat(first["t"])).days if first and first["t"] else 0
        return {"n": n, "win_pct": round(len(wins) / n * 100, 1) if n else 0,
                "avg_win": round(sum(wins) / len(wins) * 100, 2) if wins else 0,
                "avg_loss": round(sum(losses) / len(losses) * 100, 2) if losses else 0,
                "exp": round(sum(res) / n * 100, 2) if n else 0,
                "pf": round(gp / gl, 2) if gl > 0 else (99 if gp > 0 else 0),
                "pnl": round(gp - gl, 2), "max_dd": round(self.max_drawdown(days) * 100, 1),
                "streak": streak, "days_run": days_run, "by": by}

    def max_drawdown(self, days=30):
        since = (now_th() - timedelta(days=days)).isoformat()
        peak, dd = 0, 0
        for r in self.acct._q("SELECT value FROM equity WHERE t >= ? ORDER BY t", (since,)):
            peak = max(peak, r["value"])
            if peak:
                dd = max(dd, 1 - r["value"] / peak)
        return dd

    def lessons(self):
        """บทเรียนสั้นๆ ส่งให้ AI ใช้ประกอบการตัดสิน"""
        out = []
        try:
            for stage, s in self.stage_stats(30).items():
                if s["n"] >= 5:
                    out.append("%s %d ตัว ถ้าซื้อจะชนะ %d%% เฉลี่ย %+.2f%%" % (STAGE_TH.get(stage, stage), s["n"], s["win_pct"], s["avg_pct"]))
            t = self.trade_stats(30)
            for v, b in t["by"]["setup"].items():
                if b["n"] >= 4:
                    out.append("เทรดจริงจังหวะ %s %d ไม้ ชนะ %d%% เฉลี่ย %+.2f%%" % (v, b["n"], b["win"] * 100 // b["n"], b["sum"] / b["n"] * 100))
        except Exception:
            pass
        return out[:8]

    # ------------------------------------------------------------ รายงาน
    def review(self, guard=None):
        t = self.trade_stats(30)
        st = self.stage_stats(30)
        ready = [
            ("รัน paper ≥ 14 วัน", t["days_run"] >= 14, "%d วัน" % t["days_run"]),
            ("ปิดไม้แล้ว ≥ 30 ไม้", t["n"] >= 30, "%d ไม้" % t["n"]),
            ("กำไรคาดหวังต่อไม้ > 0 (หักค่าธรรมเนียมแล้ว)", t["exp"] > 0, "%+.2f%%" % t["exp"]),
            ("Profit factor > 1.3", t["pf"] > 1.3, "%.2f" % t["pf"]),
            ("Drawdown สูงสุด ≤ 10%", t["max_dd"] <= 10, "%.1f%%" % t["max_dd"]),
        ]
        tips = []
        for v, b in t["by"]["setup"].items():
            if b["n"] >= 5 and b["sum"] < 0:
                tips.append("จังหวะ \"%s\" ขาดทุนเฉลี่ย %+.2f%% จาก %d ไม้ → ระบบลดคะแนนจังหวะนี้ให้อัตโนมัติแล้ว" % (v, b["sum"] / b["n"] * 100, b["n"]))
        to = t["by"]["reason"].get("ตัดขาดทุน")
        if t["n"] >= 10 and to and to["n"] / t["n"] > 0.5:
            tips.append("โดนตัดขาดทุน %d/%d ไม้ → ลองความเสี่ยงต่ำ (TF 4h, คะแนนผ่าน 75) ให้คัดเข้มขึ้น" % (to["n"], t["n"]))
        g = st.get("gemini")
        if g and g["n"] >= 8:
            tips.append("Gemini ค้าน %d ตัว ถ้าซื้อจะเฉลี่ย %+.2f%% → %s" % (
                g["n"], g["avg_pct"], "Gemini ช่วยกันขาดทุนได้จริง" if g["avg_pct"] < 0 else "ค้านเข้มไปนิด (ระบบปรับเกณฑ์เองแล้ว)"))
        f = st.get("full")
        if f and f["n"] >= 8 and f["avg_pct"] > 0.5:
            tips.append("เหรียญที่พลาดเพราะถือครบเพดาน %d ตัว ถ้าซื้อจะเฉลี่ย %+.2f%% → ไม้ที่ถืออาจค้างนาน ระบบมีขายเมื่อหมดเวลาแล้ว" % (f["n"], f["avg_pct"]))
        if t["n"] < 10:
            tips.append("ยังมีไม้ปิดแล้วไม่ถึง 10 ไม้ สถิติยังไม่นิ่ง ให้บอทรัน paper ต่อ")
        return {"trades": t, "stages": st, "ready": ready, "ready_all": all(ok for _, ok, _ in ready),
                "tips": tips[:5], "guard": guard, "learned": self.top_learned()}

    def top_learned(self):
        rows = []
        strat = getattr(self, "strategy", None)
        learned = (strat.learn() if strat else {}) or {}
        for (k, v), s in learned.items():
            rows.append({"k": FEATURE_TH.get(k, k), "v": v, "n": round(s["n"], 1), "win": round(s["win"] * 100), "avg": round(s["avg"] * 100, 2)})
        rows.sort(key=lambda r: r["avg"])
        return (rows[:3] + rows[-3:]) if len(rows) > 6 else rows
