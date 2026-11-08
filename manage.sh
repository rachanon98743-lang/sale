#!/usr/bin/env bash
# เมนูจัดการบอท: bash /root/ai_trading_company/manage.sh
set -uo pipefail
DIR=/root/ai_trading_company
ENV=/root/bot.env
SVC=/etc/systemd/system/tradingbot.service

setvar(){ # setvar KEY VALUE
  touch "$ENV"; chmod 600 "$ENV"
  grep -v "^$1=" "$ENV" > "$ENV.tmp" || true
  echo "$1=$2" >> "$ENV.tmp"; mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"
}
mode(){ grep -o -- '--mode [a-z]*' "$SVC" | awk '{print $2}'; }
exch(){ python3 -c "import json;print(json.load(open('$DIR/config.json',encoding='utf-8')).get('exchange','binance'))"; }

while true; do
  echo
  echo "=== AI Trading Company | กระดาน: $(exch) | โหมด: $(mode) | สถานะ: $(systemctl is-active tradingbot) ==="
  echo " 1) ใส่ Binance API key"
  echo " 2) ใส่ Claude (Anthropic) API key"
  echo " 3) เปลี่ยนโหมด: paper / testnet / live"
  echo " 4) เปลี่ยนระดับความเสี่ยง: low / mid / high"
  echo " 5) ดู log ล่าสุด"
  echo " 6) รีสตาร์ทบอท"
  echo " 7) อัปเดตบอทเป็นเวอร์ชันล่าสุด"
  echo " 8) เปลี่ยนรหัสผ่านแดชบอร์ด"
  echo " 9) เลือกกระดาน: binance_th (บัญชีไทย) / binance (binance.com)"
  echo "10) ตรวจการเชื่อมต่อกระดานและยอดเงิน (ไม่ซื้อขาย)"
  echo "11) ตั้งแจ้งเตือนเข้า LINE"
  echo "12) ส่งข้อความทดสอบเข้า LINE"
  echo "13) เปิดสั่งงานผ่านแชท LINE + ติดตั้งริชเมนู"
  echo "14) ดูคู่เหรียญทั้งหมดที่กระดานเปิดให้บอทเทรด"
  echo "15) Binance TH: เลือกเทรดคู่ THB (~11 เหรียญ) หรือคู่ USDT (~380 เหรียญ)"
  echo "16) ติดตั้งริชเมนู LINE ใหม่ (มีปุ่มเปิดแดชบอร์ด)"
  echo " 0) ออก"
  read -r -p "เลือก: " c
  case "$c" in
    1) read -r -p "API Key: " k; read -r -p "API Secret: " s
       setvar BINANCE_API_KEY "$k"; setvar BINANCE_API_SECRET "$s"
       systemctl restart tradingbot; echo "บันทึกแล้ว" ;;
    2) read -r -p "Anthropic API Key: " k; setvar ANTHROPIC_API_KEY "$k"
       systemctl restart tradingbot; echo "บันทึกแล้ว" ;;
    3) read -r -p "โหมด (paper/testnet/live): " m
       case "$m" in
         paper|testnet) ;;
         live)
           echo "⚠️  เงินจริง: บอทจะซื้อขายในบัญชี Binance ของคุณ ขาดทุนได้จริง"
           read -r -p "เพดานทุนที่ให้บอทใช้ (หน่วยเป็นเงินที่เทรด: THB สำหรับ binance_th, USDT สำหรับ binance): " cap
           read -r -p 'พิมพ์ "ยืนยันเงินจริง" เพื่อยืนยัน: ' ok
           [ "$ok" = "ยืนยันเงินจริง" ] || { echo "ยกเลิก"; continue; }
           python3 - "$DIR/config.json" "$cap" <<'PY'
import json,sys
p,cap=sys.argv[1],sys.argv[2]
c=json.load(open(p,encoding="utf-8"))
c["live_confirmed"]="ยืนยันเงินจริง"
try: c["max_capital_usdt"]=float(cap)
except ValueError: pass
json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
PY
           ;;
         *) echo "ไม่ถูกต้อง"; continue ;;
       esac
       sed -i "s/--mode [a-z]*/--mode $m/" "$SVC"
       systemctl daemon-reload; systemctl restart tradingbot; echo "เปลี่ยนเป็น $m แล้ว" ;;
    4) read -r -p "ความเสี่ยง (low/mid/high): " r
       case "$r" in low|mid|high)
         python3 - "$DIR/config.json" "$r" <<'PY'
