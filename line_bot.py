"""line_bot.py — สั่งงานบอทผ่านแชท LINE OA (รับ webhook จาก LINE แล้วตอบกลับ)

ต้องตั้งใน /root/bot.env:
  LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID   (ใช้ร่วมกับระบบแจ้งเตือน)
  LINE_CHANNEL_SECRET                       (Basic settings → Channel secret ใช้ตรวจว่าข้อความมาจาก LINE จริง)

เปิดให้ LINE ส่ง webhook เข้ามาได้ด้วย Tailscale Funnel (HTTPS ฟรี):  tailscale funnel --bg 8090
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
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from departments import RISK_PROFILES, now_th

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
HELP = """📋 คำสั่งที่ใช้ได้
• สถานะ – มูลค่าพอร์ต กำไร โหมด
• แดชบอร์ด – ลิงก์เปิดหน้าแดชบอร์ด (เปิด Tailscale ก่อน)
• ไม้ที่ถือ – เหรียญที่ถืออยู่ + กำไร/ขาดทุนตอนนี้
• สรุปวันนี้ – ซื้อขายวันนี้ + ค่า AI
• หยุด – หยุดเปิดไม้ใหม่ (ไม้เดิมยังเฝ้าอยู่)
• เริ่ม – กลับมาเปิดไม้ใหม่
• ขาย BTC – ขายเหรียญนั้น (ต้องยืนยัน)
• ขายหมด – ขายทุกไม้ (ต้องยืนยัน)
• พิมพ์คำถามอะไรก็ได้ – ให้ AI ตอบจากข้อมูลพอร์ตจริง"""


def _n(x):
    return "{:,.2f}".format(x) if x >= 100 else "%.6g" % x


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

    def reply(self, reply_token, text):
        body = json.dumps({"replyToken": reply_token,
                           "messages": [{"type": "text", "text": text[:4900]}]}).encode()
        req = urllib.request.Request(REPLY_URL, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            self.co.log("CEO", "ตอบ LINE ไม่สำเร็จ: %s" % e)

    def handle_events(self, payload):
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
        try:
            out = self.command(text)
        except Exception as e:
            out = "⚠️ ทำคำสั่งไม่สำเร็จ: %s" % e
        self.reply(token, out)

    # ---------------------------------------------------------------- commands
    def command(self, text):
        co = self.co
        t = text.replace(" ", "").lower()
        co.log("CEO", "ได้รับคำสั่งจาก LINE: %s" % text[:80])
        if t in ("ช่วยเหลือ", "help", "เมนู", "คำสั่ง", "?"):
            return HELP
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
        url = os.environ.get("DASHBOARD_URL", "").strip()
        if not url:
            try:
                import subprocess
                ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()[0]
                url = "http://%s:8080" % ip
            except Exception:
                return "หาลิงก์แดชบอร์ดไม่เจอ: ตั้ง DASHBOARD_URL ใน /root/bot.env"
        return "📊 แดชบอร์ด\n%s\n\nเปิดแอป Tailscale ในมือถือก่อน แล้วค่อยกดลิงก์" % url

    def status_text(self):
        co = self.co
        s = co.acct.summary()
        eq = co.equity()
        q = co.ex.quote
        return ("📊 สถานะ\nกระดาน: %s · %s\nความเสี่ยง: %s · ตลาด: %s\nมูลค่าพอร์ต: %.2f %s\nเงินสด: %.2f %s\n"
                "กำไรที่ปิดแล้ว: %+.2f\nชนะ %d/%d ไม้ (%.0f%%) · ถืออยู่ %d ไม้%s") % (
            "Binance TH" if co.ex.th else "Binance.com", {"live": "เงินจริง", "paper": "เงินจำลอง"}.get(co.mode, co.mode),
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
        if hasattr(ai, "budget_status") and ai.budget_status()[2]:
            return "🧠 วันนี้/เดือนนี้ใช้งบ AI ครบเพดานแล้ว ปรับได้ในแดชบอร์ด\n\n" + HELP
        logs = self.co.acct.recent_logs(15)
        ctx = "%s\n\n%s\n\nบันทึกล่าสุด:\n%s" % (
            self.status_text(), self.positions_text(),
            "\n".join("%s %s: %s" % (l["t"][11:16], l["dept"], l["msg"]) for l in reversed(logs)))
        prompt = ("คุณคือผู้ช่วยของบอทเทรดคริปโตอัตโนมัติ ตอบเจ้าของผ่าน LINE เป็นภาษาไทย สั้น กระชับ อ่านง่ายบนมือถือ "
                  "ใช้ข้อมูลจริงด้านล่างเท่านั้น ห้ามรับประกันกำไร ห้ามแนะนำเพิ่มความเสี่ยงหรือใช้เลเวอเรจ "
                  "คุณสั่งซื้อขายเองไม่ได้ ถ้าเจ้าของอยากให้ทำอะไร ให้บอกคำสั่งที่พิมพ์ได้: "
                  "สถานะ, ไม้ที่ถือ, สรุปวันนี้, หยุด, เริ่ม, ขาย <เหรียญ>, ขายหมด\n\n"
                  "ข้อมูลตอนนี้:\n%s\n\nคำถาม: %s") % (ctx, question[:1000])
        try:
            return "🧠 " + ai._call(prompt, max_tokens=600, purpose="ตอบคำถามใน LINE").strip()
        except Exception as e:
            return "🧠 ถาม AI ไม่สำเร็จ: %s" % e


def serve(company, port=8090):
    bot = LineBot(company)
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
            if self.path.rstrip("/") not in ("/line/webhook", "/webhook"):
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

    company.log("CEO", "เปิดรับคำสั่งจาก LINE ที่พอร์ต %d" % port)
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever, daemon=True).start()
    return bot
