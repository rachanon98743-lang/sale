"""dashboard.py — หน้าเว็บดูสถานะบริษัท (เปิดได้เฉพาะเครื่องตัวเอง http://localhost:8081)"""

import gzip
import hashlib
import hmac
import http.cookies
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from departments import RISK_PROFILES, now_th


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # มือถือปิดแท็บ/สลับแอประหว่างโหลด = ConnectionReset ไม่ใช่ปัญหา ไม่ต้องพิมพ์ traceback ยาวๆ ลง log
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _dur(sec):
    sec = int(sec)
    return "%d วัน %d ชม." % (sec // 86400, sec % 86400 // 3600) if sec >= 86400 else "%d ชม. %d นาที" % (sec // 3600, sec % 3600 // 60)


class Snapshot:
    """สร้างข้อมูลหน้าเว็บไว้ล่วงหน้าทุก 4 วินาทีใน thread แยก: เปิดหน้าเว็บกี่เครื่องก็ไม่ไปถ่วงบอท
    และไม่ต้องรอถาม Webull ระหว่างโหลดหน้า (สาเหตุหลักที่แดชบอร์ดเคยช้า)"""

    def __init__(self, co):
        self.co = co
        self.body = b"{}"
        self.gz = gzip.compress(self.body)
        self.etag = '"0"'
        self.eq_series, self.eq_t = [], 0
        self.fc, self.fc_t = {"positions": [], "expected": 0, "best": 0, "worst": 0, "win_rate": 0, "trades": 0}, 0
        self.lock = threading.Lock()
        try:
            self.refresh()
        except Exception as e:
            print("แดชบอร์ด: สร้างข้อมูลครั้งแรกไม่สำเร็จ:", e, flush=True)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            time.sleep(4)
            try:
                self.refresh()
            except Exception as e:
                print("แดชบอร์ด: สร้างข้อมูลไม่สำเร็จ:", e, flush=True)

    def build(self):
        co = self.co
        eq = co.equity_cached() or 0
        today = now_th().date().isoformat()
        day0 = co.acct.get("day_start_" + today) or eq
        if time.time() - self.eq_t > 60:
            self.eq_series, self.eq_t = co.acct.equity_series(7), time.time()
        if time.time() - self.fc_t > 30:
            try:
                self.fc = co.forecast()
            except Exception:
                pass
            self.fc_t = time.time()
        usage, limits, over = co.ai.budget_status()
        st = co.line._load()
        lb = getattr(co, "line_bot", None)
        research = [{k: r.get(k) for k in ("symbol", "price", "chg24", "rsi", "score", "why", "bt")} for r in co.last_research[:30]]
        return {
            "t": now_th().isoformat(timespec="seconds"),
            "mode": co.mode + " · Webull", "quote": co.ex.quote,
            "risk": RISK_PROFILES[co.risk]["name"], "tf": RISK_PROFILES[co.risk]["tf"], "regime": co.regime,
            "equity": eq, "day_pct": (eq / day0 - 1) * 100 if day0 else 0, "equity_series": self.eq_series,
            "summary": co.acct.summary(), "positions": co.acct.open_positions(), "closed": co.acct.closed_positions(50),
            "research": research, "logs": co.acct.recent_logs(80), "decisions": co.acct.recent_decisions(30, only_rejected=True),
            "stopped": os.path.exists("STOP"), "forecast": self.fc,
            "health": {"loop_age": time.time() - co.last_ok, "uptime": _dur(time.time() - co.started_at), "down": co.downtime_note},
            "ai": {"has_key": bool(co.ai.key), "enabled": co.ai.enabled, "enabled_setting": bool(co.ai.setting("ai_enabled", True)),
                   "model": co.ai.model, "usage": usage, "limits": limits, "over": over, "calls": co.acct.recent_ai_calls(30)},
            "activity": co.acct.dept_activity(),
            "line": {"enabled": co.line.enabled, "limit": co.line.monthly_limit,
                     "count": st.get("count", 0) if st.get("month") == time.strftime("%Y-%m") else 0,
                     "health": getattr(lb, "health", None), "last_webhook": co.acct.get("line_last_webhook")},
            "depts": [{"icon": i, "name": n, "note": ("Claude " + co.ai.model.split("-")[1].title()) if "AI" in n and co.ai.enabled
                       else ("ปิดอยู่" if "AI" in n else "")} for i, n in DEPTS],
        }

    def refresh(self):
        data = self.build()
        stable = dict(data, t="", health=dict(data["health"], loop_age=int(data["health"]["loop_age"] // 60), uptime=""))
        tag = hashlib.md5(json.dumps(stable, ensure_ascii=False, default=str, sort_keys=True).encode()).hexdigest()[:16]
        with self.lock:
            if '"%s"' % tag == self.etag:
                return  # ไม่มีอะไรเปลี่ยน: มือถือได้ 304 ไม่ต้องโหลดซ้ำ
            body = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")).encode()
            self.body, self.gz, self.etag = body, gzip.compress(body, 6), '"%s"' % tag

    def get(self):
        with self.lock:
            return self.body, self.gz, self.etag

PAGE = r"""<!DOCTYPE html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0d1117">
<title>AI Stock Company (Webull)</title>
<style>
*{-webkit-tap-highlight-color:transparent}
body{margin:0;background:#0d1117;color:#e6edf3;font:14px/1.5 system-ui,-apple-system,Tahoma,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:1100px;margin:auto;padding:12px 12px 40px}h1{font-size:18px;margin:6px 0}h3{margin:0 0 8px;font-size:15px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:12px;margin-bottom:12px;contain:content}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.s{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:8px}.s b{display:block;font-size:18px}.s span{color:#8b949e;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 5px;border-bottom:1px solid #30363d;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:#8b949e;font-weight:500;position:sticky;top:0;background:#161b22}
.sc{overflow:auto;max-height:360px;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
.g{color:#3fb950}.r{color:#f85149}.a{color:#d29922}.m{color:#8b949e}.dept{color:#58a6ff}
button{padding:8px 14px;border-radius:8px;border:1px solid #30363d;background:#21262d;color:#e6edf3;cursor:pointer;font-size:14px}
button.pri{background:#58a6ff;color:#000;border:0;font-weight:600}
.live{background:#5a1010;padding:2px 8px;border-radius:6px}
.depts{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px}
.dp{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:8px;cursor:pointer}
.dp.on{border-color:#58a6ff;background:#13233a}.dp b{font-size:14px}.dp .last{font-size:12px;color:#8b949e;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;background:#30363d}.dot.act{background:#3fb950}.dot.w{background:#d29922}.dot.x{background:#f85149}
.bar{height:8px;background:#21262d;border-radius:4px;overflow:hidden;margin:4px 0 10px}.bar i{display:block;height:100%;background:#3fb950}
.bar i.w{background:#d29922}.bar i.x{background:#f85149}
label{display:block;color:#8b949e;font-size:12px;margin-top:8px}
input,select{width:100%;box-sizing:border-box;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:8px;font-size:16px}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:4px 10px}
details summary{cursor:pointer;color:#58a6ff}pre{white-space:pre-wrap;font-size:12px;color:#c9d1d9;margin:4px 0}
.strip{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:#8b949e;margin-bottom:10px}
.strip span{background:#161b22;border:1px solid #30363d;border-radius:20px;padding:3px 10px}
svg{display:block;width:100%;height:auto}
.fc{border:1px solid #30363d;border-radius:8px;padding:8px;margin-bottom:8px;background:#0d1117}
.rng{position:relative;height:10px;border-radius:5px;background:linear-gradient(90deg,#5a1d1d,#21262d 45%,#21262d 55%,#163b22);margin:6px 0}
.rng i{position:absolute;top:-3px;width:3px;height:16px;background:#e6edf3;border-radius:2px}.rng i.e{background:#8b949e}
tr.click{cursor:pointer}tr.click:active{background:#21262d}
.tabs button{padding:4px 10px;font-size:12px;margin:0 4px 6px 0}.tabs button.on{border-color:#58a6ff;color:#58a6ff}
</style></head><body><main>
<h1>🏢 AI Stock Company (Webull) <span id="mode"></span> <a href="/logout" style="float:right;font-size:13px;color:#8b949e">ออกจากระบบ</a></h1>
<div class="strip" id="strip"><span>กำลังโหลด...</span></div>

<div class="card"><div class="grid">
<div class="s"><span>มูลค่าพอร์ต (<span id="q">USD</span>)</span><b id="eq">-</b></div>
<div class="s"><span>วันนี้</span><b id="day">-</b></div>
<div class="s"><span>กำไรที่ปิดแล้ว</span><b id="pnl">-</b></div>
<div class="s"><span>ชนะ / ทั้งหมด</span><b id="wr">-</b></div>
<div class="s"><span>ภาวะตลาด</span><b id="reg">-</b></div></div>
<div id="eqChart" style="margin-top:10px"></div>
<p style="margin-bottom:0"><button id="stop">⏸ หยุดเปิดไม้ใหม่</button> <span class="m" id="stopTxt"></span></p></div>

<div class="card"><h3>แผนกต่างๆ <span class="m" style="font-weight:400;font-size:12px">แตะเพื่อดูงานของแผนกนั้น</span></h3><div class="depts" id="depts"></div>
<h3 style="margin-top:12px">บันทึกการทำงาน <span id="logTitle" class="m" style="font-weight:400"></span></h3><div class="sc"><table><tbody id="logs"></tbody></table></div></div>

<div class="card"><h3>📐 คาดการณ์ไม้ที่ถือ <span class="m" style="font-weight:400;font-size:12px">ประเมินจากสถิติ ไม่ใช่การรับประกัน</span></h3><div id="fc"></div></div>

<div class="card"><h3>📈 กราฟ <span id="chTitle" class="m" style="font-weight:400"></span></h3>
<div class="tabs" id="chTabs"></div><div id="chart" class="m">แตะหุ้นในตาราง "สถานะที่ถืออยู่" หรือ "ผลวิจัย" เพื่อดูกราฟ</div></div>

<div class="card"><h3>สถานะที่ถืออยู่</h3><div class="sc"><table><thead><tr><th>หุ้น</th><th>ตอนนี้</th><th>ราคาซื้อ</th><th>ทุน</th><th>SL</th><th>TP</th><th>เหตุผล</th></tr></thead><tbody id="pos"></tbody></table></div></div>

<div class="card"><h3>🚫 ไม่อนุมัติล่าสุด + เหตุผล</h3><div class="sc" id="dec"></div></div>

<div class="card"><h3>ผลวิจัยล่าสุด</h3><div class="sc"><table><thead><tr><th>หุ้น</th><th>ราคา</th><th>24ชม.</th><th>RSI</th><th>คะแนน</th><th>ย้อนหลังชนะ</th><th>คาดหวัง/ไม้</th><th>เหตุผล</th></tr></thead><tbody id="res"></tbody></table></div></div>

<div class="card"><h3>🧠 ฝ่ายนักวิเคราะห์ AI · งบและการใช้ token</h3>
<div id="aiState" class="m"></div>
<div id="bars"></div>
<details id="setBox"><summary>⚙️ ตั้งงบ / เพดาน token</summary>
<div class="form">
<div><label>เปิดใช้ AI</label><select id="f_en"><option value="1">เปิด</option><option value="0">ปิด</option></select></div>
<div><label>รุ่น AI</label><select id="f_model"><option value="claude-haiku-5-5">Haiku 5.5 (ถูกสุด)</option><option value="claude-sonnet-5-5">Sonnet 5.5</option><option value="claude-opus-5-5">Opus 5.5 (แพงสุด)</option></select></div>
<div><label>งบต่อวัน (USD)</label><input id="f_day" type="number" step="0.01" min="0" max="100"></div>
<div><label>งบต่อเดือน (USD)</label><input id="f_month" type="number" step="0.1" min="0" max="1000"></div>
<div><label>token สูงสุดต่อวัน</label><input id="f_tok" type="number" step="1000" min="0" max="50000000"></div>
<div><label>token ตอบสูงสุดต่อครั้ง</label><input id="f_call" type="number" step="50" min="100" max="4000"></div>
</div>
<p><button class="pri" id="save">บันทึก</button> <span id="saveMsg" class="m"></span></p>
<p class="m" style="font-size:12px">ถึงเพดานไหนก่อน AI จะหยุดทันที บอทยังเทรดต่อตามกฎ ไม่ใช้ AI จนกว่าจะขึ้นวัน/เดือนใหม่</p>
</details>
<h3 style="margin-top:12px">ประวัติการเรียก AI</h3>
<div class="sc"><table><thead><tr><th>เวลา</th><th>งาน</th><th>token เข้า/ออก</th><th>ค่าใช้จ่าย</th></tr></thead><tbody id="calls"></tbody></table></div>
</div>

<div class="card"><h3>💬 LINE</h3>
<div id="lineState" class="m"></div><div id="lineBar"></div><div id="lineHealth" style="font-size:12px"></div>
<p><button id="ltest">ส่งข้อความทดสอบ</button> <span id="lmsg" class="m"></span></p>
<p class="m" style="font-size:12px">แจ้งเมื่อ: บอทเริ่มงาน/ดับแล้วกลับมา · ซื้อ (พร้อมคาดการณ์และเหตุผล AI)/ขายทุกไม้ · ขาดทุนถึงเพดาน · บอทมีปัญหา · LINE เชื่อมต่อไม่ได้ · สรุปทุกคืน 3 ทุ่ม<br>
ในแชทกดปุ่มลัด: แผนก · ไม่อนุมัติ · คาดการณ์ (ตอบกลับไม่กินโควตา) · LINE OA ฟรี 300 ข้อความ/เดือน</p>
</div>

<div class="card"><h3>ประวัติการเทรด</h3><div class="sc"><table><thead><tr><th>หุ้น</th><th>ซื้อ</th><th>ขาย</th><th>กำไร</th><th>เหตุผล</th><th>ปิดเมื่อ</th></tr></thead><tbody id="hist"></tbody></table></div></div>
</main><script>
const f=(n,d=2)=>Number(n??0).toLocaleString('en-US',{maximumFractionDigits:d,minimumFractionDigits:d});
const p6=n=>{n=Number(n);const a=Math.abs(n);return a>=100?f(n,2):a>=1?f(n,4):n.toPrecision(4);};
const c=v=>v>0?'g':v<0?'r':'m';const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
let sel=null,D=null,formLoaded=false,last={},chartSym=null,chartTf=null,etag='',timer=null;
const ago=t=>{if(!t)return'ยังไม่ทำงานวันนี้';const s=(Date.now()-new Date(t).getTime())/1000;return s<60?'เมื่อสักครู่':s<3600?Math.floor(s/60)+' นาทีที่แล้ว':Math.floor(s/3600)+' ชม.ที่แล้ว';};
// อัปเดตเฉพาะส่วนที่ข้อมูลเปลี่ยน: หน้าไม่กระตุก ไม่เด้ง และ <details> ที่เปิดไว้ไม่หุบเอง
function put(id,html){if(last[id]===html)return;last[id]=html;const el=$(id);
 const open=[...el.querySelectorAll('details[open]')].map(d=>d.dataset.k);el.innerHTML=html;
 open.forEach(k=>{const d=el.querySelector('details[data-k="'+k+'"]');if(d)d.open=true;});}
function bar(label,used,lim,fmt){const p=lim>0?Math.min(100,used/lim*100):100;const cl=p>=100?'x':p>=80?'w':'';
 return `<div class="m" style="font-size:12px">${label}: <b style="color:#e6edf3">${fmt(used)}</b> / ${fmt(lim)}</div><div class="bar"><i class="${cl}" style="width:${p}%"></i></div>`;}
function line(pts,W=600,H=150){if(pts.length<2)return'<div class="m" style="font-size:12px">กราฟมูลค่าพอร์ตจะขึ้นเมื่อมีข้อมูลมากขึ้น</div>';
 const v=pts.map(p=>p[1]),mn=Math.min(...v),mx=Math.max(...v),pad=(mx-mn)*0.1||mx*0.001||1,lo=mn-pad,hi=mx+pad;
 const X=i=>i/(pts.length-1)*W,Y=y=>H-(y-lo)/(hi-lo)*H;const d=pts.map((p,i)=>(i?'L':'M')+X(i).toFixed(1)+','+Y(p[1]).toFixed(1)).join('');
 const up=v[v.length-1]>=v[0],col=up?'#3fb950':'#f85149';
 return `<svg viewBox="0 0 ${W} ${H+18}" preserveAspectRatio="none"><path d="${d}L${W},${H}L0,${H}Z" fill="${col}" opacity=".12"/><path d="${d}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/>
 <text x="2" y="12" fill="#8b949e" font-size="11">${p6(mx)}</text><text x="2" y="${H-2}" fill="#8b949e" font-size="11">${p6(mn)}</text>
 <text x="0" y="${H+15}" fill="#8b949e" font-size="11">${esc(pts[0][0].slice(5).replace('T',' '))}</text><text x="${W}" y="${H+15}" fill="#8b949e" font-size="11" text-anchor="end">${esc(pts[pts.length-1][0].slice(5).replace('T',' '))}</text></svg>`;}
function candles(k,lines,W=600,H=260){if(!k.length)return'<div class="m">ไม่มีข้อมูลกราฟ</div>';
 let mn=Math.min(...k.map(x=>x[3])),mx=Math.max(...k.map(x=>x[2]));lines.forEach(l=>{mn=Math.min(mn,l.v);mx=Math.max(mx,l.v);});
 const pad=(mx-mn)*0.05,lo=mn-pad,hi=mx+pad,Y=y=>H-(y-lo)/(hi-lo)*H,w=W/k.length;
 let s=k.map((x,i)=>{const up=x[4]>=x[1],col=up?'#3fb950':'#f85149',cx=i*w+w/2,t=Y(Math.max(x[1],x[4])),b=Y(Math.min(x[1],x[4]));
  return `<line x1="${cx}" x2="${cx}" y1="${Y(x[2])}" y2="${Y(x[3])}" stroke="${col}"/><rect x="${i*w+w*.15}" width="${w*.7}" y="${t}" height="${Math.max(1,b-t)}" fill="${col}"/>`;}).join('');
 s+=lines.map(l=>`<line x1="0" x2="${W}" y1="${Y(l.v)}" y2="${Y(l.v)}" stroke="${l.c}" stroke-dasharray="6 4"/><text x="${W-2}" y="${Y(l.v)-3}" fill="${l.c}" font-size="12" text-anchor="end">${esc(l.n)} ${p6(l.v)}</text>`).join('');
 return `<svg viewBox="0 0 ${W} ${H}">${s}</svg>`;}
async function showChart(sym,tf){chartSym=sym;chartTf=tf||chartTf||D.tf;$('chTitle').textContent='· '+sym+' '+chartTf;
 $('chTabs').innerHTML=['15m','1h','4h','1d'].map(t=>`<button class="${t===chartTf?'on':''}" data-t="${t}">${t}</button>`).join('');
 $('chTabs').querySelectorAll('button').forEach(b=>b.onclick=()=>showChart(chartSym,b.dataset.t));
 $('chart').innerHTML='<span class="m">กำลังโหลด...</span>';
 try{const r=await fetch('/api/chart?symbol='+encodeURIComponent(sym)+'&tf='+chartTf);const j=await r.json();
  const p=D.positions.find(x=>x.symbol===sym),L=p?[{v:p.entry,n:'ซื้อ',c:'#8b949e'},{v:p.sl,n:'SL',c:'#f85149'},{v:p.tp,n:'TP',c:'#3fb950'}]:[];
  $('chart').innerHTML=candles(j.k||[],L)+(j.error?'<div class="r">'+esc(j.error)+'</div>':'');}catch(e){$('chart').textContent='โหลดกราฟไม่ได้';}}
function renderLogs(){const L=D.logs.filter(l=>!sel||l.dept===sel);$('logTitle').textContent=sel?'· '+sel+' (แตะแผนกเดิมเพื่อดูทั้งหมด)':'· ทุกแผนก';
 put('logs',L.map(l=>`<tr><td class="m">${esc(l.t.slice(11,19))}</td><td class="dept">${esc(l.dept)}</td><td style="text-align:left;white-space:normal">${esc(l.msg)}</td></tr>`).join('')||'<tr><td class="m">ยังไม่มี</td></tr>');}
function renderDepts(){put('depts',D.depts.map(d=>{const a=D.activity[d.name]||{};const act=a.t&&(Date.now()-new Date(a.t).getTime())<20*60000;
 return `<div class="dp ${sel===d.name?'on':''}" data-n="${esc(d.name)}"><b><span class="dot ${act?'act':''}"></span>${d.icon} ${esc(d.name)}</b>
 <div class="m" style="font-size:12px">${ago(a.t)}${a.today?' · วันนี้ '+a.today+' งาน':''}${d.note?' · '+esc(d.note):''}</div><div class="last">${esc(a.last||'')}</div></div>`;}).join(''));}
$('depts').onclick=e=>{const el=e.target.closest('.dp');if(!el)return;sel=sel===el.dataset.n?null:el.dataset.n;last.depts=null;renderDepts();renderLogs();};
document.addEventListener('click',e=>{const tr=e.target.closest('tr[data-s]');if(tr){showChart(tr.dataset.s);$('chart').scrollIntoView({behavior:'smooth',block:'center'});}});
function render(){
$('mode').innerHTML=D.mode.startsWith('live')?'<span class="live">เงินจริง'+esc(D.mode.slice(4))+'</span>':'<span class="m">'+esc(D.mode)+'</span>';
const H=D.health;put('strip',`<span>${H.loop_age<180?'🟢 ทำงานอยู่':'🔴 ลูปหลักไม่ขยับ '+Math.floor(H.loop_age/60)+' นาที'}</span><span>อัปเดต ${esc(D.t.slice(11,19))}</span><span>เปิดมาแล้ว ${H.uptime}</span>${H.down?'<span class="a">⚠️ '+esc(H.down)+'</span>':''}`);
$('eq').textContent=f(D.equity);$('q').textContent=D.quote||'USD';$('day').innerHTML='<span class="'+c(D.day_pct)+'">'+(D.day_pct>0?'+':'')+f(D.day_pct)+'%</span>';
$('pnl').innerHTML='<span class="'+c(D.summary.realized_pnl)+'">'+f(D.summary.realized_pnl)+'</span>';
$('wr').textContent=D.summary.wins+' / '+D.summary.trades+' ('+D.summary.win_rate+'%)';$('reg').textContent=D.regime;
put('eqChart',line(D.equity_series));
$('stopTxt').textContent=D.stopped?'หยุดเปิดไม้ใหม่อยู่ (ไม้เดิมยังถูกเฝ้า)':'';$('stop').textContent=D.stopped?'▶ กลับมาเปิดไม้ใหม่':'⏸ หยุดเปิดไม้ใหม่';
const A=D.ai,u=A.usage,l=A.limits;
put('aiState',!A.has_key?'⚪ ยังไม่ได้ใส่ Claude API key (เมนู manage.sh ข้อ 2) บอทใช้กฎอย่างเดียว':!A.enabled?'⚪ ปิด AI อยู่':A.over.length?'🔴 ถึงเพดานแล้ว: '+esc(A.over.join(', '))+' (AI หยุดชั่วคราว)':'🟢 ทำงานอยู่ · รุ่น '+esc(A.model)+' · วันนี้เรียก '+u.day_calls+' ครั้ง');
put('bars',bar('ค่าใช้จ่ายวันนี้',u.day_usd,l.day_usd,v=>'$'+f(v,4))+bar('ค่าใช้จ่ายเดือนนี้',u.month_usd,l.month_usd,v=>'$'+f(v,4))+bar('token วันนี้',u.day_tokens,l.day_tokens,v=>f(v,0)));
if(!formLoaded){$('f_en').value=A.enabled_setting?'1':'0';$('f_model').value=A.model;$('f_day').value=l.day_usd;$('f_month').value=l.month_usd;$('f_tok').value=l.day_tokens;$('f_call').value=l.max_tokens_per_call;formLoaded=true;}
put('calls',A.calls.map(x=>`<tr><td class="m">${esc(x.t.slice(5,16).replace('T',' '))}</td><td style="text-align:left;white-space:normal"><details data-k="${esc(x.t)}"><summary>${esc(x.purpose)}</summary><pre>${esc(x.result)}</pre></details></td><td>${f(x.tin,0)} / ${f(x.tout,0)}</td><td>$${f(x.usd,5)}</td></tr>`).join('')||'<tr><td class="m">ยังไม่มี</td></tr>');
const LN=D.line;put('lineState',LN.enabled?'🟢 ส่งแจ้งเตือนได้':'⚪ ยังไม่ได้เชื่อม LINE: ใส่ token และ user ID ที่เมนู manage.sh ข้อ 11');
put('lineBar',bar('ข้อความเดือนนี้',LN.count,LN.limit,v=>f(v,0)));
put('lineHealth',(LN.last_webhook?'<div class="m">รับคำสั่งจากแชทล่าสุด: '+esc(LN.last_webhook.slice(5,16).replace('T',' '))+'</div>':'')+(LN.health?LN.health.items.map(i=>`<div class="${i[0]?'g':'r'}">${i[0]?'✅':'❌'} ${esc(i[1])}</div>`).join('')+'<div class="m">ตรวจล่าสุด '+esc(LN.health.t.slice(11,16))+' (ตรวจ/ซ่อมเองทุก 30 นาที)</div>':''));
const F=D.forecast,q=D.quote;put('fc',F.positions.length?F.positions.map(r=>{const span=r.tp-r.sl||1,x=v=>Math.min(100,Math.max(0,(v-r.sl)/span*100));
 return `<div class="fc"><b>${esc(r.symbol)}</b> <span class="${c(r.pnl_now)}">${r.pnl_now>0?'+':''}${f(r.pnl_now)}</span> <span class="m" style="font-size:12px">ตอนนี้</span>
 <div class="rng"><i class="e" style="left:${x(r.entry)}%"></i><i style="left:${x(r.now)}%"></i></div>
 <div style="font-size:12px;display:flex;justify-content:space-between"><span class="r">SL ${f(r.if_sl)}</span><span>โอกาสถึงเป้า ~${Math.round(r.p_win*100)}% · คาดหวัง <b class="${c(r.expected)}">${r.expected>0?'+':''}${f(r.expected)}</b></span><span class="g">TP +${f(r.if_tp)}</span></div></div>`;}).join('')+
 `<div style="font-size:13px">รวม: คาดหวัง <b class="${c(F.expected)}">${F.expected>0?'+':''}${f(F.expected)} ${esc(q)}</b> · ดีสุด <span class="g">+${f(F.best)}</span> · แย่สุด <span class="r">${f(F.worst)}</span></div>`
 :'<div class="m">ยังไม่มีไม้ที่ถืออยู่ · สถิติที่ผ่านมา ชนะ '+F.win_rate+'% จาก '+F.trades+' ไม้</div>');
const nowpx=Object.fromEntries(F.positions.map(r=>[r.symbol,r.now]));
put('pos',D.positions.map(p=>{const n=nowpx[p.symbol]??p.entry,pc=(n/p.entry-1)*100;return `<tr class="click" data-s="${esc(p.symbol)}"><td>📈 ${esc(p.symbol)}</td><td class="${c(pc)}">${p6(n)} (${pc>0?'+':''}${f(pc)}%)</td><td>${p6(p.entry)}</td><td>${f(p.cost)}</td><td>${p6(p.sl)}</td><td>${p6(p.tp)}</td><td class="m" style="text-align:left;white-space:normal;min-width:160px">${esc(p.why)}</td></tr>`;}).join('')||'<tr><td class="m">ยังไม่มี</td></tr>');
put('dec',D.decisions.map(d=>`<div style="border-bottom:1px solid #30363d;padding:6px 0;font-size:13px"><span class="m">${esc(d.t.slice(11,16))}</span> <b>${esc(d.symbol)}</b> <span class="dept">· ${esc(d.dept.replace('ฝ่าย',''))}</span><div>${esc(d.reason)}</div></div>`).join('')||'<div class="m">ยังไม่มี</div>');
put('res',D.research.map(r=>{const b=r.bt||{};return `<tr class="click" data-s="${esc(r.symbol)}"><td>${esc(r.symbol)}</td><td>${p6(r.price)}</td><td class="${c(r.chg24)}">${f(r.chg24)}%</td><td>${f(r.rsi,0)}</td><td><b>${r.score}</b></td><td>${b.trades?Math.round(b.p_win*100)+'% <span class="m">('+b.trades+')</span>':'-'}</td><td class="${c(b.ev)}">${b.trades?(b.ev>0?'+':'')+f(b.ev*100)+'%':'-'}</td><td class="m" style="text-align:left;white-space:normal;min-width:160px">${esc(r.why.join(', '))}</td></tr>`;}).join('')||'<tr><td class="m">ยังไม่สแกน</td></tr>');
put('hist',D.closed.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${p6(p.entry)}</td><td>${p6(p.exit)}</td><td class="${c(p.pnl)}">${f(p.pnl)}</td><td>${esc(p.reason)}</td><td class="m">${esc((p.closed||'').slice(5,16).replace('T',' '))}</td></tr>`).join('')||'<tr><td class="m">ยังไม่มี</td></tr>');
renderDepts();renderLogs();}
async function load(){clearTimeout(timer);try{const res=await fetch('/api/status',{cache:'no-cache'});if(res.status===401){location.reload();return;}
 const tx=await res.text();if(tx!==etag){etag=tx;D=JSON.parse(tx);requestAnimationFrame(render);}}catch(e){}
 if(!document.hidden)timer=setTimeout(load,5000);}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
$('stop').onclick=async()=>{await fetch('/api/toggle-stop',{method:'POST'});load();};
$('save').onclick=async()=>{const body={ai_enabled:$('f_en').value==='1',ai_model:$('f_model').value,ai_daily_budget_usd:+$('f_day').value,ai_monthly_budget_usd:+$('f_month').value,ai_daily_token_limit:+$('f_tok').value,ai_max_tokens_per_call:+$('f_call').value};
 const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json().catch(()=>({}));
 $('saveMsg').textContent=r.ok?'✅ บันทึกแล้ว':'❌ '+(j.error||'บันทึกไม่ได้');formLoaded=false;load();};
$('ltest').onclick=async()=>{$('lmsg').textContent='กำลังส่ง...';const r=await fetch('/api/line-test',{method:'POST'});const j=await r.json().catch(()=>({}));$('lmsg').textContent=j.msg||'ผิดพลาด';load();};
load();
</script></body></html>"""

DEPTS = [("👔", "CEO"), ("🔎", "ฝ่ายวิจัยตลาด"), ("♟️", "ฝ่ายกลยุทธ์"), ("🧠", "ฝ่ายนักวิเคราะห์ AI"),
         ("🛡️", "ฝ่ายบริหารความเสี่ยง"), ("⚡", "ฝ่ายเทรด"), ("📒", "ฝ่ายบัญชี")]
SETTINGS = {  # ชื่อ: (ชนิด, ต่ำสุด, สูงสุด)
    "ai_enabled": (bool, None, None), "ai_model": (str, None, None),
    "ai_daily_budget_usd": (float, 0, 100), "ai_monthly_budget_usd": (float, 0, 1000),
    "ai_daily_token_limit": (int, 0, 50_000_000), "ai_max_tokens_per_call": (int, 100, 4000),
}
MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}


LOGIN_PAGE = r"""<!DOCTYPE html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>เข้าสู่ระบบ</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;font:15px system-ui,Tahoma,sans-serif}
form{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:min(320px,90vw)}
input,button{width:100%;box-sizing:border-box;padding:12px;margin-top:10px;border-radius:8px;font-size:16px}
input{background:#0d1117;color:#e6edf3;border:1px solid #30363d}button{background:#58a6ff;border:0;font-weight:600}
p{color:#f85149;min-height:1em}</style></head><body>
<form method="post" action="/login"><h2>🏢 AI Stock Company (Webull)</h2>
<input type="password" name="password" placeholder="รหัสผ่าน" autofocus required>
<button>เข้าสู่ระบบ</button><p>%MSG%</p></form></body></html>"""

MAX_FAILS, LOCK_SECONDS, SESSION_SECONDS = 5, 15 * 60, 7 * 24 * 3600


def serve(company, port, host="127.0.0.1"):
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if host != "127.0.0.1" and len(password) < 10:
        company.log("CEO", "ไม่เปิดแดชบอร์ดสู่เครือข่าย: ต้องตั้ง DASHBOARD_PASSWORD ยาวอย่างน้อย 10 ตัวอักษร")
        host = "127.0.0.1"
    snap = Snapshot(company)
    chart_cache = {}       # (symbol, tf) -> (เวลา, ข้อมูล)
    sessions = {}          # token -> หมดอายุเมื่อไร
    fails = {}             # ip -> (จำนวนครั้งที่ผิด, เวลาที่เริ่มนับ)
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, headers=None, cache="no-store"):
            b = body.encode() if isinstance(body, str) else body
            if len(b) > 1500 and "gzip" in (self.headers.get("Accept-Encoding") or "") and not (headers or {}).get("Content-Encoding"):
                b = gzip.compress(b, 6)
                headers = dict(headers or {}, **{"Content-Encoding": "gzip"})
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _authed(self):
            if not password:
                return True
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            tok = c["wsid"].value if "wsid" in c else ""
            with lock:
                exp = sessions.get(tok)
                if exp and exp > time.time():
                    return True
                sessions.pop(tok, None)
            return False

        def _login_page(self, msg=""):
            self._send(200, LOGIN_PAGE.replace("%MSG%", msg), "text/html; charset=utf-8")

        def do_GET(self):
            if not self._authed():
                if self.path.startswith("/api/"):
                    return self._send(401, "{}", "application/json")
                return self._login_page()
            if self.path == "/logout":
                c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                with lock:
                    sessions.pop(c["wsid"].value if "wsid" in c else "", None)
                return self._send(302, "", "text/plain", {"Location": "/", "Set-Cookie": "wsid=; Max-Age=0; Path=/"})
            if self.path == "/api/status":
                body, gz, tag = snap.get()
                if self.headers.get("If-None-Match") == tag:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                use_gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
                return self._send(200, gz if use_gz else body, "application/json; charset=utf-8",
                                  dict({"ETag": tag}, **({"Content-Encoding": "gzip"} if use_gz else {})), cache="no-cache")
            if self.path.startswith("/api/chart"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                sym = (qs.get("symbol") or [""])[0].upper()
                tf = (qs.get("tf") or [RISK_PROFILES[company.risk]["tf"]])[0]
                if sym not in company.ex.filters or tf not in ("15m", "1h", "4h", "1d"):
                    return self._send(400, json.dumps({"error": "หุ้น/ช่วงเวลาไม่ถูกต้อง"}, ensure_ascii=False), "application/json; charset=utf-8")
                hit = chart_cache.get((sym, tf))
                if hit and time.time() - hit[0] < 60:
                    out = hit[1]
                else:
                    try:
                        k = company.ex.klines(sym, tf, 100)
                        out = {"k": [[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in k]}
                        chart_cache[(sym, tf)] = (time.time(), out)
                    except Exception as e:
                        out = {"k": [], "error": "ดึงกราฟไม่ได้: %s" % e}
                return self._send(200, json.dumps(out), "application/json")
            else:
                self._send(200, PAGE, "text/html; charset=utf-8")

        def do_POST(self):
            ip = self.client_address[0]
            if self.path == "/login":
                with lock:
                    n, t0 = fails.get(ip, (0, time.time()))
                    if time.time() - t0 > LOCK_SECONDS:
                        n, t0 = 0, time.time()
                    if n >= MAX_FAILS:
                        return self._login_page("ใส่รหัสผิดหลายครั้ง ล็อกไว้ 15 นาที")
                length = min(int(self.headers.get("Content-Length", 0) or 0), 2000)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
                given = (form.get("password") or [""])[0]
                if password and hmac.compare_digest(given.encode(), password.encode()):
                    tok = secrets.token_urlsafe(32)
                    with lock:
                        sessions[tok] = time.time() + SESSION_SECONDS
                        fails.pop(ip, None)
                    company.log("CEO", "เข้าสู่ระบบแดชบอร์ดจาก %s" % ip)
                    return self._send(302, "", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": "wsid=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (tok, SESSION_SECONDS)})
                with lock:
                    fails[ip] = (n + 1, t0)
                company.log("CEO", "ใส่รหัสแดชบอร์ดผิดจาก %s (ครั้งที่ %d)" % (ip, n + 1))
                time.sleep(1)
                return self._login_page("รหัสผ่านไม่ถูกต้อง")
            if not self._authed():
                return self._send(401, "{}", "application/json")
            if self.path == "/api/settings":
                length = min(int(self.headers.get("Content-Length", 0) or 0), 5000)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    clean = {}
                    for k, v in body.items():
                        if k not in SETTINGS:
                            continue
                        typ, lo, hi = SETTINGS[k]
                        v = typ(v)
                        if typ in (int, float) and not (lo <= v <= hi):
                            raise ValueError("%s ต้องอยู่ระหว่าง %s ถึง %s" % (k, lo, hi))
                        if k == "ai_model" and v not in MODELS:
                            raise ValueError("รุ่นไม่ถูกต้อง")
                        clean[k] = v
                except (ValueError, TypeError) as e:
                    return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")
                for k, v in clean.items():
                    company.acct.set("set_" + k, v)
                company.log("CEO", "ปรับงบ AI จากแดชบอร์ด: " + ", ".join("%s=%s" % kv for kv in clean.items()))
                return self._send(200, "{}", "application/json")
            if self.path == "/api/line-test":
                if not company.line.enabled:
                    msg = "ยังไม่ได้ตั้ง LINE (manage.sh ข้อ 11)"
                elif company.line.send("🔔 ทดสอบแจ้งเตือนจากแดชบอร์ด", key="dash_test", every_sec=30):
                    msg = "✅ ส่งแล้ว ดูใน LINE ได้เลย"
                else:
                    msg = "❌ ส่งไม่สำเร็จ: ตรวจ token / user ID และต้องเพิ่มเพื่อน LINE OA ก่อน (หรือกดถี่เกิน 30 วินาที)"
                return self._send(200, json.dumps({"msg": msg}, ensure_ascii=False), "application/json; charset=utf-8")
            if self.path == "/api/toggle-stop":
                if os.path.exists("STOP"):
                    os.remove("STOP")
                    company.log("CEO", "กลับมาเปิดไม้ใหม่ (สั่งจากแดชบอร์ด)")
                else:
                    open("STOP", "w").close()
                    company.log("CEO", "หยุดเปิดไม้ใหม่ (สั่งจากแดชบอร์ด)")
                self._send(200, "{}", "application/json")
            else:
                self._send(404, "", "text/plain")

    company.log("CEO", "แดชบอร์ดเปิดที่ %s:%d %s" % (host, port, "(มีรหัสผ่าน)" if password else "(เฉพาะเครื่องนี้)"))
    QuietServer((host, port), H).serve_forever()
