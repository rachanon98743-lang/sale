#!/usr/bin/env bash
# เมนูจัดการบอทหุ้น Webull: bash /root/webull_trading_company/manage.sh
set -uo pipefail
DIR=/root/webull_trading_company
ENV=/root/webull.env
SVC_NAME=webullbot
SVC=/etc/systemd/system/$SVC_NAME.service
PY="$DIR/venv/bin/python"

setvar(){ touch "$ENV"; chmod 600 "$ENV"; grep -v "^$1=" "$ENV" > "$ENV.tmp" || true; echo "$1=$2" >> "$ENV.tmp"; mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"; }
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

while true; do
  echo
  echo "=== AI Stock Company (Webull) | โหมด: $(mode) | สถานะ: $(systemctl is-active $SVC_NAME) ==="
  echo " 1) ใส่ Webull App Key / App Secret"
  echo " 2) ใส่ Claude (Anthropic) API key"
  echo " 3) เปลี่ยนโหมด: paper / live"
  echo " 4) เปลี่ยนระดับความเสี่ยง: low / mid / high"
  echo " 5) ดู log ล่าสุด"
  echo " 6) รีสตาร์ทบอท"
  echo " 7) อัปเดตบอทเป็นเวอร์ชันล่าสุด"
  echo " 8) เปลี่ยนรหัสผ่านแดชบอร์ด"
  echo " 9) ตรวจการเชื่อมต่อ Webull และยอดเงิน (ไม่ซื้อขาย)"
  echo "10) ให้ Webull ตรวจคำสั่งซื้อทดลอง SPY \$5 (preview ไม่ซื้อจริง)"
  echo "11) ตั้งแจ้งเตือนเข้า LINE (ถ้ายังไม่ได้ใช้ของบอทคริปโต)"
  echo "12) ส่งข้อความทดสอบเข้า LINE"
  echo "13) ตั้งเพดานทุนเงินจริง (USD)"
  echo "14) ตั้งแจ้งเตือนเมื่อ VPS ดับ (healthchecks.io)"
  echo " 0) ออก"
  read -r -p "เลือก: " c
  case "$c" in
    1) echo "ขอได้ที่ webull.co.th/en/open-api → Get API Key (ห้ามส่ง key ให้ใครในแชท)"
       read -r -p "App Key: " k; read -r -s -p "App Secret (พิมพ์แล้วไม่แสดง): " s; echo
       [ -n "$k" ] && [ -n "$s" ] || { echo "ยกเลิก"; continue; }
       setvar WEBULL_APP_KEY "$k"; setvar WEBULL_APP_SECRET "$s"
       echo "กำลังทดสอบ... ถ้าแอป Webull เด้งให้ยืนยันการเข้าสู่ระบบ ให้กดยืนยัน"
       run --check; systemctl restart $SVC_NAME; echo "บันทึกแล้ว" ;;
    2) read -r -s -p "Anthropic API Key: " k; echo; setvar ANTHROPIC_API_KEY "$k"
       systemctl restart $SVC_NAME; echo "บันทึกแล้ว" ;;
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
       read -r -p "ระดับ (low/mid/high): " r
       case "$r" in low|mid|high) cfgset risk "\"$r\""; systemctl restart $SVC_NAME; echo "เปลี่ยนเป็น $r แล้ว" ;;
         *) echo "ไม่ถูกต้อง" ;; esac ;;
    5) journalctl -u $SVC_NAME -n 40 --no-pager ;;
    6) systemctl restart $SVC_NAME; echo "รีสตาร์ทแล้ว" ;;
    7) git -C "$DIR" stash -q 2>/dev/null; git -C "$DIR" pull -q && git -C "$DIR" stash pop -q 2>/dev/null
       "$DIR/venv/bin/pip" install -q --upgrade webull-openapi-python-sdk
       systemctl restart $SVC_NAME; echo "อัปเดตแล้ว" ;;
    8) read -r -p "รหัสใหม่ (อย่างน้อย 10 ตัว): " p
       [ ${#p} -ge 10 ] || { echo "สั้นเกินไป"; continue; }
       setvar DASHBOARD_PASSWORD "$p"; systemctl restart $SVC_NAME; echo "เปลี่ยนแล้ว" ;;
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
    0) exit 0 ;;
  esac
done
