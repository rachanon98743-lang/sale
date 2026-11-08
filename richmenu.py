#!/usr/bin/env python3
"""richmenu.py — ติดตั้งริชเมนู 6 ปุ่ม + แถบเปิดแดชบอร์ด ให้ LINE OA (รันซ้ำเพื่อแทนของเดิม)

    python3 richmenu.py          # ใช้ LINE_CHANNEL_ACCESS_TOKEN จาก environment
ลิงก์แดชบอร์ด: ใช้ DASHBOARD_URL ถ้าตั้งไว้ ไม่งั้นใช้ IP Tailscale ของ VPS พอร์ต 8081
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"
NAME = "kiki-bot-menu"
W, H = 2500, 1686
BUTTONS = ["สถานะ", "ไม้ที่ถือ", "สรุปวันนี้", "หยุด", "เริ่ม", "ช่วยเหลือ"]


def call(method, url, token, body=None, ctype="application/json"):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError("LINE ตอบ HTTP %s: %s" % (e.code, e.read().decode(errors="replace")[:300]))


def dashboard_url(strict=True):
    """ลิงก์แดชบอร์ด: DASHBOARD_URL > ชื่อ MagicDNS (*.ts.net ให้ iPhone เปิด Tailscale อัตโนมัติได้) > IP Tailscale"""
    url = os.environ.get("DASHBOARD_URL", "").strip()
    if url:
        return url
    try:
        st = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                                       timeout=10).stdout)
        name = (st.get("Self", {}).get("DNSName") or "").rstrip(".")
        if name:
            return "http://%s:8081" % name
        return "http://%s:8081" % st["Self"]["TailscaleIPs"][0]
    except Exception:
        if strict:
            sys.exit("❌ หาที่อยู่ Tailscale ไม่เจอ: ตั้ง DASHBOARD_URL ใน /root/bot.env แล้วรันใหม่")
        return None


def install(token, url, bar="เมนูบอทเทรด"):
    """สร้างริชเมนูใหม่ (ข้อความบนแถบ = bar) ตั้งเป็นค่าเริ่มต้น แล้วค่อยลบของเก่า (ไม่มีช่วงที่เมนูหาย)"""
    old = [m["richMenuId"] for m in call("GET", API + "/richmenu/list", token).get("richmenus", []) if m.get("name") == NAME]
    cw, ch = W // 3, 620  # 6 ปุ่มบน 2 แถว + แถบเปิดแดชบอร์ดด้านล่าง (ต้องตรงกับรูป richmenu.png)
    areas = [{"bounds": {"x": (i % 3) * cw, "y": (i // 3) * ch, "width": cw, "height": ch},
              "action": {"type": "message", "text": t}} for i, t in enumerate(BUTTONS)]
    areas.append({"bounds": {"x": 0, "y": 2 * ch, "width": W, "height": H - 2 * ch},
                  "action": {"type": "uri", "label": "แดชบอร์ด", "uri": url}})
    menu = {"size": {"width": W, "height": H}, "selected": True, "name": NAME,
            "chatBarText": bar[:14], "areas": areas}
    rid = call("POST", API + "/richmenu", token, menu)["richMenuId"]
    with open(os.path.join(HERE, "richmenu.png"), "rb") as f:
        call("POST", DATA_API + "/richmenu/%s/content" % rid, token, f.read(), "image/png")
    call("POST", API + "/user/all/richmenu/" + rid, token)
    for o in old:
        try:
            call("DELETE", API + "/richmenu/" + o, token)
        except RuntimeError:
            pass
    return rid


def set_bar(token, text):
    """เปลี่ยนข้อความบนแถบเมนูใต้แชทเป็นสถานะบอท (เรียกจากบอททุก ~15 นาที)"""
    url = dashboard_url(strict=False)
    if not url:
        raise RuntimeError("หาลิงก์แดชบอร์ดไม่เจอ")
    return install(token, url, text)


def main():
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        sys.exit("❌ ยังไม่ได้ตั้ง LINE_CHANNEL_ACCESS_TOKEN (manage.sh ข้อ 11)")
    url = dashboard_url()
    try:
        install(token, url)
    except RuntimeError as e:
        sys.exit("❌ %s" % e)
    print("✅ ติดตั้งริชเมนูแล้ว ปิดแชท OA แล้วเปิดใหม่จะเห็นเมนูด้านล่าง")
    print("   ปุ่มแดชบอร์ดเปิด: %s (ต้องเปิดแอป Tailscale ในมือถือก่อน)" % url)


if __name__ == "__main__":
    main()
