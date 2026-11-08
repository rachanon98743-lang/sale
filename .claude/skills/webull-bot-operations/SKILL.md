---
name: webull-bot-operations
description: ดูแล ติดตั้ง อัปเดต และแก้ปัญหาบอทหุ้น Webull บน VPS (branch webull-bot, service webullbot, พอร์ต 8081) — ใช้เมื่อผู้ใช้สั่งติดตั้ง เปลี่ยนโหมด/ความเสี่ยง ใส่ key หรือบอทมีปัญหา
---

# คู่มือดูแลบอทหุ้น Webull

ผู้ใช้ทำงานจากมือถืออย่างเดียว ให้คำสั่งที่ copy-paste ได้ทีละบรรทัด และอธิบายเป็นภาษาไทยง่ายๆ

## ที่อยู่ของทุกอย่าง (แยกจากบอทคริปโตทั้งหมด)
| | บอทหุ้น Webull | บอทคริปโต |
|---|---|---|
| branch | `webull-bot` | `kiki-bot` |
| โฟลเดอร์ | `/root/webull_trading_company` | `/root/ai_trading_company` |
| ไฟล์ key | `/root/webull.env` | `/root/bot.env` |
| service | `webullbot` | `tradingbot` |
| แดชบอร์ด | พอร์ต 8081 | พอร์ต 8080 |

## คำสั่งหลัก
- ติดตั้ง/อัปเดต: `curl -fsSL https://raw.githubusercontent.com/rachanon98743-lang/sale/webull-bot/install.sh | sudo bash`
- เมนู: `bash /root/webull_trading_company/manage.sh`
- log: `journalctl -u webullbot -n 50 --no-pager`

## ลำดับเปิดใช้เงินจริง (ห้ามข้าม)
1. ขอ App Key ที่ webull.co.th/en/open-api → manage.sh ข้อ 1 (ครั้งแรกกดยืนยัน 2FA ในแอป Webull)
2. ข้อ 9 ตรวจการเชื่อมต่อ → ข้อ 10 ให้ Webull ตรวจคำสั่งทดลอง (preview)
3. รันโหมด paper อย่างน้อย 2-4 สัปดาห์ ดูผลด้วย skill trade-journal-review
4. แลกเงินเป็น USD ในแอป → ข้อ 3 เลือก live + ตั้งเพดานทุนเล็กๆ ก่อน

## ปัญหาที่พบบ่อย
- 401/403: key ผิด / ยังไม่อนุมัติ OpenAPI / ยังไม่กดยืนยัน 2FA → ลบโฟลเดอร์ `conf/` แล้วรันข้อ 9 ใหม่
- ซื้อไม่ได้ "เงินไม่พอ": บัญชีเงินสดต้องรอเงิน settle (T+1) หรือยังไม่มี USD
- ไม่ซื้อเลย: ดูเหตุผลใน LINE/แดชบอร์ด "ไม่อนุมัติ" — ตลาดปิด, ตลาดขาลง, ใกล้ประกาศงบ เป็นเรื่องปกติ
- LINE: ใช้ LINE OA เดียวกับบอทคริปโต (แจ้งเตือนอย่างเดียว) โควตา 300 ข้อความ/เดือนใช้ร่วมกัน

## ห้ามทำ
- ห้ามขอให้ผู้ใช้ส่ง App Secret, รหัสผ่าน, รหัสเทรด มาในแชท
- ห้ามสลับเป็น live หรือเพิ่มเพดานทุนเองโดยผู้ใช้ไม่ได้สั่งชัดเจน
