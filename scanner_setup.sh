#!/usr/bin/env bash
# ติดตั้ง/อัปเดต "ฝ่ายสแกนหุ้น US + Gemini" (service stockscan, หน้าเว็บพอร์ต 8082) — ไม่ต้องใช้ Webull API
#   bash /root/webull_trading_company/scanner_setup.sh         = ติดตั้ง + เริ่มทำงาน
#   bash /root/webull_trading_company/scanner_setup.sh quiet   = ตรวจให้ครบเฉยๆ (auto_update เรียก) ไม่รีสตาร์ท
set -uo pipefail
DIR=/root/webull_trading_company
ENV=/root/webull.env
CRYPTO_ENV=/root/bot.env
PORT=8082
PY="$DIR/venv/bin/python"
SVC=/etc/systemd/system/stockscan.service

setvar(){ touch "$ENV"; chmod 600 "$ENV"; grep -v "^$1=" "$ENV" > "$ENV.tmp" || true; echo "$1=$2" >> "$ENV.tmp"; mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"; }

[ -x "$PY" ] || python3 -m venv "$DIR/venv"
if [ "${1:-}" != quiet ] || ! "$PY" -c "import yfinance, pandas" 2>/dev/null; then
  echo "ติดตั้งตัวดึงข้อมูลหุ้น (yfinance)..."
  "$DIR/venv/bin/pip" install -q --upgrade yfinance pandas >/dev/null 2>&1
fi

# ใช้ Gemini key / LINE / รหัสแดชบอร์ด เดียวกับบอทคริปโต ถ้ายังไม่มี
touch "$ENV"; chmod 600 "$ENV"
if [ -f "$CRYPTO_ENV" ]; then
  for k in GEMINI_API_KEY LINE_CHANNEL_ACCESS_TOKEN LINE_USER_ID; do
    v=$(grep "^$k=" "$CRYPTO_ENV" | head -1 | cut -d= -f2- || true)
    [ -n "$v" ] && ! grep -q "^$k=" "$ENV" && setvar "$k" "$v" && echo "ใช้ $k เดียวกับบอทคริปโต"
  done
fi
if ! grep -q '^DASHBOARD_PASSWORD=' "$ENV"; then
  PW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16); setvar DASHBOARD_PASSWORD "$PW"
  printf "\n\033[1;33mรหัสผ่านหน้าเว็บสแกนหุ้น: %s  (จดไว้)\033[0m\n" "$PW"
fi

NEW_UNIT=$(cat <<EOF
[Unit]
Description=Stock scanner + Gemini (US stocks, LINE report)
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
WorkingDirectory=$DIR
EnvironmentFile=$ENV
ExecStart=$PY scanner.py
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
EOF
)
if [ "$(cat "$SVC" 2>/dev/null)" != "$NEW_UNIT" ]; then
  echo "$NEW_UNIT" > "$SVC"
  systemctl daemon-reload
fi
systemctl enable -q stockscan
command -v ufw >/dev/null && ufw allow in on tailscale0 to any port $PORT >/dev/null 2>&1

[ "${1:-}" = quiet ] && exit 0
systemctl restart stockscan
IP=$(tailscale ip -4 2>/dev/null | head -1)
cat <<EOF

  ✅ ฝ่ายสแกนหุ้น US + Gemini ทำงานแล้ว (ไม่ต้องใช้ Webull API)

  หน้าเว็บ:  http://${IP:-<IP Tailscale>}:$PORT   (ชื่อผู้ใช้พิมพ์อะไรก็ได้ รหัส = รหัสแดชบอร์ดหุ้น)
  ส่ง LINE:  ทุกวันอังคาร-เสาร์ 07:40 น. (หลังตลาด US ปิด)
  รอบแรก:    กำลังสแกน รอ 3 นาทีแล้วเปิดหน้าเว็บ
  ลองส่ง LINE ตอนนี้: เมนู bash $DIR/manage.sh ข้อ 15
EOF
grep -q '^GEMINI_API_KEY=' "$ENV" || echo "  ⚠️ ยังไม่มี Gemini key: ใส่ที่ manage.sh ข้อ 17 (ไม่มีก็ทำงานได้ แต่ไม่มีความเห็น Gemini)"
