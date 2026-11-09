"""line_bot.py — สั่งงานบอทผ่านแชท LINE OA (รับ webhook จาก LINE แล้วตอบกลับ)

ต้องตั้งใน /root/bot.env:
  LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID   (ใช้ร่วมกับระบบแจ้งเตือน)
  LINE_CHANNEL_SECRET                       (Basic settings → Channel secret ใช้ตรวจว่าข้อความมาจาก LINE จริง)

เปิดให้ LINE ส่ง webhook เข้ามาได้ด้วย Tailscale Funnel (HTTPS ฟรี):  tailscale funnel --bg 8091
Webhook URL = https://<ชื่อเครื่อง>.<tailnet>.ts.net/line/webhook

ความปลอดภัย:
  - ตรวจลายเซ็น X-Line-Signature ทุกครั้ง ข้อความปลอมถูกทิ้ง
  - รับคำสั่งจาก LINE_USER_ID (เจ้าของ) คนเดียว คนอื่นทักมาจะไม่ได้อะไรเลย
  - คำสั่งขายต้องพิมพ์ยืนยันอีกรอบ และไม่มีคำสั่งถอนเงินหรือเปลี่ยน key ผ่าน LINE
  - การตอบกลับใช้ Reply API ซึ่งไม่กินโควตาข้อความรายเดือน
"""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from departments import RISK_PROFILES, now_th, th_hm

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
API = "https://api.line.me/v2/bot"
LAST_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "line_last_webhook.txt")
HELP = """📋 คำสั่งที่ใช้ได้ (กดปุ่มด้านล่างแชทได้เลย)
• สถานะ – มูลค่าพอร์ต กำไร โหมด
• แผนก – งานล่าสุดของทุกแผนก (แตะดูทีละแผนก)
• ไม่อนุมัติ – หุ้นที่ไม่ซื้อ พร้อมเหตุผลของแต่ละฝ่าย
• คาดการณ์ – ไม้ที่ถือ ถ้าถึงเป้า/โดนตัด ได้เสียเท่าไร
• แดชบอร์ด – ลิงก์เปิดหน้าแดชบอร์ด (เปิด Tailscale ก่อน)
• ไม้ที่ถือ – หุ้นที่ถืออยู่ + กำไร/ขาดทุนตอนนี้
• สรุปวันนี้ – ซื้อขายวันนี้ + ค่า AI
• ตรวจไลน์ – ตรวจ/ซ่อมการเชื่อมต่อ LINE
• หยุด – หยุดเปิดไม้ใหม่ (ไม้เดิมยังเฝ้าอยู่)
• เริ่ม – กลับมาเปิดไม้ใหม่
• ขาย AAPL – ขายหุ้นนั้น (ต้องยืนยัน)
• ขายหมด – ขายทุกไม้ (ต้องยืนยัน)
• พิมพ์คำถามอะไรก็ได้ – ให้ AI ตอบจากข้อมูลพอร์ตจริง"""


def _n(x):
    return "{:,.2f}".format(x) if x >= 100 else "%.6g" % x


DEPTS = [("👔", "CEO", ("ceo", "ซีอีโอ")),
         ("🔎", "ฝ่ายวิจัยตลาด", ("ฝ่ายวิจัย", "วิจัย", "วิจัยตลาด")),
         ("♟️", "ฝ่ายกลยุทธ์", ("กลยุทธ์",)),
         ("🧠", "ฝ่ายนักวิเคราะห์ AI", ("ai", "นักวิเคราะห์", "ฝ่ายai", "นักวิเคราะห์ai")),
         ("🛡️", "ฝ่ายบริหารความเสี่ยง", ("ความเสี่ยง", "บริหารความเสี่ยง", "ฝ่ายความเสี่ยง")),
         ("⚡", "ฝ่ายเทรด", ("เทรด",)),
         ("📒", "ฝ่ายบัญชี", ("บัญชี",))]
SHORT = {"CEO": "CEO", "ฝ่ายวิจัยตลาด": "วิจัย", "ฝ่ายกลยุทธ์": "กลยุทธ์", "ฝ่ายนักวิเคราะห์ AI": "AI",
         "ฝ่ายบริหารความเสี่ยง": "ความเสี่ยง", "ฝ่ายเทรด": "เทรด", "ฝ่ายบัญชี": "บัญชี"}
MAIN_QR = ["สถานะ", "แผนก", "ไม่อนุมัติ", "คาดการณ์", "ไม้ที่ถือ", "สรุปวันนี้", "แดชบอร์ด", "ช่วยเหลือ"]


def dept_of(t):
    for icon, name, alias in DEPTS:
        if t in (name.replace(" ", "").lower(),) + alias:
            return icon, name
    return None


