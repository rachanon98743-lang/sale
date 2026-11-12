#!/usr/bin/env bash
# เมนูจัดการบอทหุ้น Webull: bash /root/webull_trading_company/manage.sh
set -uo pipefail
DIR=/root/webull_trading_company
ENV=/root/webull.env
SVC_NAME=webullbot
SVC=/etc/systemd/system/$SVC_NAME.service
PY="$DIR/venv/bin/python"

setvar(){ touch "$ENV"; chmod 600 "$ENV"; grep -v "^$1=" "$ENV" > "$ENV.tmp" || true; echo "$1=$2" >> "$ENV.tmp"; mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"; }
pw_test(){ local code i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --data-urlencode "password=$1" http://127.0.0.1:8082/login 2>/dev/null)
    [ "$code" != 000 ] && break; sleep 1
  done
  case "$code" in
    303) echo "✅ หน้าสแกนหุ้นรับรหัสนี้แล้ว เข้าได้เลย (กดรีเฟรชก่อน)" ;;
    000) echo "❌ หน้าสแกนหุ้นยังไม่ทำงาน ดู log ข้อ 16" ;;
    *) echo "❌ หน้าสแกนหุ้นยังไม่รับรหัสนี้ (code $code) ส่งภาพหน้าจอนี้มาให้ดู" ;;
  esac; }
mode(){ grep -o -- '--mode [a-z]*' "$SVC" | awk '{print $2}'; }
run(){ ( set -a; . "$ENV"; set +a; cd "$DIR" && "$PY" main.py "$@" ); }
cfgset(){ "$PY" - "$DIR/config.json" "$1" "$2" <<'PY'
import json,sys
p,k,v=sys.argv[1:4]
c=json.load(open(p,encoding="utf-8"))
try: v=json.loads(v)
except ValueError: pass
c[k]=v
json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
PY
}

# เปิดอัปเดตอัตโนมัติให้เอง (ไม่ต้องกด 7) ยกเว้นเจ้าของสั่งปิดไว้ (ข้อ 21)
if [ -f "$DIR/auto_update.sh" ] && [ ! -f "$DIR/.no_autoupdate" ] && ! systemctl is-enabled -q $SVC_NAME-autoupdate.timer 2>/dev/null; then
  bash "$DIR/auto_update.sh" install
fi

