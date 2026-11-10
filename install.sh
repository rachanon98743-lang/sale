#!/usr/bin/env bash
# ติดตั้ง AI Trading Company บน VPS Ubuntu ในคำสั่งเดียว
#   curl -fsSL https://raw.githubusercontent.com/rachanon98743-lang/sale/kiki-bot/install.sh | sudo bash
set -euo pipefail

REPO="https://github.com/rachanon98743-lang/sale.git"
BRANCH=kiki-bot
DIR=/root/ai_trading_company
ENV=/root/bot.env
TTY=/dev/tty

say(){ printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
ask(){ local v; read -r -p "$1" v < "$TTY" || true; printf "%s" "$v"; }

[ "$(id -u)" = 0 ] || { echo "ต้องรันด้วย root"; exit 1; }

say "1/6 ติดตั้งโปรแกรมที่จำเป็น"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 git curl ufw >/dev/null

say "2/6 ดาวน์โหลดบอท"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q; else rm -rf "$DIR"; git clone -q -b "$BRANCH" --single-branch "$REPO" "$DIR"; fi

say "3/6 ตั้งรหัสผ่านแดชบอร์ด"
if [ -f "$ENV" ] && grep -q '^DASHBOARD_PASSWORD=' "$ENV"; then
  echo "มีรหัสผ่านอยู่แล้ว (ใช้ของเดิม)"
else
  PW=$(ask "ตั้งรหัสผ่านแดชบอร์ด (อย่างน้อย 10 ตัว, เว้นว่าง = สุ่มให้): ")
  if [ ${#PW} -lt 10 ]; then PW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16); GEN=1; fi
  touch "$ENV"; chmod 600 "$ENV"
  echo "DASHBOARD_PASSWORD=$PW" >> "$ENV"
  [ "${GEN:-0}" = 1 ] && printf "\n\033[1;33mรหัสผ่านแดชบอร์ดของคุณคือ: %s\nจดเก็บไว้ตอนนี้เลย\033[0m\n" "$PW"
fi
chmod 600 "$ENV"

say "4/6 ตั้งให้บอทรันเองตลอด 24 ชม. (ติดตั้งใหม่เริ่มที่โหมดเงินจำลอง, ติดตั้งซ้ำใช้โหมดเดิม)"
SVC=/etc/systemd/system/tradingbot.service
MODE=paper
[ -f "$SVC" ] && MODE=$(grep -o -- '--mode [a-z]*' "$SVC" | awk '{print $2}') && [ -n "$MODE" ] || MODE=${MODE:-paper}
cat > "$SVC" <<EOF
[Unit]
Description=AI Trading Company
Wants=network-online.target
After=network-online.target
# รีสตาร์ตได้ไม่จำกัดครั้ง (ไม่ยอมแพ้แม้เน็ตล่มนาน)
StartLimitIntervalSec=0
[Service]
WorkingDirectory=$DIR
EnvironmentFile=$ENV
ExecStart=/usr/bin/python3 main.py --host 0.0.0.0 --mode $MODE
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q tradingbot
systemctl restart tradingbot
# อัปเดตเองจาก GitHub ทุก 5 นาที (ไม่ต้องกดเมนู 7)
[ -f "$DIR/.no_autoupdate" ] || bash "$DIR/auto_update.sh" install

say "5/6 ติดตั้ง Tailscale (เครือข่ายส่วนตัวสำหรับเข้าแดชบอร์ดจากมือถือ)"
mkdir -p /etc/apt/sources.list.d /usr/share/keyrings
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow in on tailscale0 to any port 8080 >/dev/null
ufw --force enable >/dev/null
if ! tailscale ip -4 >/dev/null 2>&1; then
  echo "เปิดลิงก์ที่ขึ้นด้านล่างบนมือถือ แล้วล็อกอินเพื่อเชื่อม VPS เข้ากับบัญชี Tailscale ของคุณ"
  tailscale up
fi

say "6/6 เสร็จแล้ว"
IP=$(tailscale ip -4 | head -1)
cat <<EOF

  ✅ บอทกำลังทำงาน (โหมด $MODE) และจะเปิดเองทุกครั้งที่ VPS รีบูตหรือบอทล่ม

  บนมือถือ:
    1) ติดตั้งแอป Tailscale แล้วล็อกอินบัญชีเดียวกัน กดเปิดการเชื่อมต่อ
    2) เปิดเบราว์เซอร์ไปที่:  http://$IP:8080
    3) ใส่รหัสผ่านแดชบอร์ด

  คำสั่งที่ใช้บ่อย:
    bash $DIR/manage.sh          เมนูจัดการบอท (ใส่ API key / เปลี่ยนโหมด / ดู log / ตั้งแจ้งเตือน LINE ข้อ 11)
EOF
