"""
โต๊ะเดย์เทรดเดอร์ (Pro Desk) — เครื่องมือจาก day-trader-skill มาช่วยฝ่ายสแกนหุ้น
ที่มา: https://github.com/Jakeschincariol/day-trader-skill (MIT License, Jake Schincariol)
       skill ทั้ง 11 ตัวอยู่ใน .claude/skills/trader-*

ใช้ 3 อย่าง (ทุกอย่างพังได้โดยไม่ทำให้การสแกนหลักพัง):
  🎲 Time Machine   จำลอง 3,000 เส้นทางราคาจากแท่งรายวันของหุ้นตัวนั้นเอง + ย้อนดูอดีต
                    ว่าแผน (จุดซื้อ / ตัดขาดทุน / เป้า1) ถึงเป้าก่อน หรือโดนตัดก่อน กี่ %
  📰 ข่าว            หัวข่าว Yahoo ต่อหุ้น + เอกสาร 8-K ที่ SEC (ล้มละลาย, งบเชื่อถือไม่ได้, ถูกถอดตลาด ...)
  🏛️ สส.สหรัฐฯ       รายงานซื้อขายหุ้นของ สส. (House PTR) 45 วันล่าสุด ว่าใครซื้อ/ขายหุ้นที่ระบบคัด
"""
import datetime as dt
import importlib.util
import json
import os
import random
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, ".claude", "skills")
NEWS_STATE = os.path.join(HERE, "scan_desk_news.json")
CONGRESS_CACHE = os.path.join(HERE, "scan_desk_congress.json")
PDF_DIR = os.path.join(HERE, "congress_pdfs")

# เอกสาร 8-K ที่ถือว่า "ห้ามแตะ" ทันที
CRITICAL_8K = {"1.03": "ยื่นล้มละลาย", "3.01": "ถูกเตือนถอดออกจากตลาด", "4.02": "งบที่ผ่านมาเชื่อถือไม่ได้",
               "1.05": "ถูกโจมตีทางไซเบอร์", "2.04": "หนี้ถูกเรียกคืนก่อนกำหนด"}
TAG_TH = {"earnings": "งบ", "analyst": "นักวิเคราะห์", "legal": "คดี/สอบสวน", "deal": "ควบรวม/ดีล",
          "capital": "ปันผล/ซื้อคืน/เพิ่มทุน", "people": "ผู้บริหาร", "risk": "ความเสี่ยง", "halt": "หยุดซื้อขาย",
          "regulatory": "หน่วยงานกำกับ", "macro": "เศรษฐกิจ"}

_mods = {}