while true; do
  echo
  echo "=== AI Stock Company (Webull) | โหมด: $(mode) | สถานะ: $(systemctl is-active $SVC_NAME) ==="
  echo " 1) ใส่ Webull App Key / App Secret"
  echo " 2) ใส่ Claude (Anthropic) API key"
  echo " 3) เปลี่ยนโหมด: paper / live"
  echo " 4) เปลี่ยนระดับความเสี่ยง: low / mid / high"
  echo " 5) ดู log ล่าสุด"
  echo " 6) รีสตาร์ทบอท"
  echo " 7) อัปเดตบอทเป็นเวอร์ชันล่าสุดทันที (ปกติอัปเดตเองทุก 5 นาทีอยู่แล้ว)"
  echo " 8) เปลี่ยนรหัสผ่านแดชบอร์ด"
  echo " 9) ตรวจการเชื่อมต่อ Webull และยอดเงิน (ไม่ซื้อขาย)"
  echo "10) ให้ Webull ตรวจคำสั่งซื้อทดลอง SPY \$5 (preview ไม่ซื้อจริง)"
  echo "11) ตั้งแจ้งเตือนเข้า LINE (ถ้ายังไม่ได้ใช้ของบอทคริปโต)"
  echo "12) ส่งข้อความทดสอบเข้า LINE"
  echo "13) ตั้งเพดานทุนเงินจริง (USD)"
  echo "14) ตั้งแจ้งเตือนเมื่อ VPS ดับ (healthchecks.io)"
  echo "--- ฝ่ายสแกนหุ้น + Gemini (ไม่ต้องใช้ Webull key) | สถานะ: $(systemctl is-active stockscan 2>/dev/null) ---"
  echo "15) สแกนหุ้นเดี๋ยวนี้ + ส่งเข้า LINE"
  echo "16) ดู log ฝ่ายสแกนหุ้น"
  echo "17) ใส่ Gemini API key"
  echo "18) ตั้งทุน (USD) ให้คำนวณขนาดไม้ / เวลาส่ง LINE"
  echo "19) ดูรหัสหน้าเว็บตอนนี้ + ทดสอบเข้าระบบ"
  echo "21) อัปเดตอัตโนมัติ: ดูสถานะ / เปิด / ปิด"
  echo " 0) ออก"
  read -r -p "เลือก: " c
  case "$c" in
    1) echo "ขอได้ที่ webull.co.th/en/open-api → Get API Key (ห้ามส่ง key ให้ใครในแชท)"
       read -r -p "App Key: " k; read -r -s -p "App Secret (พิมพ์แล้วไม่แสดง): " s; echo
       [ -n "$k" ] && [ -n "$s" ] || { echo "ยกเลิก"; continue; }
       setvar WEBULL_APP_KEY "$k"; setvar WEBULL_APP_SECRET "$s"
       echo "กำลังทดสอบ... ถ้าแอป Webull เด้งให้ยืนยันการเข้าสู่ระบบ ให้กดยืนยัน"
       run --check; systemctl restart $SVC_NAME; echo "บันทึกแล้ว" ;;
    2) echo "ขอได้ที่ console.anthropic.com → API Keys → Create Key (ขึ้นต้นด้วย sk-ant-)"
       echo "วิธีวาง: แตะค้างบนจอดำตรงบรรทัดนี้ → Paste (อย่าวางในช่อง Ask AI ด้านล่าง)"
       read -r -p "Anthropic API Key: " k; k=$(echo "$k" | tr -d '[:space:]')
       case "$k" in
         sk-ant-*) setvar ANTHROPIC_API_KEY "$k"; systemctl restart $SVC_NAME
                   echo "✅ บันทึกแล้ว (${k:0:10}...${k: -4})" ;;
         "") echo "ยกเลิก (ยังไม่ได้วางอะไร)" ;;
         *) echo "❌ ไม่ใช่ API key: key ต้องขึ้นต้นด้วย sk-ant- (ที่วางมาขึ้นต้นด้วย ${k:0:7}) ไม่ได้บันทึก" ;;
       esac ;;
    3) read -r -p "โหมด (paper/live): " m
       case "$m" in
         paper) ;;
         live)
           echo "⚠️  เงินจริง: บอทจะซื้อขายหุ้นในบัญชี Webull ของคุณ ขาดทุนได้จริง"
           echo "   ต้องมีเงิน USD ในบัญชี (แลกบาทเป็นดอลลาร์ในแอป Webull ก่อน)"
           read -r -p "เพดานทุนที่ให้บอทใช้ (USD): " cap
           read -r -p 'พิมพ์ "ยืนยันเงินจริง" เพื่อยืนยัน: ' ok
           [ "$ok" = "ยืนยันเงินจริง" ] || { echo "ยกเลิก"; continue; }
           cfgset live_confirmed '"ยืนยันเงินจริง"'
           [ -n "$cap" ] && cfgset max_capital_usd "$cap" ;;
         *) echo "ไม่ถูกต้อง"; continue ;;
       esac
       sed -i "s/--mode [a-z]*/--mode $m/" "$SVC"; systemctl daemon-reload
       systemctl restart $SVC_NAME; echo "เปลี่ยนเป็น $m แล้ว" ;;
    4) echo "low  = สวิงเทรด ซื้อตอนย่อในหุ้นขาขึ้น ถือหลายวัน (เสี่ยง 0.5%/ไม้)"
       echo "mid  = โมเมนตัม หุ้นแข็งกว่าตลาดทะลุฐาน (เสี่ยง 1%/ไม้)"
       echo "high = เทรดระยะสั้น 15 นาที ขายหมดก่อนตลาดปิด (เสี่ยง 1.5%/ไม้)"
       echo "gold = ทองคำ (GLD/IAU/GLDM) ตามแนวโน้มทอง + ดอลลาร์/ดอกเบี้ย/ความกลัว (เสี่ยง 1%/ไม้ ถือทีละ 1)"
       read -r -p "ระดับ (low/mid/high/gold): " r
       case "$r" in low|mid|high|gold) cfgset risk "\"$r\""; systemctl restart $SVC_NAME; echo "เปลี่ยนเป็น $r แล้ว" ;;
         *) echo "ไม่ถูกต้อง" ;; esac ;;
    5) journalctl -u $SVC_NAME -n 40 --no-pager ;;
    6) systemctl restart $SVC_NAME; echo "รีสตาร์ทแล้ว" ;;
    7) echo "กำลังตรวจเวอร์ชันใหม่..."
       bash "$DIR/auto_update.sh" && echo "เรียบร้อย" || echo "อัปเดตไม่สำเร็จ ย้อนกลับเวอร์ชันเดิมแล้ว"
       bash "$DIR/auto_update.sh" status ;;
    21) bash "$DIR/auto_update.sh" status
       read -r -p "พิมพ์ 1 = เปิด, 2 = ปิด, Enter = ไม่เปลี่ยน: " a
       case "$a" in 1) bash "$DIR/auto_update.sh" install ;; 2) bash "$DIR/auto_update.sh" remove ;; esac ;;
    8) read -r -p "รหัสใหม่ (อย่างน้อย 10 ตัว): " p
       [ ${#p} -ge 10 ] || { echo "สั้นเกินไป"; continue; }
       setvar DASHBOARD_PASSWORD "$p"; systemctl restart $SVC_NAME 2>/dev/null; systemctl restart stockscan 2>/dev/null
       echo "เปลี่ยนแล้ว กำลังทดสอบเข้าหน้าสแกนหุ้นด้วยรหัสใหม่..."; pw_test "$p" ;;
    19) p=$(grep '^DASHBOARD_PASSWORD=' "$ENV" | tail -1 | cut -d= -f2-)
        echo "รหัสหน้าเว็บตอนนี้: $p"; systemctl restart stockscan 2>/dev/null; pw_test "$p" ;;
    9) run --check ;;
    10) run --check-order ;;
    11) read -r -p "Channel access token (long-lived): " t; read -r -p "Your user ID (ขึ้นต้นด้วย U): " u
        [ -n "$t" ] && [ -n "$u" ] || { echo "ยกเลิก"; continue; }
        setvar LINE_CHANNEL_ACCESS_TOKEN "$t"; setvar LINE_USER_ID "$u"; run --test-line
        systemctl restart $SVC_NAME ;;
    12) run --test-line ;;
    13) read -r -p "เพดานทุน (USD): " cap; cfgset max_capital_usd "$cap"; systemctl restart $SVC_NAME; echo "บันทึกแล้ว" ;;
    14) read -r -p "Ping URL (https://hc-ping.com/...): " u
        case "$u" in https://*) setvar HEALTHCHECK_URL "$u"; systemctl restart $SVC_NAME; echo "บันทึกแล้ว" ;;
          *) echo "ยกเลิก" ;; esac ;;
    15) [ -f /etc/systemd/system/stockscan.service ] || bash "$DIR/scanner_setup.sh"
        echo "กำลังสแกน ~1-3 นาที..."; ( set -a; . "$ENV"; set +a; cd "$DIR" && "$PY" scanner.py --once ) ;;
    16) journalctl -u stockscan -n 40 --no-pager ;;
    17) echo "ขอฟรี/ดูของเดิมที่ aistudio.google.com → Get API key"
        read -r -s -p "Gemini API Key (พิมพ์แล้วไม่แสดง): " k; echo
        [ -n "$k" ] || { echo "ยกเลิก"; continue; }
        setvar GEMINI_API_KEY "$k"; systemctl restart stockscan; echo "บันทึกแล้ว" ;;
    18) read -r -p "ทุนที่ใช้เทรดหุ้น (USD, 0 = ไม่คำนวณขนาดไม้): " cap
        read -r -p "เวลาส่ง LINE (HH:MM เวลาไทย, Enter = 07:40): " tm
        "$PY" - "$DIR/config.json" "${cap:-0}" "${tm:-07:40}" <<'PY'
import json,sys
p,cap,tm=sys.argv[1:4]
c=json.load(open(p,encoding="utf-8"))
s=c.setdefault("scanner",{})
s["account_usd"]=float(cap); s["scan_time_th"]=tm
json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
PY
        systemctl restart stockscan; echo "บันทึกแล้ว" ;;
    0) exit 0 ;;
  esac
done
