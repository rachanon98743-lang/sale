#!/usr/bin/env bash
# ติดตั้ง AI Stock Company (Webull) บน VPS Ubuntu ในคำสั่งเดียว — แยกจากบอทคริปโต รันพร้อมกันได้
#   curl -fsSL https://raw.githubusercontent.com/rachanon98743-lang/sale/webull-bot/install.sh | sudo bash
set -euo pipefail

REPO="https://github.com/rachanon98743-lang/sale.git"
BRANCH=webull-bot
DIR=/root/webull_trading_company
ENV=/root/webull.env
CRYPTO_ENV=/root/bot.env
SVC_NAME=webullbot
PORT=8081
TTY=/dev/tty

say(){ printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
ask(){ local v; read -r -p "$1" v < "$TTY" || true; printf "%s" "$v"; }
setvar(){ touch "$ENV"; chmod 600 "$ENV"; grep -v "^$1=" "$ENV" > "$ENV.tmp" || true; echo "$1=$2" >> "$ENV.tmp"; mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"; }

[ "$(id -u)" = 0 ] || { echo "ต้องรันด้วย root"; exit 1; }

say "1/7 ติดตั้งโปรแกรมที่จำเป็น"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl ufw >/dev/null

say "2/7 ดาวน์โหลดบอทหุ้น"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q; else rm -rf "$DIR"; git clone -q -b "$BRANCH" --single-branch "$REPO" "$DIR"; fi

say "3/7 ติดตั้ง Webull OpenAPI SDK (ในโฟลเดอร์ของบอทเอง ไม่ยุ่งกับบอทคริปโต)"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip >/dev/null
"$DIR/venv/bin/pip" install -q --upgrade webull-openapi-python-sdk

say "4/7 ตั้งรหัสผ่านแดชบอร์ด + ใช้ LINE ตัวเดียวกับบอทคริปโต"
touch "$ENV"; chmod 600 "$ENV"
if ! grep -q '^DASHBOARD_PASSWORD=' "$ENV"; then
  PW=$(ask "ตั้งรหัสผ่านแดชบอร์ดหุ้น (อย่างน้อย 10 ตัว, เว้นว่าง = สุ่มให้): ")
  if [ ${#PW} -lt 10 ]; then PW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16); GEN=1; fi
  setvar DASHBOARD_PASSWORD "$PW"
  [ "${GEN:-0}" = 1 ] && printf "\n\033[1;33mรหัสผ่านแดชบอร์ดหุ้นของคุณคือ: %s\nจดเก็บไว้ตอนนี้เลย\033[0m\n" "$PW"
fi
if [ -f "$CRYPTO_ENV" ]; then  # ส่งแจ้งเตือนเข้า LINE OA เดิม และใช้ Claude key เดิม
  for k in LINE_CHANNEL_ACCESS_TOKEN LINE_USER_ID ANTHROPIC_API_KEY; do
    v=$(grep "^$k=" "$CRYPTO_ENV" | head -1 | cut -d= -f2- || true)
    [ -n "$v" ] && ! grep -q "^$k=" "$ENV" && setvar "$k" "$v" && echo "ใช้ $k เดียวกับบอทคริปโต"
  done
fi

say "5/7 ใส่ Webull API key (ขอที่ webull.co.th/en/open-api → Get API Key)"
if grep -q '^WEBULL_APP_KEY=' "$ENV"; then
  echo "มี key อยู่แล้ว (ใช้ของเดิม เปลี่ยนได้ที่ manage.sh ข้อ 1)"
else
  K=$(ask "App Key (เว้นว่าง = ใส่ทีหลังที่ manage.sh ข้อ 1): ")
  if [ -n "$K" ]; then
    S=$(ask "App Secret: ")
    setvar WEBULL_APP_KEY "$K"; setvar WEBULL_APP_SECRET "$S"
  fi
fi

say "6/7 ตั้งให้บอทรันเองตลอด 24 ชม. (ติดตั้งใหม่เริ่มที่โหมดเงินจำลอง)"
SVC=/etc/systemd/system/$SVC_NAME.service
MODE=paper
[ -f "$SVC" ] && MODE=$(grep -o -- '--mode [a-z]*' "$SVC" | awk '{print $2}') && [ -n "$MODE" ] || MODE=${MODE:-paper}
cat > "$SVC" <<EOF
[Unit]
Description=AI Stock Company (Webull)
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
WorkingDirectory=$DIR
EnvironmentFile=$ENV
ExecStart=$DIR/venv/bin/python main.py --host 0.0.0.0 --port $PORT --mode $MODE
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q $SVC_NAME
if grep -q '^WEBULL_APP_KEY=' "$ENV"; then systemctl restart $SVC_NAME; else echo "ยังไม่มี key: บอทจะเริ่มหลังใส่ key ที่ manage.sh ข้อ 1"; fi
# อัปเดตเองจาก GitHub ทุก 5 นาที (ไม่ต้องกดเมนู 7)
[ -f "$DIR/.no_autoupdate" ] || bash "$DIR/auto_update.sh" install

say "7/7 เปิดพอร์ตแดชบอร์ดให้เฉพาะ Tailscale"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow in on tailscale0 to any port $PORT >/dev/null
ufw --force enable >/dev/null
tailscale ip -4 >/dev/null 2>&1 || tailscale up
IP=$(tailscale ip -4 | head -1)
cat <<EOF

  ✅ ติดตั้งบอทหุ้น Webull แล้ว (โหมด $MODE) แยกจากบอทคริปโต

  แดชบอร์ดหุ้น:  http://$IP:$PORT   (บอทคริปโตยังอยู่ที่พอร์ต 8080)
  เมนูจัดการ:    bash $DIR/manage.sh

  ครั้งแรกที่บอทต่อ Webull: เปิดแอป Webull บนมือถือแล้วกดยืนยันการเข้าสู่ระบบ (2FA) ที่เด้งขึ้นมา
EOF