def _ago(iso):
    try:
        from datetime import datetime
        s = (now_th() - datetime.fromisoformat(iso)).total_seconds()
    except Exception:
        return ""
    return "เมื่อสักครู่" if s < 60 else "%d นาทีที่แล้ว" % (s // 60) if s < 3600 else "%d ชม.ที่แล้ว" % (s // 3600)


class LineBot:
    def __init__(self, company):
        self.co = company
        self.token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        self.secret = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
        self.owner = os.environ.get("LINE_USER_ID", "").strip()
        self.pending = {}  # คำสั่งที่รอยืนยัน: ข้อความยืนยัน -> (เวลา, รายการ symbol)

    @property
    def ready(self):
        return bool(self.token and self.secret and self.owner)

    # ---------------------------------------------------------------- LINE I/O
    def verify(self, body, signature):
        mac = hmac.new(self.secret.encode(), body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(mac).decode(), signature or "")

    def reply(self, reply_token, text, quick=None):
        msg = {"type": "text", "text": text[:4900]}
        items = quick if quick is not None else MAIN_QR
        if items:  # ปุ่มลัดใต้ข้อความ กดแล้วส่งคำสั่งทันที (ตอบกลับฟรี ไม่กินโควตา)
            msg["quickReply"] = {"items": [{"type": "action", "action": {"type": "message", "label": q[:20], "text": q}}
                                           for q in items[:13]]}
        body = json.dumps({"replyToken": reply_token, "messages": [msg]}).encode()
        req = urllib.request.Request(REPLY_URL, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            self.co.log("CEO", "ตอบ LINE ไม่สำเร็จ: %s" % e)

    def handle_events(self, payload):
        if payload.get("events"):
            self.last_webhook = time.time()
            try:  # ให้ manage.sh ข้อ 17 (รันแยกโปรเซส) รู้ด้วยว่าข้อความจริงเข้ามาล่าสุดเมื่อไร
                with open(LAST_HOOK, "w") as f:
                    f.write(str(self.last_webhook))
            except Exception:
                pass
            self.co.acct.set("line_last_webhook", now_th().isoformat(timespec="seconds"))
        for ev in payload.get("events", []):
            if ev.get("source", {}).get("userId") != self.owner:
                continue  # ไม่ใช่เจ้าของ: เงียบ
            if ev.get("type") != "message" or ev.get("message", {}).get("type") != "text":
                continue
            text = ev["message"]["text"].strip()
            token = ev.get("replyToken")
            # งานที่อาจช้า (เช่นถาม AI) ทำใน thread แยก LINE ต้องการให้ตอบ 200 เร็ว
            threading.Thread(target=self._run, args=(text, token), daemon=True).start()

    def _run(self, text, token):
        quick = None
        try:
            out = self.command(text)
            if isinstance(out, tuple):
                out, quick = out
        except Exception as e:
            out = "⚠️ ทำคำสั่งไม่สำเร็จ: %s" % e
        self.reply(token, out, quick)

    # ---------------------------------------------------------------- commands
    def command(self, text):
        co = self.co
        t = text.replace(" ", "").lower()
        co.log("CEO", "ได้รับคำสั่งจาก LINE: %s" % text[:80])
        if t in ("ช่วยเหลือ", "help", "เมนู", "คำสั่ง", "?"):
            return HELP
        if t in ("แผนก", "ทุกแผนก", "ฝ่าย", "การทำงาน", "งาน"):
            return self.depts_text()
        d = dept_of(t)
        if d:
            return self.dept_text(*d)
        if t in ("ไม่อนุมัติ", "เหตุผล", "ปฏิเสธ", "ทำไมไม่ซื้อ", "ไม่ซื้อ"):
            return self.rejections_text()
        if t in ("คาดการณ์", "พยากรณ์", "forecast", "คาดการ"):
            return self.forecast_text()
        if t in ("ตรวจไลน์", "ซ่อมไลน์", "ตรวจline", "doctor"):
            return "\n".join(("✅ " if ok else "❌ ") + m for ok, m in doctor(self, fix=True))
        if t in ("แดชบอร์ด", "dashboard", "เว็บ", "ลิงก์"):
            return self.dashboard_text()
        if t in ("สถานะ", "status", "พอร์ต", "พอร์ท"):
            return self.status_text()
        if t in ("ไม้ที่ถือ", "ถืออะไร", "ถืออยู่", "positions"):
            return self.positions_text()
        if t in ("สรุปวันนี้", "สรุป", "วันนี้", "report"):
            return self.today_text()
        if t in ("หยุด", "หยุดเปิดไม้", "หยุดเปิดไม้ใหม่", "stop"):
            open("STOP", "w").close()
            co.log("CEO", "หยุดเปิดไม้ใหม่ (สั่งจาก LINE)")
            return "⏸ หยุดเปิดไม้ใหม่แล้ว\nไม้ที่ถืออยู่ยังมีตัดขาดทุน/ทำกำไรคุมเหมือนเดิม\nพิมพ์ \"เริ่ม\" เพื่อกลับมาเทรดต่อ"
        if t in ("เริ่ม", "เริ่มต่อ", "start", "resume"):
            if os.path.exists("STOP"):
                os.remove("STOP")
            co.log("CEO", "กลับมาเปิดไม้ใหม่ (สั่งจาก LINE)")
            return "▶️ กลับมาเปิดไม้ใหม่แล้ว บอทจะสแกนรอบถัดไปตามปกติ"
        if t.startswith("ยืนยัน"):
            return self.confirm(t)
        if t.startswith("ขาย"):
            return self.ask_sell(t[3:])
        return self.ask_ai(text)

    def dashboard_text(self):
        from richmenu import dashboard_url
        url = dashboard_url(strict=False)
        if not url:
            return "หาลิงก์แดชบอร์ดไม่เจอ: ตั้ง DASHBOARD_URL ใน /root/bot.env"
        return "📊 แดชบอร์ด\n%s\n\n(ตั้ง VPN On Demand ในแอป Tailscale ไว้ จะเปิดได้ทันทีไม่ต้องกดเปิด Tailscale)" % url

    def depts_text(self):
        act = self.co.acct.dept_activity()
        lines = ["🏢 งานล่าสุดของแต่ละแผนก (แตะปุ่มด้านล่างเพื่อดูละเอียด)"]
        for icon, name, _ in DEPTS:
            a = act.get(name)
            if not a:
                lines.append("\n⚪ %s %s\nยังไม่ทำงานวันนี้" % (icon, name))
                continue
            fresh = _ago(a["t"])
            lines.append("\n%s %s %s · วันนี้ %d งาน · %s\n%s" % (
                "🟢" if "นาที" in fresh and int(fresh.split()[0]) < 20 or fresh == "เมื่อสักครู่" else "🟡",
                icon, name, a["today"], fresh, a["last"][:160]))
        return "\n".join(lines), [SHORT[n] for _, n, _ in DEPTS] + ["ไม่อนุมัติ", "สถานะ"]

    def dept_text(self, icon, name):
        rows = self.co.acct.dept_logs(name, 12)
        if not rows:
            return "%s %s ยังไม่มีงาน" % (icon, name)
        out = ["%s %s · งานล่าสุด" % (icon, name)]
        for r in rows:
            out.append("%s %s" % (r["t"][11:16], r["msg"][:300]))
        text = "\n".join(out)
        return text[:4800], [SHORT[n] for _, n, _ in DEPTS if n != name] + ["แผนก", "สถานะ"]

    def rejections_text(self):
        rows = self.co.acct.recent_decisions(15, only_rejected=True)
        if not rows:
            return "ยังไม่มีหุ้นที่ถูกปฏิเสธ"
        out = ["🚫 หุ้นที่ไม่ซื้อล่าสุด + เหตุผล"]
        for r in rows:
            out.append("\n%s %s · %s\n%s" % (r["t"][11:16], r["symbol"], r["dept"], r["reason"][:250]))
        return "\n".join(out)[:4800]

    def forecast_text(self):
        f = self.co.forecast()
        q = self.co.ex.quote
        if not f["positions"]:
            return "ยังไม่มีไม้ที่ถืออยู่ ไม่มีอะไรให้คาดการณ์\nสถิติที่ผ่านมา: ชนะ %.0f%% จาก %d ไม้" % (f["win_rate"], f["trades"])
        out = ["📐 คาดการณ์ไม้ที่ถือ (ประเมินจากสถิติ ไม่ใช่การรับประกัน)"]
        for r in f["positions"]:
            out.append("\n%s ตอนนี้ %+.2f\n  ถึงเป้า %+.2f · โดนตัด %+.2f\n  โอกาสถึงเป้า ~%.0f%% → คาดหวัง %+.2f" % (
                r["symbol"], r["pnl_now"], r["if_tp"], r["if_sl"], r["p_win"] * 100, r["expected"]))
        out.append("\nรวมทุกไม้: คาดหวัง %+.2f %s\nดีสุด (ถึงเป้าหมด) %+.2f · แย่สุด (โดนตัดหมด) %+.2f" % (
            f["expected"], q, f["best"], f["worst"]))
        return "\n".join(out)

    def health_text(self):
        """สถานะระบบ: บอทออนไลน์ไหม แต่ละส่วนทำงานไหม"""
        co = self.co
        from departments import RISK_PROFILES as RP

        def dur(sec):
            sec = int(sec)
            if sec < 60:
                return "%d วินาที" % sec
            if sec < 3600:
                return "%d นาที" % (sec // 60)
            if sec < 86400:
                return "%d ชม. %d นาที" % (sec // 3600, sec % 3600 // 60)
            return "%d วัน %d ชม." % (sec // 86400, sec % 86400 // 3600)
        loop_age = time.time() - co.last_ok
        ok = loop_age < 300
        lines = ["%s ระบบ%s · เปิดต่อเนื่อง %s" % ("🟢" if ok else "🔴", "ออนไลน์" if ok else "ค้าง/มีปัญหา",
                                                dur(time.time() - co.started_at))]
        lines.append("• ลูปหลัก: ทำงานล่าสุด %sที่แล้ว" % dur(loop_age))
        if co.last_scan:
            nxt = co.last_scan + RP[co.risk]["every_min"] * 60
            lines.append("• สแกนตลาดล่าสุด %s · รอบถัดไป ~%s" % (
                th_hm(co.last_scan), th_hm(nxt)))
        else:
            lines.append("• สแกนตลาด: กำลังเริ่มรอบแรก")
        alive = {n: t.is_alive() for n, (t, _) in co.threads.items()}
        lines.append("• " + " · ".join("%s %s" % (n, "✅" if a else "❌") for n, a in alive.items()))
        lines.append("• AI: %s" % ("✅ " + co.ai.model.split("-")[1].title() if co.ai.enabled else "ปิดอยู่"))
        lines.append("• แจ้งเตือนเมื่อ VPS ดับ: %s" % ("✅ ตั้งแล้ว" if os.environ.get("HEALTHCHECK_URL") else "ยังไม่ตั้ง (manage.sh ข้อ 14)"))
        if co.downtime_note:
            lines.append("• ⚠️ ครั้งล่าสุด: " + co.downtime_note)
        return "\n".join(lines)

    def status_text(self):
        return self.health_text() + "\n\n" + self.port_text()

    def port_text(self):
        co = self.co
        s = co.acct.summary()
        eq = co.equity_cached()
        q = co.ex.quote
        return ("📊 พอร์ต\nกระดาน: %s · %s\nความเสี่ยง: %s · ตลาด: %s\nมูลค่าพอร์ต: %.2f %s\nเงินสด: %.2f %s\n"
                "กำไรที่ปิดแล้ว: %+.2f\nชนะ %d/%d ไม้ (%.0f%%) · ถืออยู่ %d ไม้%s") % (
            "Webull Thailand", {"live": "เงินจริง", "paper": "เงินจำลอง"}.get(co.mode, co.mode),
            RISK_PROFILES[co.risk]["name"], co.regime, eq, q, co.ex.usdt_free(), q, s["realized_pnl"],
            s["wins"], s["trades"], s["win_rate"], s["open"],
            "\n⏸ ตอนนี้หยุดเปิดไม้ใหม่อยู่" if os.path.exists("STOP") else "")

    def positions_text(self):
        co = self.co
        pos = co.acct.open_positions()
        if not pos:
            return "ยังไม่มีไม้ที่ถืออยู่"
        px = co.ex.prices([p["symbol"] for p in pos])
        lines = ["📈 ไม้ที่ถืออยู่"]
        for p in pos:
            now = px.get(p["symbol"], p["entry"])
            pct = (now / p["entry"] - 1) * 100
            lines.append("%s %s\n  ซื้อ %s → ตอนนี้ %s (%+.2f%%)\n  ตัดขาดทุน %s · ทำกำไร %s" % (
                "🟢" if pct >= 0 else "🔴", p["symbol"], _n(p["entry"]), _n(now), pct, _n(p["sl"]), _n(p["tp"])))
        return "\n".join(lines)

    def today_text(self):
        co = self.co
        today = now_th().date().isoformat()
        closed = [p for p in co.acct.closed_positions(500) if (p.get("closed") or "").startswith(today)]
        opened = [p for p in co.acct.open_positions() if (p.get("opened") or "").startswith(today)]
        pnl = sum(p["pnl"] for p in closed)
        u = co.ai.budget_status()[0] if hasattr(co.ai, "budget_status") else {"day_usd": 0, "day_calls": 0}
        lines = ["🧾 สรุปวันนี้ %s" % today, "ปิดไม้แล้ว %d ไม้ · กำไร/ขาดทุน %+.2f %s" % (len(closed), pnl, co.ex.quote),
                 "เปิดไม้ใหม่วันนี้ %d ไม้" % len(opened)]
        for p in closed[:8]:
            lines.append("• %s %s %+.2f" % (p["symbol"], p["reason"], p["pnl"]))
        lines.append("ค่า AI วันนี้ $%.4f (%d ครั้ง)" % (u.get("day_usd", 0), u.get("day_calls", 0)))
        return "\n".join(lines)

    # ---------------------------------------------------------------- sell (ต้องยืนยัน)
    def ask_sell(self, arg):
        pos = self.co.acct.open_positions()
        if not pos:
            return "ไม่มีไม้ที่ถืออยู่ให้ขาย"
        if arg in ("หมด", "ทั้งหมด", "all"):
            syms = [p["symbol"] for p in pos]
            key = "ยืนยันขายหมด"
        else:
            coin = arg.upper()
            syms = [p["symbol"] for p in pos if p["symbol"] == coin or p["symbol"] == coin + self.co.ex.quote]
            if not syms:
                return "ไม่พบไม้ %s ที่ถืออยู่\nถืออยู่: %s" % (coin or "-", ", ".join(p["symbol"] for p in pos))
            key = "ยืนยันขาย" + syms[0].replace(self.co.ex.quote, "").lower()
        self.pending = {key: (time.time(), syms)}
        return "⚠️ จะขาย %s ทันทีที่ราคาตลาด\nถ้าแน่ใจ พิมพ์: %s\n(ภายใน 2 นาที)" % (", ".join(syms), key)

    def confirm(self, t):
        item = self.pending.pop(t, None)
        if not item or time.time() - item[0] > 120:
            return "ไม่มีคำสั่งที่รอยืนยัน หรือหมดเวลาแล้ว พิมพ์คำสั่งขายใหม่อีกครั้ง"
        self.co.sell_queue.extend(item[1])
        self.co.log("CEO", "รับคำสั่งขายจาก LINE: %s" % ", ".join(item[1]))
        return "✅ รับคำสั่งแล้ว บอทจะขาย %s ภายในไม่เกิน 1 นาที แล้วแจ้งผลใน LINE" % ", ".join(item[1])

    # ---------------------------------------------------------------- AI
    def ask_ai(self, question):
        ai = self.co.ai
        if not ai.enabled:
            return "🧠 AI ปิดอยู่ หรือยังไม่ได้ใส่ Claude API key\n\n" + HELP
        if ai.budget_status()[2]:
            return "🧠 วันนี้/เดือนนี้ใช้งบ AI ครบเพดานแล้ว ปรับได้ในแดชบอร์ด\n\n" + HELP
        logs = self.co.acct.recent_logs(25)
        ctx = "%s\n\n%s\n\nบันทึกล่าสุด:\n%s" % (
            self.port_text(), self.positions_text(),
            "\n".join("%s %s: %s" % (l["t"][11:16], l["dept"], l["msg"]) for l in reversed(logs)))
        prompt = ("คุณคือผู้ช่วยของบอทเทรดหุ้นสหรัฐฯอัตโนมัติ ตอบเจ้าของผ่าน LINE เป็นภาษาไทย สั้น กระชับ อ่านง่ายบนมือถือ "
                  "ใช้ข้อมูลจริงด้านล่างเท่านั้น ห้ามรับประกันกำไร ห้ามแนะนำเพิ่มความเสี่ยงหรือใช้เลเวอเรจ "
                  "คุณสั่งซื้อขายเองไม่ได้ ถ้าเจ้าของอยากให้ทำอะไร ให้บอกคำสั่งที่พิมพ์ได้: "
                  "สถานะ, แผนก, ไม่อนุมัติ, คาดการณ์, ไม้ที่ถือ, สรุปวันนี้, หยุด, เริ่ม, ขาย <หุ้น>, ขายหมด\n\n"
                  "ข้อมูลตอนนี้:\n%s\n\nคำถาม: %s") % (ctx, question[:1000])
        try:
            return "🧠 " + ai._call(prompt, max_tokens=600, purpose="ตอบคำถามใน LINE").strip()
        except Exception as e:
            return "🧠 ถาม AI ไม่สำเร็จ: %s" % e


# ====================================================================== ตรวจ/ซ่อมการเชื่อมต่อ LINE อัตโนมัติ
def _line_api(bot, method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + bot.token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError("LINE ตอบ HTTP %s: %s" % (e.code, e.read().decode(errors="replace")[:200]))


def _ts(*args, timeout=25):
    return subprocess.run(["tailscale"] + list(args), capture_output=True, text=True, timeout=timeout)


HOOK_PATH = "/webull/line/webhook"  # path ของบอทหุ้นเอง ไม่ทับ /line/webhook ของบอทคริปโตบนเครื่องเดียวกัน


def shared_with_crypto(token):
    """ใช้ LINE OA เดียวกับบอทคริปโตไหม (LINE ตั้ง webhook ได้ทีละ 1 ที่ ใช้ร่วมกันจะแย่งกันรับคำสั่ง)"""
    try:
        for line in open("/root/bot.env", encoding="utf-8"):
            if line.startswith("LINE_CHANNEL_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip() == token
    except OSError:
        pass
    return False


def _funnel_ok(port, path=HOOK_PATH):
    try:
        st = json.loads(_ts("funnel", "status", "--json").stdout or "{}")
    except Exception:
        return False
    if not any(st.get("AllowFunnel", {}).values()):
        return False
    for web in (st.get("Web") or {}).values():
        own = ((web.get("Handlers") or {}).get(path) or {}).get("Proxy", "")
        if own:
            return (":%d" % port) in own
    return False


def doctor(bot, fix=True):
    """ไล่ตรวจทุกจุดที่ทำให้ LINE กดแล้วบอทเงียบ แล้วซ่อมที่ซ่อมเองได้ คืนรายการ (ผ่านไหม, ข้อความ)"""
    out, port = [], bot.port
    # 1) ตัวรับ webhook ในเครื่องยังทำงานไหม
    try:
        body = b'{"events":[]}'
        sig = base64.b64encode(hmac.new(bot.secret.encode(), body, hashlib.sha256).digest()).decode()
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/line/webhook" % port, data=body, method="POST",
                               headers={"X-Line-Signature": sig, "Content-Type": "application/json"}), timeout=5).read()
        out.append((True, "ตัวรับข้อความ LINE ในเครื่องทำงาน (พอร์ต %d)" % port))
    except Exception as e:
        out.append((False, "ตัวรับข้อความ LINE ในเครื่องไม่ตอบ: %s" % e))
    # 2) ชื่อเครื่องบน Tailscale
    try:
        st = json.loads(_ts("status", "--json").stdout)
        host = (st.get("Self", {}).get("DNSName") or "").rstrip(".")
    except Exception as e:
        out.append((False, "เรียก tailscale ไม่ได้: %s" % e))
        return out
    if not host:
        out.append((False, "ยังไม่ได้เปิด MagicDNS/HTTPS ใน Tailscale (admin console → DNS)"))
        return out
    expected = "https://%s%s" % (host, HOOK_PATH)
    if shared_with_crypto(bot.token):
        out.append((False, "บอทหุ้นใช้ LINE OA เดียวกับบอทคริปโต: ส่งแจ้งเตือนได้ แต่รับคำสั่งในแชทไม่ได้ "
                           "(ถ้าตั้ง webhook มาที่บอทหุ้น บอทคริปโตจะใช้ปุ่มไม่ได้) สร้าง LINE OA ใหม่ให้บอทหุ้นแล้วใส่ที่ manage.sh ข้อ 11 และ 13"))
        return out
    # 3) Funnel (ทางเข้าจากอินเทอร์เน็ตให้ LINE ส่งข้อความเข้ามา)
    if _funnel_ok(port):
        out.append((True, "Tailscale Funnel เปิดอยู่ → พอร์ต %d" % port))
    elif fix:
        target = "http://127.0.0.1:%d%s" % (port, HOOK_PATH)
        r = _ts("funnel", "--bg", "--yes", "--set-path=" + HOOK_PATH, target)
        if r.returncode != 0 and "yes" in (r.stderr or ""):
            r = _ts("funnel", "--bg", "--set-path=" + HOOK_PATH, target)
        txt = (r.stdout or "") + (r.stderr or "")
        if _funnel_ok(port):
            out.append((True, "Funnel หลุดอยู่ เปิดใหม่ให้แล้ว → พอร์ต %d" % port))
        elif "login.tailscale.com" in txt or "https://" in txt:
            link = [w for w in txt.split() if w.startswith("https://")]
            out.append((False, "ต้องกดอนุญาต Funnel ครั้งเดียว เปิดลิงก์นี้: %s" % (link[0] if link else txt[:200])))
        else:
            out.append((False, "เปิด Funnel ไม่สำเร็จ: %s" % txt.strip()[:200]))
    else:
        out.append((False, "Tailscale Funnel ไม่ได้เปิด"))
    # 4) Webhook URL ใน LINE ตรงกับเครื่องนี้ไหม (แก้ให้อัตโนมัติผ่าน API)
    try:
        info = _line_api(bot, "GET", "/channel/webhook/endpoint")
        if info.get("endpoint") != expected:
            if fix:
                _line_api(bot, "PUT", "/channel/webhook/endpoint", {"endpoint": expected})
                out.append((True, "แก้ Webhook URL ใน LINE เป็น %s แล้ว (เดิม %s)" % (expected, info.get("endpoint") or "ว่าง")))
            else:
                out.append((False, "Webhook URL ไม่ตรง: %s" % info.get("endpoint")))
        else:
            out.append((True, "Webhook URL ถูกต้อง"))
        if not info.get("active"):
            out.append((False, "สวิตช์ Use webhook ใน LINE ปิดอยู่: เข้า developers.line.biz → channel → Messaging API → เปิด Use webhook"))
        # 5) ให้ LINE ลองยิงจริงจากอินเทอร์เน็ต
        t = _line_api(bot, "POST", "/channel/webhook/test", {"endpoint": expected})
        last = getattr(bot, "last_webhook", 0)
        try:
            last = max(last, float(open(LAST_HOOK).read()))
        except Exception:
            pass
        bot.last_webhook = last
        recent = time.time() - last < 6 * 3600
        if t.get("success"):
            out.append((True, "LINE ทดสอบส่งเข้าบอทสำเร็จ"))
        elif recent:  # ปุ่มทดสอบของ LINE บางครั้งต่อ HTTPS ของ Funnel ไม่ติด แต่ข้อความจริงเข้าได้ ไม่ต้องเตือน
            out.append((True, "ข้อความจริงจากแชทเข้าบอทได้ปกติ (ล่าสุด %s) ปุ่มทดสอบของ LINE ไม่ผ่านไม่เป็นไร" %
                        th_hm(bot.last_webhook)))
        else:
            out.append((False, "LINE ส่งเข้าบอทไม่ได้: %s %s %s (ลองกดปุ่มในแชท ถ้าบอทตอบ = ใช้ได้)" % (
                t.get("statusCode", ""), t.get("reason", ""), t.get("detail", ""))))
    except Exception as e:
        out.append((False, "ตรวจกับ LINE ไม่ได้: %s" % e))
    return out


def doctor_loop(bot):
    """ตรวจตอนเปิดบอท แล้วทุก 30 นาที ถ้าพังแล้วซ่อมเองไม่ได้ แจ้งเจ้าของทาง LINE (ไม่เกิน 6 ชม.ครั้ง)"""
    time.sleep(20)
    while True:
        try:
            res = doctor(bot, fix=True)
            bad = [m for ok, m in res if not ok]
            fixed = [m for ok, m in res if ok and ("ให้แล้ว" in m or "แก้ " in m)]
            bot.health = {"t": now_th().isoformat(timespec="seconds"), "ok": not bad, "items": res}
            for m in fixed + bad:
                bot.co.log("CEO", "LINE: " + m)
            if bad:
                bot.co.line.send("⚠️ ปุ่มในแชท LINE อาจกดแล้วบอทไม่ตอบ\n" + "\n".join("• " + m for m in bad),
                                 key="line_doctor", every_sec=6 * 3600, urgent=True)
        except Exception as e:
            bot.co.log("CEO", "ตรวจ LINE ไม่สำเร็จ: %s" % e)
        time.sleep(1800)


def status_bar_loop(bot, every_min):
    """อัปเดตข้อความบนแถบริชเมนู (ใต้แชท) เป็นสถานะบอท เช่น '🟢17:40 +1.2%' / '🔴17:40 -1.7%' (ไม่กินโควตาข้อความ)
    LINE ให้ใส่แค่ตัวหนังสือ เปลี่ยนสีตัวอักษรไม่ได้ จึงใช้วงกลมเขียว/แดงบอกกำไร/ขาดทุนวันนี้
    ถ้าเวลาบนแถบเก่ากว่ารอบอัปเดตมาก แปลว่าบอทหยุดทำงาน"""
    import richmenu
    time.sleep(60)
    last_bar, wait = None, every_min * 60
    while True:
        try:
            co = bot.co
            eq = co.equity_cached()
            day0 = co.acct.get("day_start_" + now_th().date().isoformat()) or eq
            chg = (eq / day0 - 1) * 100 if day0 else 0
            icon = "⏸" if os.path.exists("STOP") else ("🔴" if chg < -0.05 else "🟢")
            bar = "%s%s %+.1f%%" % (icon, now_th().strftime("%H:%M"), chg)
            if bar != last_bar:
                richmenu.set_bar(bot.token, bar)
                last_bar, wait = bar, every_min * 60
                if not getattr(bot, "bar_ok", False):
                    bot.bar_ok = True
                    bot.co.log("CEO", "อัปเดตแถบใต้แชท LINE เป็น '%s' แล้ว (ปิดแชทแล้วเปิดใหม่ถึงจะเห็น)" % bar)
        except BaseException as e:  # richmenu error ห้ามให้ thread ตาย
            if "429" in str(e):  # LINE บอกว่าถี่เกิน: ชะลอลงเอง
                wait = min(wait * 2, 3600)
            bot.co.log("CEO", "อัปเดตแถบสถานะริชเมนูไม่ได้: %s" % e)
        time.sleep(wait)


def reset_bar(bot):
    """ปิดแถบสถานะแล้ว: คืนข้อความใต้แชทเป็น 'เมนูบอทเทรด' (ทำครั้งเดียว ถ้ายังเป็นข้อความสถานะเก่าค้างอยู่)"""
    import richmenu
    time.sleep(30)
    try:
        menus = richmenu.call("GET", richmenu.API + "/richmenu/list", bot.token).get("richmenus", [])
        if any(m.get("name") == richmenu.NAME and m.get("chatBarText") != "เมนูบอทเทรด" for m in menus):
            richmenu.set_bar(bot.token, "เมนูบอทเทรด")
            bot.co.log("CEO", "คืนแถบใต้แชท LINE เป็น 'เมนูบอทเทรด' แล้ว")
    except BaseException as e:
        bot.co.log("CEO", "คืนแถบใต้แชท LINE ไม่ได้: %s" % e)


def serve(company, port=8091):
    bot = LineBot(company)
    bot.port = port
    bot.health = None
    bot.last_webhook = 0
    company.line_bot = bot
    if not bot.ready:
        return None

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self, code=200):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._ok(404)

        def _read_body(self):
            """รองรับทั้ง Content-Length และ chunked (บาง proxy ส่งแบบ chunked)"""
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                out = b""
                while len(out) < 1_000_000:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    out += self.rfile.read(size)
                    self.rfile.readline()
                return out
            n = min(int(self.headers.get("Content-Length", 0) or 0), 1_000_000)
            return self.rfile.read(n) if n else b""

        def do_POST(self):
            p = self.path.split("?")[0].rstrip("/")
            if p not in ("", "/line/webhook", "/webhook") and not p.endswith("/webhook"):
                return self._ok(404)
            body = self._read_body()
            sig = self.headers.get("X-Line-Signature")
            if not bot.verify(body, sig):
                now = time.time()
                if now - bot.__dict__.get("_last_bad", 0) > 60:  # log ไม่เกินนาทีละครั้ง
                    bot._last_bad = now
                    company.log("CEO", "ปฏิเสธ webhook: ลายเซ็นไม่ตรง (มีลายเซ็น=%s, ข้อมูล %d ไบต์, secret ยาว %d ตัว%s)" % (
                        "ใช่" if sig else "ไม่", len(body), len(bot.secret),
                        "" if len(bot.secret) == 32 else " ⚠️ ปกติ Channel secret ยาว 32 ตัว น่าจะใส่ผิดตัว"))
                return self._ok(401)
            self._ok(200)
            try:
                bot.handle_events(json.loads(body or b"{}"))
            except Exception as e:
                company.log("CEO", "อ่าน webhook LINE ไม่ได้: %s" % e)

    if shared_with_crypto(bot.token):
        company.log("CEO", "ใช้ LINE OA เดียวกับบอทคริปโต: ส่งแจ้งเตือนเข้า LINE ได้ แต่ไม่รับคำสั่งในแชท "
                           "(กันแย่ง webhook/ริชเมนูกับบอทคริปโต) อยากสั่งงานในแชท ให้สร้าง LINE OA ใหม่ให้บอทหุ้น")
        return None
    company.log("CEO", "เปิดรับคำสั่งจาก LINE ที่พอร์ต %d" % port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    company.keep_thread("รับคำสั่ง LINE", srv.serve_forever)
    threading.Thread(target=doctor_loop, args=(bot,), daemon=True).start()
    every = int(company.cfg.get("line_status_bar_min", 0) or 0)
    if every > 0:
        threading.Thread(target=status_bar_loop, args=(bot, max(1, every)), daemon=True).start()
    else:
        threading.Thread(target=reset_bar, args=(bot,), daemon=True).start()
    return bot


if __name__ == "__main__":  # python3 line_bot.py  = ตรวจ/ซ่อมการเชื่อมต่อ LINE (manage.sh ข้อ 17)
    b = LineBot.__new__(LineBot)
    b.token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    b.secret = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
    b.port = 8091
    if not (b.token and b.secret):
        raise SystemExit("❌ ยังไม่ได้ตั้ง LINE token / Channel secret (manage.sh ข้อ 11 และ 13)")
    for ok, m in doctor(b, fix=True):
        print(("✅ " if ok else "❌ ") + m)
