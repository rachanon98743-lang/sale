#!/usr/bin/env bash
# อัปเดตบอทเองอัตโนมัติ (แทนการกดเมนู 7)
#   bash auto_update.sh install  = เปิดอัปเดตอัตโนมัติ (ตรวจ GitHub ทุก 5 นาที)
#   bash auto_update.sh remove   = ปิดอัปเดตอัตโนมัติ
#   bash auto_update.sh status   = ดูว่าเปิดอยู่ไหม + อัปเดตล่าสุดเมื่อไร
#   bash auto_update.sh          = ตรวจและอัปเดตทันที 1 ครั้ง (timer เรียกแบบนี้)
# ปลอดภัย: เก็บ config.json ในเครื่องไว้เสมอ, ตรวจโค้ดก่อนรีสตาร์ท, บอทเปิดไม่ขึ้น = ย้อนกลับเวอร์ชันเดิมเอง
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$DIR" in
  */webull_trading_company) SVC=webullbot; PY="$DIR/venv/bin/python" ;;
  *) SVC=tradingbot; PY=/usr/bin/python3 ;;
esac
UNIT="$SVC-autoupdate"
LOG="$DIR/auto_update.log"
log(){ echo "$(TZ=Asia/Bangkok date '+%Y-%m-%d %H:%M') $*" >> "$LOG"; tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; }

case "${1:-}" in
  install)
    cat > /etc/systemd/system/$UNIT.service <<UNITEOF
[Unit]
Description=Auto update $SVC from GitHub
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/bash $DIR/auto_update.sh
UNITEOF
    cat > /etc/systemd/system/$UNIT.timer <<UNITEOF
[Unit]
Description=Check GitHub for $SVC updates every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
    systemctl daemon-reload
    systemctl enable -q --now $UNIT.timer
    rm -f "$DIR/.no_autoupdate"
    echo "✅ เปิดอัปเดตอัตโนมัติแล้ว ($SVC ตรวจ GitHub ทุก 5 นาที ไม่ต้องกด 7 อีก)"
    exit 0 ;;
  remove)
    systemctl disable -q --now $UNIT.timer 2>/dev/null
    rm -f /etc/systemd/system/$UNIT.timer /etc/systemd/system/$UNIT.service
    systemctl daemon-reload
    touch "$DIR/.no_autoupdate"
    echo "ปิดอัปเดตอัตโนมัติแล้ว (กลับไปใช้เมนู 7 เอง)"
    exit 0 ;;
  status)
    if systemctl is-active -q $UNIT.timer; then echo "🟢 อัปเดตอัตโนมัติ: เปิดอยู่"; else echo "⚪ อัปเดตอัตโนมัติ: ปิดอยู่"; fi
    echo "เวอร์ชันตอนนี้: $(git -C "$DIR" log -1 --format='%h %s' 2>/dev/null)"
    [ -f "$LOG" ] && { echo "ประวัติล่าสุด:"; tail -n 5 "$LOG"; }
    exit 0 ;;
esac

# ---- ตรวจและอัปเดต ----
exec 9>/tmp/$UNIT.lock
flock -n 9 || exit 0   # มีรอบอื่นทำอยู่
cd "$DIR" || exit 1
BR=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$BR" 2>/dev/null || exit 0   # เน็ตหลุด/GitHub ล่ม ไว้รอบหน้า
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BR")
[ "$OLD" = "$NEW" ] && exit 0
BAD="$DIR/.bad_version"
[ "$(cat "$BAD" 2>/dev/null)" = "$NEW" ] && exit 0   # เวอร์ชันนี้เคยพังแล้ว รอเวอร์ชันแก้ใหม่

# เก็บค่าที่ตั้งในเครื่อง (config.json) ไว้ก่อน
cp -f config.json /tmp/$UNIT.config.json 2>/dev/null
restore_cfg(){ [ -f /tmp/$UNIT.config.json ] && cp -f /tmp/$UNIT.config.json config.json; }

git reset -q --hard "$NEW"
restore_cfg

# ตรวจโค้ดก่อน: ไฟล์ Python ต้องคอมไพล์ผ่านทุกไฟล์
if ! "$PY" -m py_compile *.py 2>>"$LOG"; then
  git reset -q --hard "$OLD"; restore_cfg
  echo "$NEW" > "$BAD"
  log "❌ เวอร์ชัน ${NEW:0:7} มีโค้ดเสีย ย้อนกลับ ${OLD:0:7} (บอทยังทำงานเวอร์ชันเดิม)"
  exit 1
fi
SVCS="$SVC"
if [ "$SVC" = webullbot ]; then
  "$DIR/venv/bin/pip" install -q --upgrade webull-openapi-python-sdk >/dev/null 2>&1
  [ -f "$DIR/scanner_setup.sh" ] && bash "$DIR/scanner_setup.sh" quiet >/dev/null 2>&1
  # บอทเทรดเปิดได้เมื่อมี Webull key เท่านั้น / ฝ่ายสแกนหุ้นไม่ต้องใช้ key
  SVCS=""
  grep -q '^WEBULL_APP_KEY=.' /root/webull.env 2>/dev/null && SVCS="webullbot"
  systemctl is-enabled -q stockscan 2>/dev/null && SVCS="$SVCS stockscan"
fi

for s in $SVCS; do systemctl restart "$s"; done
ok=1
for _ in 1 2; do sleep 30; for s in $SVCS; do systemctl is-active -q "$s" || ok=0; done; [ $ok = 1 ] || break; done
if [ $ok = 1 ]; then
  log "✅ อัปเดต ${OLD:0:7} → ${NEW:0:7} $(git log -1 --format=%s)"
else
  git reset -q --hard "$OLD"; restore_cfg
  for s in $SVCS; do systemctl restart "$s"; done
  echo "$NEW" > "$BAD"
  log "❌ เวอร์ชัน ${NEW:0:7} เปิดไม่ขึ้น ย้อนกลับ ${OLD:0:7} แล้ว"
  exit 1
fi