import json,sys
p,r=sys.argv[1],sys.argv[2]
c=json.load(open(p,encoding="utf-8")); c["risk"]=r
json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
PY
         systemctl restart tradingbot; echo "เปลี่ยนเป็น $r แล้ว" ;;
       *) echo "ไม่ถูกต้อง" ;; esac ;;
    5) journalctl -u tradingbot -n 40 --no-pager ;;
    6) systemctl restart tradingbot; echo "รีสตาร์ทแล้ว" ;;
    7) git -C "$DIR" stash -q 2>/dev/null; git -C "$DIR" pull -q && git -C "$DIR" stash pop -q 2>/dev/null
       systemctl restart tradingbot; echo "อัปเดตแล้ว" ;;
    8) read -r -p "รหัสใหม่ (อย่างน้อย 10 ตัว): " p
       [ ${#p} -ge 10 ] || { echo "สั้นเกินไป"; continue; }
       setvar DASHBOARD_PASSWORD "$p"; systemctl restart tradingbot; echo "เปลี่ยนแล้ว" ;;
    9) read -r -p "กระดาน (binance_th/binance): " e
       case "$e" in binance_th|binance)
         python3 - "$DIR/config.json" "$e" <<'PY'
import json,sys
p,e=sys.argv[1],sys.argv[2]
c=json.load(open(p,encoding="utf-8")); c["exchange"]=e
if e=="binance": c.pop("quote",None)
json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
PY
         [ "$e" = binance_th ] && [ "$(mode)" = testnet ] && sed -i "s/--mode [a-z]*/--mode paper/" "$SVC" && systemctl daemon-reload
         systemctl restart tradingbot; echo "เปลี่ยนเป็น $e แล้ว" ;;
       *) echo "ไม่ถูกต้อง" ;; esac ;;
    10) ( set -a; . "$ENV"; set +a; cd "$DIR" && python3 main.py --check --mode "$(mode)" ) ;;
    11) echo "ต้องมี LINE OA + Messaging API ก่อน (ดูวิธีใน README หัวข้อ แจ้งเตือนเข้า LINE)"
        read -r -p "Channel access token (long-lived): " t; read -r -p "Your user ID (ขึ้นต้นด้วย U): " u
        [ -n "$t" ] && [ -n "$u" ] || { echo "ยกเลิก"; continue; }
        setvar LINE_CHANNEL_ACCESS_TOKEN "$t"; setvar LINE_USER_ID "$u"
        ( set -a; . "$ENV"; set +a; cd "$DIR" && python3 main.py --test-line )
        systemctl restart tradingbot; echo "บันทึกแล้ว และรีสตาร์ทบอทแล้ว" ;;
    12) ( set -a; . "$ENV"; set +a; cd "$DIR" && python3 main.py --test-line ) ;;
    14) ( cd "$DIR" && python3 main.py --markets ) ;;
    16) grep -q '^LINE_CHANNEL_ACCESS_TOKEN=' "$ENV" || { echo "ต้องทำข้อ 11 (ตั้ง LINE) ก่อน"; continue; }
        ( set -a; . "$ENV"; set +a; cd "$DIR" && python3 richmenu.py ); systemctl restart tradingbot ;;
    15) read -r -p "สกุลเงินที่ใช้เทรด (THB/USDT): " q
        case "$q" in THB|USDT)
          [ "$(mode)" = live ] && echo "⚠️ โหมดเงินจริง: เพดานทุน (max_capital_usdt) จะนับเป็น $q และต้องมีเงิน $q ในบัญชี Binance TH"
          python3 - "$DIR/config.json" "$q" <<'PY2'
import json,sys
p,q=sys.argv[1],sys.argv[2]
c=json.load(open(p,encoding="utf-8")); c["quote"]=q
json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
PY2
          systemctl restart tradingbot; echo "เปลี่ยนเป็นคู่ $q แล้ว" ;;
        *) echo "ไม่ถูกต้อง" ;; esac ;;
    13) grep -q '^LINE_CHANNEL_ACCESS_TOKEN=' "$ENV" || { echo "ต้องทำข้อ 11 (ตั้ง LINE) ก่อน"; continue; }
        echo "Channel secret อยู่ที่ developers.line.biz → channel → Basic settings"
        read -r -p "Channel secret: " sec
        [ -n "$sec" ] || { echo "ยกเลิก"; continue; }
        setvar LINE_CHANNEL_SECRET "$sec"
        echo "เปิดทางให้ LINE ส่งข้อความเข้ามา (Tailscale Funnel)..."
        echo "ถ้ามีลิงก์ขึ้นมา ให้เปิดลิงก์นั้นแล้วกดอนุญาต Funnel แล้วรันข้อ 13 ซ้ำอีกครั้ง"
        tailscale funnel --bg --yes 8090 || { echo "เปิด Funnel ไม่สำเร็จ"; continue; }
        DNS=$(tailscale status --json | python3 -c "import json,sys;print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
        systemctl restart tradingbot
        ( set -a; . "$ENV"; set +a; cd "$DIR" && python3 richmenu.py )
        echo
        echo "✅ เกือบเสร็จ: ไปที่ developers.line.biz → channel → แท็บ Messaging API"
        echo "   1) Webhook URL ใส่:  https://$DNS/line/webhook   แล้วกด Update และ Verify"
        echo "   2) เปิดสวิตช์ Use webhook"
        echo "   3) LINE Official Account features: ปิด Auto-reply messages และ Greeting messages"
        echo "   แล้วลองพิมพ์ 'สถานะ' ในแชท LINE OA" ;;
    0) exit 0 ;;
  esac
done