def _load(name):
    """โหลดไฟล์ .py ของ skill (เช่น trader-news/news.py) คืน module หรือ None"""
    if name in _mods:
        return _mods[name]
    path = os.path.join(SKILLS, name)
    mod = None
    if os.path.isfile(path):
        spec = importlib.util.spec_from_file_location("dts_" + name.replace("/", "_").replace("-", "_")[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    _mods[name] = mod
    return mod


def _jload(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _jsave(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, p)


# ===================================================================== 🎲 Time Machine (แท่งรายวัน)
def _bars(d):
    o, h, lo, c = (list(map(float, d[k].values)) for k in ("Open", "High", "Low", "Close"))
    return o, h, lo, c


def _walk(entry, stop, target, steps):
    """เดินตามแท่ง (open, high, low, close) จริงหรือจำลอง คืน (ผล, R, วันที่จบ)
    ราคาเปิดกระโดดข้าม stop/เป้า = ได้ราคาเปิด, วันเดียวโดนทั้งคู่ = นับว่าโดนตัดก่อน (มองแง่ร้ายไว้ก่อน)"""
    R = entry - stop
    px = entry
    for k, (op, hi, lo, cl) in enumerate(steps, 1):
        if op <= stop:
            return "stop", (op - entry) / R, k
        if op >= target:
            return "target", (op - entry) / R, k
        if lo <= stop:
            return "stop", -1.0, k
        if hi >= target:
            return "target", (target - entry) / R, k
        px = cl
    return "neither", (px - entry) / R, len(steps)


def time_machine(d, entry, stop, target, days=20, paths=3000, lookback=250, seed=19):
    """จำลองแผนเทรด: สุ่มแท่งรายวันจริงของหุ้นตัวนี้ (lookback วันล่าสุด) ต่อกันเป็น paths เส้นทาง
    + ย้อนทดสอบ: ถ้าเข้าทุกวันในอดีตด้วยระยะ stop/เป้า % เท่านี้ จะได้ผลยังไง"""
    if not (stop < entry < target):
        return None
    o, h, lo, c = _bars(d)
    n = len(c)
    if n < 60:
        return None
    pool = [(o[i] / c[i - 1], h[i] / c[i - 1], lo[i] / c[i - 1], c[i] / c[i - 1])
            for i in range(max(1, n - lookback), n) if c[i - 1] > 0]
    rng = random.Random(seed)
    cnt = {"target": 0, "stop": 0, "neither": 0}
    rs, dd = [], []
    for _ in range(paths):
        px, steps = entry, []
        for _ in range(days):
            ro, rh, rl, rc = rng.choice(pool)
            steps.append((px * ro, px * rh, px * rl, px * rc))
            px *= rc
        res, r, k = _walk(entry, stop, target, steps)
        cnt[res] += 1
        rs.append(r)
        if res == "target":
            dd.append(k)
    s_pct, t_pct = (entry - stop) / entry, (target - entry) / entry
    hist = {"target": 0, "stop": 0, "neither": 0}
    hr = []
    for i in range(max(0, n - 500), n - days - 1):
        e = c[i]
        steps = [(o[j], h[j], lo[j], c[j]) for j in range(i + 1, i + 1 + days)]
        res, r, _ = _walk(e, e * (1 - s_pct), e * (1 + t_pct), steps)
        hist[res] += 1
        hr.append(r)
    P = lambda x: round(100.0 * x / paths)  # noqa: E731
    avg_r = sum(rs) / len(rs)
    out = {"p_target": P(cnt["target"]), "p_stop": P(cnt["stop"]), "p_neither": P(cnt["neither"]),
           "avg_r": round(avg_r, 2), "days": days, "paths": paths,
           "median_days": sorted(dd)[len(dd) // 2] if dd else None,
           "breakeven_pct": round(100.0 / (1 + t_pct / s_pct)),
           "hist_n": len(hr), "hist_target": round(100.0 * hist["target"] / len(hr)) if hr else None,
           "hist_avg_r": round(sum(hr) / len(hr), 2) if hr else None}
    if avg_r >= 0.3:
        out["verdict"], out["verdict_th"] = "edge", "ได้เปรียบชัด"
    elif avg_r >= 0.05:
        out["verdict"], out["verdict_th"] = "lean", "ได้เปรียบเล็กน้อย"
    elif avg_r > -0.05:
        out["verdict"], out["verdict_th"] = "coin", "พอๆ กับโยนเหรียญ"
    else:
        out["verdict"], out["verdict_th"] = "against", "เสียเปรียบ"
    return out


def tm_text(tm):
    return "ถึงเป้า1 ก่อน %d%% · โดนตัดก่อน %d%% · ไม่ถึงทั้งคู่ใน %d วัน %d%% · เฉลี่ย %+.2fR → %s" % (
        tm["p_target"], tm["p_stop"], tm["days"], tm["p_neither"], tm["avg_r"], tm["verdict_th"])


# ===================================================================== 📰 ข่าว (Yahoo + SEC 8-K)
def sec_ua(cfg):
    ua = (os.environ.get("SEC_USER_AGENT") or cfg.get("sec_user_agent") or "").strip()
    return ua if "@" in ua and "example.com" not in ua else None


def news_check(symbols, cfg, hours=72, names=None):
    """คืน ({symbol: {...}}, ข้อความสถานะ)"""
    news = _load("trader-news/news.py")
    if not news or not symbols:
        return {}, "ไม่มีโมดูลข่าว"
    ua = sec_ua(cfg)
    state = _jload(NEWS_STATE, {})
    state = {"ciks": state.get("ciks", {})}
    errors = []
    watch = {t: (names or {}).get(t) or t for t in symbols}   # ticker -> ชื่อบริษัท (ใช้จับข่าวที่พูดถึง)
    f = news.Fetcher(sec_ua=ua, pause=0.25)
    items = news.collect(f, watch, state, sec=bool(ua), count=10, errors=errors)
    news.tag(items, watch)
    items = news.dedupe(items)
    items = news.select(items, state, dt.datetime.now(dt.timezone.utc), hours=hours, show_all=True)
    try:
        _jsave(NEWS_STATE, {"ciks": state.get("ciks", {})})
    except Exception:
        pass
    out = {}
    for it in items:
        for t in it.get("touches") or [it["ticker"]]:
            if t not in watch:
                continue
            o = out.setdefault(t, {"headlines": [], "filings": [], "tags": [], "critical": []})
            for tg in it.get("tags") or []:
                if tg not in o["tags"]:
                    o["tags"].append(tg)
            when = it["time"].astimezone(dt.timezone(dt.timedelta(hours=7))).strftime("%d/%m %H:%M") if it["time"] else ""
            if it["kind"] == "filing":
                crit = [CRITICAL_8K[i] for i in it.get("items", []) if i in CRITICAL_8K]
                o["critical"] += [c for c in crit if c not in o["critical"]]
                if len(o["filings"]) < 3:
                    o["filings"].append({"title": it["title"][:160], "time": when, "link": it["link"],
                                         "important": it.get("important", False)})
            elif len(o["headlines"]) < 3 and it["ticker"] == t:
                o["headlines"].append({"title": it["title"][:160], "time": when, "link": it["link"],
                                       "tags": it.get("tags") or []})
    n_err = len(errors)
    status = "หัวข่าว Yahoo%s · %d หุ้นมีข่าวใน %d ชม." % (
        " + SEC 8-K" if ua else " (SEC 8-K ปิดอยู่: ยังไม่ได้ตั้ง sec_user_agent)", len(out), hours)
    if n_err:
        status += " · ดึงไม่ได้ %d รายการ" % n_err
    return out, status


def news_text(nw, headline=True):
    parts = []
    if nw.get("critical"):
        parts.append("🚨 SEC: " + ", ".join(nw["critical"]))
    tags = [TAG_TH.get(t, t) for t in nw.get("tags", []) if t != "macro"]
    if tags:
        parts.append("ข่าวเรื่อง " + ", ".join(tags[:4]))
    if headline and nw.get("headlines"):
        parts.append("ล่าสุด: " + nw["headlines"][0]["title"][:90])
    return " · ".join(parts)


# ===================================================================== 🏛️ สส.สหรัฐฯ ซื้อขายหุ้น (House PTR)
def congress_trades(cfg, log=print):
    """อัปเดตวันละครั้ง คืน ({ticker: [รายการ]}, สถานะ)"""
    ptr = _load("trader-pelosi/ptr.py")
    if not ptr:
        return {}, "ไม่มีโมดูล สส."
    days = int(cfg.get("congress_days", 45))
    cache = _jload(CONGRESS_CACHE, {"docs": {}, "checked": ""})
    today = dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    note = ""
    if cache.get("checked") != today.isoformat():
        try:
            rows = []
            years = {today.year} | ({today.year - 1} if today.month == 1 else set())
            for y in sorted(years):
                rows += ptr.load_index(y)
            sel = ptr.select(rows, kind="P", since=since)
            todo = [r for r in sel if r["doc_id"] not in cache["docs"]][:int(cfg.get("congress_max_pdfs", 60))]
            no_tool = 0
            for r in todo:
                try:
                    _, rep = ptr._fetch_and_read(r["doc_id"], r["year"], PDF_DIR, filed=r["filed"])
                except Exception as e:
                    log("อ่านรายงาน สส. %s ไม่ได้: %s" % (r["doc_id"], str(e)[:80]))
                    continue
                if rep is None:
                    no_tool += 1
                    continue
                cache["docs"][r["doc_id"]] = {
                    "name": r["name"], "filed": r["filed"],
                    "rows": [{"ticker": x.get("ticker"), "type": x.get("type"), "date": x.get("date"),
                              "amount": x.get("amount"), "owner": x.get("owner"),
                              "option": bool(x.get("option"))} for x in rep.get("rows", []) if x.get("ticker")]}
                time.sleep(0.3)
            if no_tool:
                note = " · ยังไม่ได้ติดตั้ง pdftotext อ่านไม่ได้ %d ฉบับ" % no_tool
            cache["docs"] = {k: v for k, v in cache["docs"].items() if (v.get("filed") or "") >= since}
            cache["checked"] = today.isoformat() if not no_tool else ""
            _jsave(CONGRESS_CACHE, cache)
            # ลบ PDF เก่าที่ไม่ใช้แล้ว
            if os.path.isdir(PDF_DIR):
                for fn in os.listdir(PDF_DIR):
                    if fn[:-4] not in cache["docs"]:
                        try:
                            os.remove(os.path.join(PDF_DIR, fn))
                        except OSError:
                            pass
        except Exception as e:
            note = " · ดึงรายชื่อรายงานไม่ได้ (%s) ใช้ข้อมูลเดิม" % str(e)[:60]
    by = {}
    for doc in cache["docs"].values():
        for x in doc["rows"]:
            t = (x.get("ticker") or "").upper()
            if t:
                by.setdefault(t, []).append({"name": doc["name"], "type": x.get("type") or "", "date": x.get("date"),
                                             "amount": x.get("amount"), "option": x.get("option")})
    for v in by.values():
        v.sort(key=lambda z: z.get("date") or "", reverse=True)
    return by, "รายงาน สส. %d ฉบับใน %d วัน%s" % (len(cache["docs"]), days, note)


def congress_text(rows):
    buys = [r for r in rows if r["type"].startswith("P")]
    sells = [r for r in rows if r["type"].startswith("S")]
    parts = []
    if buys:
        parts.append("ซื้อ %d รายการ (ล่าสุด %s %s)" % (len(buys), buys[0]["name"], buys[0].get("date") or ""))
    if sells:
        parts.append("ขาย %d รายการ" % len(sells))
    return " · ".join(parts)


# ===================================================================== รวมทุกฝ่าย
def run(cands, data, cfg, log=print):
    """เติมผลลงในแต่ละ candidate: c['tm'], c['desk_news'], c['congress'] คืนสถานะสำหรับรายงาน"""
    st = {}
    if cfg.get("desk_time_machine", True):
        n = 0
        for c in cands:
            p = c["pick"]
            if p.get("cat") != "trade" or c["symbol"] not in data:
                continue
            entry = p["zone"][0] if p.get("order") == "buy_stop" else (p["zone"][0] + p["zone"][1]) / 2
            try:
                c["tm"] = time_machine(data[c["symbol"]], entry, p["stop"], p["t1"],
                                       days=int(cfg.get("tm_days", 20)), paths=int(cfg.get("tm_paths", 3000)))
                n += 1 if c["tm"] else 0
            except Exception as e:
                log("Time Machine %s ผิดพลาด: %s" % (c["symbol"], str(e)[:80]))
        st["time_machine"] = "จำลองแผนเทรด %d ตัว (%d เส้นทาง/ตัว)" % (n, int(cfg.get("tm_paths", 3000)))
    syms = sorted({c["symbol"] for c in cands})
    if cfg.get("desk_news", True):
        try:
            names = {c["symbol"]: (c.get("f") or {}).get("shortName") for c in cands}
            nw, st["news"] = news_check(syms, cfg, names=names)
            for c in cands:
                if c["symbol"] in nw:
                    c["desk_news"] = nw[c["symbol"]]
        except Exception as e:
            st["news"] = "ดึงข่าวไม่ได้: " + str(e)[:80]
    if cfg.get("desk_congress", True):
        try:
            cg, st["congress"] = congress_trades(cfg, log)
            for c in cands:
                if c["symbol"] in cg:
                    c["congress"] = cg[c["symbol"]][:6]
        except Exception as e:
            st["congress"] = "ดึงรายงาน สส. ไม่ได้: " + str(e)[:80]
    return st
