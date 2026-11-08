"""
webull_client.py — ตัวเชื่อม Webull OpenAPI (บัญชี Webull Thailand, หุ้น/ETF สหรัฐฯ)

ใช้ SDK ทางการของ Webull: pip install webull-openapi-python-sdk (install.sh ติดตั้งให้)
หน้าตาฟังก์ชันเหมือน binance_client.py ของบอทคริปโต เพื่อให้แผนกอื่นใช้โค้ดชุดเดียวกันได้

2 โหมด:
  paper : ใช้ราคาจริงจาก Webull แต่ซื้อขายจำลองในเครื่อง (ต้องมี App Key เพื่ออ่านราคา แต่ไม่ส่งคำสั่ง)
  live  : ส่งคำสั่งจริงเข้าบัญชี Webull (ทุกคำสั่งผ่าน preview_order ก่อน ถ้า Webull ไม่ผ่านจะไม่ส่ง)
"""

import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # Python เก่า: ใช้ UTC-5 (คลาดเคลื่อน 1 ชม. ช่วง daylight saving)
    NY = timezone(timedelta(hours=-5))

HERE = os.path.dirname(os.path.abspath(__file__))

TF = {"5m": "M5", "15m": "M15", "30m": "M30", "1h": "M60", "4h": "M240", "1d": "D", "1w": "W"}

# วันหยุดตลาดหุ้นสหรัฐฯ (NYSE/Nasdaq) — เพิ่มได้ด้วย "extra_holidays" ใน config.json
HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}
HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-11-26"}  # ปิด 13:00 เวลานิวยอร์ก


class WebullError(Exception):
    pass


def _round_down(x, dp):
    f = 10 ** dp
    return math.floor(x * f + 1e-9) / f


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _find(obj, names):
    """หาค่าตัวแรกที่ชื่อตรงกับ names ในคำตอบ API (ซ้อนกี่ชั้นก็ได้)"""
    if isinstance(obj, dict):
        for n in names:
            if obj.get(n) not in (None, ""):
                return obj[n]
        for v in obj.values():
            r = _find(v, names)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, names)
            if r is not None:
                return r
    return None


# ===================================================================== เวลาตลาด
def ny_now():
    return datetime.now(NY)


def is_trading_day(d, extra=()):
    s = d.strftime("%Y-%m-%d")
    return d.weekday() < 5 and s not in HOLIDAYS and s not in extra


def session_times(d):
    close = (13, 0) if d.strftime("%Y-%m-%d") in HALF_DAYS else (16, 0)
    o = d.replace(hour=9, minute=30, second=0, microsecond=0)
    c = d.replace(hour=close[0], minute=close[1], second=0, microsecond=0)
    return o, c


def market_clock(extra=()):
    """สถานะตลาดตอนนี้: open, mins_since_open, mins_to_close, next_open (datetime NY)"""
    now = ny_now()
    out = {"open": False, "mins_since_open": None, "mins_to_close": None}
    if is_trading_day(now, extra):
        o, c = session_times(now)
        if o <= now < c:
            out.update(open=True, mins_since_open=(now - o).total_seconds() / 60, mins_to_close=(c - now).total_seconds() / 60)
    d = now
    for _ in range(10):
        if is_trading_day(d, extra):
            o, _c = session_times(d)
            if o > now:
                out["next_open"] = o
                break
        d = (d + timedelta(days=1)).replace(hour=0, minute=0)
    return out


def next_settle_ts(extra=()):
    """เงินจากการขายในบัญชีเงินสด ใช้ซื้อใหม่ได้หลัง T+1 (วันทำการถัดไป)"""
    d = ny_now() + timedelta(days=1)
    while not is_trading_day(d, extra):
        d += timedelta(days=1)
    return session_times(d)[0].timestamp()


# ===================================================================== client
class Webull:
    th = False  # ให้โค้ดที่ใช้ร่วมกับบอทคริปโตไม่เข้าเส้นทางของ Binance TH

    def __init__(self, mode="paper", app_key="", app_secret="", fee=0.001, paper_balance=1000.0,
                 region="th", account_id="", fractional=True, extra_holidays=()):
        if mode not in ("paper", "live"):
            raise WebullError("Webull มีโหมด paper กับ live เท่านั้น (ไม่มี testnet)")
        if not (app_key and app_secret):
            raise WebullError("ต้องตั้ง WEBULL_APP_KEY และ WEBULL_APP_SECRET (ขอได้ที่ webull.co.th/en/open-api → Get API Key)")
        self.mode, self.fee, self.quote = mode, fee, "USD"
        self.fractional = fractional
        self.extra_holidays = set(extra_holidays or ())
        self.paper = {"USD": paper_balance, "_unsettled": []}
        self.filters = {}
        self._oid = 0
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
            from webull.trade.trade_client import TradeClient
        except ImportError:
            raise WebullError("ยังไม่ได้ติดตั้ง SDK: pip install webull-openapi-python-sdk (หรือรัน install.sh ใหม่)")
        self.api = ApiClient(app_key, app_secret, region)
        tok = os.path.join(HERE, "conf")
        os.makedirs(tok, exist_ok=True)
        try:
            self.api.set_token_dir(tok)  # เก็บ token ยืนยันตัวตน 2 ชั้น (กดยืนยันในแอป Webull ครั้งแรก)
        except Exception:
            pass
        self.data = DataClient(self.api)
        self.trade = TradeClient(self.api)
        self.account_id = account_id

    # ------------------------------------------------------------ http
    def _call(self, fn, *a, **kw):
        last = None
        for attempt in range(3):
            try:
                res = fn(*a, **kw)
            except Exception as e:  # เน็ตหลุด/หมดเวลา
                last = e
                time.sleep(3 * (attempt + 1))
                continue
            code = getattr(res, "status_code", 200)
            if code == 200:
                try:
                    return res.json()
                except Exception:
                    return {}
            body = getattr(res, "text", "")
            if code == 429:
                time.sleep(5 * (attempt + 1))
                last = WebullError("ถูกจำกัดความถี่ (429)")
                continue
            if code in (401, 403):
                raise WebullError("Webull ไม่อนุญาต (%s): ตรวจ App Key/Secret, สิทธิ์ OpenAPI และการยืนยัน 2 ชั้นในแอป · %s" % (code, body[:200]))
            raise WebullError("HTTP %s: %s" % (code, body[:300]))
        raise WebullError("เชื่อมต่อ Webull ไม่ได้: %s" % last)

    # ------------------------------------------------------------ setup
    def sync_time(self):
        pass

    def ensure_account(self):
        if self.account_id:
            return self.account_id
        accts = self._call(self.trade.account_v2.get_account_list)
        rows = accts if isinstance(accts, list) else (accts.get("data") or accts.get("accounts") or [])
        if not rows:
            raise WebullError("ไม่พบบัญชีเทรดใน Webull")
        cash = [a for a in rows if "CASH" in str(a.get("account_type", "")).upper()]
        self.account_id = str((cash or rows)[0].get("account_id"))
        return self.account_id

    def load_filters(self, universe=None):
        """รายชื่อหุ้นที่บอทเทรดได้ (มาจาก universe.py) เก็บรูปแบบเดียวกับ filters ของ Binance"""
        from universe import UNIVERSE
        for sym, (sector, kind) in (universe or UNIVERSE).items():
            self.filters[sym] = {"base": sym, "sector": sector, "kind": kind, "step": 0.00001 if self.fractional else 1,
                                 "min_qty": 0.00001 if self.fractional else 1, "tick": 0.01, "min_notional": 5.0, "oco": False}
        return self.filters

    # ------------------------------------------------------------ market data
    def snapshots(self, symbols):
        out = {}
        symbols = list(symbols)
        for i in range(0, len(symbols), 100):
            part = symbols[i:i + 100]
            r = self._call(self.data.market_data.get_snapshot, ",".join(part), "US_STOCK", False, False)
            rows = r if isinstance(r, list) else (r.get("data") or r.get("result") or [])
            for d in rows:
                if d.get("symbol"):
                    out[d["symbol"]] = d
        return out

    def tickers_24h(self):
        """แปลง snapshot ให้มีชื่อช่องแบบ Binance (lastPrice, quoteVolume, priceChangePercent, bidPrice, askPrice)"""
        out = []
        for s, d in self.snapshots(self.filters).items():
            px = _f(d.get("price") or d.get("close"))
            out.append(dict(d, symbol=s, lastPrice=px, quoteVolume=px * _f(d.get("volume")),
                            priceChangePercent=_f(d.get("change_ratio")) * 100,
                            bidPrice=_f(d.get("bid")), askPrice=_f(d.get("ask"))))
        return out

    def klines_many(self, symbols, interval, limit=250):
        """แท่งเทียนหลายตัวพร้อมกัน (ทีละ 20) คืน {symbol: [[t, o, h, l, c, v], ...] เก่า→ใหม่}"""
        ts = TF.get(interval, interval)
        out = {}
        symbols = list(symbols)
        for i in range(0, len(symbols), 20):
            r = self._call(self.data.market_data.get_batch_history_bar, symbols[i:i + 20], "US_STOCK", ts, str(min(limit, 1200)))
            rows = r.get("result", r) if isinstance(r, dict) else r
            for item in rows or []:
                bars = []
                for b in reversed(item.get("result") or []):  # API ส่งใหม่→เก่า
                    bars.append([b.get("time"), _f(b.get("open")), _f(b.get("high")), _f(b.get("low")),
                                 _f(b.get("close")), _f(b.get("volume"))])
                out[item.get("symbol")] = bars
            time.sleep(0.2)
        return out

    def klines(self, symbol, interval, limit=250):
        return self.klines_many([symbol], interval, limit).get(symbol, [])

    def prices(self, symbols=None):
        return {s: _f(d.get("price") or d.get("close")) for s, d in self.snapshots(symbols or list(self.filters)).items()}

    def order_book(self, symbol, limit=20):
        d = self.snapshots([symbol]).get(symbol, {})
        bid, ask = _f(d.get("bid")), _f(d.get("ask"))
        if bid <= 0 or ask <= 0:
            raise WebullError("ไม่มีราคาเสนอซื้อ/ขาย (ตลาดปิด?)")
        lot = 100  # L1 ของหุ้นสหรัฐฯ แสดงขนาดเป็นล็อตละ 100 หุ้น
        return [(bid, (_f(d.get("bid_size"), 1) or 1) * lot)], [(ask, (_f(d.get("ask_size"), 1) or 1) * lot)]

    def earnings_dates(self, symbol):
        try:
            r = self._call(self.data.fundamentals.get_earnings_calendar, symbol, "US_STOCK")
        except Exception:
            return []
        rows = r if isinstance(r, list) else (r.get("data") or r.get("result") or [])
        return [str(x.get("expected_publish_date") or x.get("publish_date") or "")[:10] for x in rows
                if x.get("expected_publish_date") or x.get("publish_date")]

    # ------------------------------------------------------------ account
    def _settle_paper(self):
        now = time.time()
        keep = []
        for t, amt in self.paper.get("_unsettled", []):
            if t <= now:
                self.paper["USD"] = self.paper.get("USD", 0) + amt
            else:
                keep.append([t, amt])
        self.paper["_unsettled"] = keep

    def balances(self):
        if self.mode == "paper":
            self._settle_paper()
            return {"USD": self.paper.get("USD", 0.0)}
        b = self._call(self.trade.account_v2.get_account_balance, self.ensure_account())
        out = {}
        for a in b.get("account_currency_assets", []) if isinstance(b, dict) else []:
            # บัญชีเงินสด: ใช้ buying_power (เงินที่ใช้ซื้อได้จริงตอนนี้) ไม่ใช้เงินที่ยังไม่ settle
            out[a.get("currency")] = _f(a.get("settled_cash") or a.get("buying_power") or a.get("cash_balance"))
        return out

    def usdt_free(self):
        """เงิน USD ที่ใช้ซื้อได้ (ชื่อฟังก์ชันเดียวกับบอทคริปโต)"""
        return self.balances().get("USD", 0.0)

    def unsettled(self):
        return sum(a for _t, a in self.paper.get("_unsettled", [])) if self.mode == "paper" else 0.0

    def broker_positions(self):
        if self.mode == "paper":
            return {k: v for k, v in self.paper.items() if not k.startswith("_") and k != "USD" and v > 0}
        r = self._call(self.trade.account_v2.get_account_position, self.ensure_account())
        rows = r if isinstance(r, list) else (r.get("data") or r.get("holdings") or [])
        return {p["symbol"]: _f(p.get("quantity")) for p in rows if p.get("symbol") and p.get("currency", "USD") == "USD"}

    # ------------------------------------------------------------ orders
    def _qty(self, q):
        return _round_down(q, 5) if self.fractional else float(math.floor(q))

    def _order(self, symbol, side, qty):
        cid = uuid.uuid4().hex
        order = [{"combo_type": "NORMAL", "client_order_id": cid, "symbol": symbol, "instrument_type": "EQUITY",
                  "market": "US", "order_type": "MARKET", "quantity": ("%.5f" % qty).rstrip("0").rstrip("."),
                  "support_trading_session": "CORE", "side": side, "time_in_force": "DAY", "entrust_type": "QTY"}]
        acct = self.ensure_account()
        # ตรวจกับ Webull ก่อน (ไม่ส่งคำสั่งจริง) ถ้าไม่ผ่าน เช่นเงินไม่พอ/หุ้นซื้อเศษไม่ได้ จะหยุดตรงนี้
        self._call(self.trade.order_v2.preview_order, acct, order)
        self._call(self.trade.order_v2.place_order, acct, order)
        for _ in range(20):  # รอให้คำสั่ง market จับคู่ (ปกติไม่กี่วินาที)
            time.sleep(1.5)
            try:
                d = self._call(self.trade.order_v2.get_order_detail, acct, cid)
            except WebullError:
                continue
            st = str(_find(d, ["status", "order_status"]) or "").upper()
            filled = _f(_find(d, ["filled_quantity", "filled_qty", "executed_quantity"]))
            avg = _f(_find(d, ["filled_price", "avg_filled_price", "average_price", "avg_price"]))
            if st == "FILLED" and filled > 0 and avg > 0:
                return filled, avg, cid
            if st in ("CANCELLED", "CANCELED", "FAILED", "REJECTED", "EXPIRED"):
                if filled > 0 and avg > 0:
                    return filled, avg, cid
                raise WebullError("คำสั่ง %s %s ไม่สำเร็จ (%s)" % (side, symbol, st))
        raise WebullError("คำสั่ง %s %s ยังไม่จับคู่ภายใน 30 วินาที (client_order_id %s) เช็กในแอป Webull" % (side, symbol, cid))

    def market_buy(self, symbol, usd):
        """ซื้อด้วยเงิน usd ดอลลาร์ คืน (จำนวนหุ้น, ราคาเฉลี่ย, order id, เงินที่จ่ายจริง)"""
        px = self.prices([symbol]).get(symbol)
        if not px:
            raise WebullError("ไม่มีราคา %s" % symbol)
        if self.mode == "paper":
            self._settle_paper()
            if self.paper.get("USD", 0) < usd:
                raise WebullError("เงิน USD จำลองที่ settle แล้วไม่พอ")
            qty = self._qty(usd / px / (1 + self.fee))
            if qty <= 0:
                raise WebullError("เงินไม่พอซื้อ %s 1 หุ้น (ปิดหุ้นเศษอยู่)" % symbol)
            spent = qty * px * (1 + self.fee)
            self.paper["USD"] -= spent
            self.paper[symbol] = self.paper.get(symbol, 0) + qty
            self._oid += 1
            return qty, px, "paper-%d" % self._oid, spent
        qty = self._qty(usd / px * 0.995)  # เผื่อราคาขยับระหว่างส่งคำสั่ง
        if qty <= 0:
            raise WebullError("เงินไม่พอซื้อ %s 1 หุ้น (ตั้ง fractional=true เพื่อซื้อหุ้นเศษ)" % symbol)
        filled, avg, cid = self._order(symbol, "BUY", qty)
        return filled, avg, cid, filled * avg * (1 + self.fee)

    def market_sell(self, symbol, qty):
        """ขาย คืน (เงินที่ได้หลังค่าธรรมเนียม, ราคาเฉลี่ย)"""
        if self.mode == "paper":
            px = self.prices([symbol])[symbol]
            have = self.paper.get(symbol, 0)
            qty = min(qty, have)
            self.paper[symbol] = have - qty
            got = qty * px * (1 - self.fee)
            self.paper.setdefault("_unsettled", []).append([next_settle_ts(self.extra_holidays), got])
            return got, px
        have = self.broker_positions().get(symbol, 0.0)
        qty = self._qty(min(qty, have) if have else qty)
        if qty <= 0:
            raise WebullError("ไม่มีหุ้น %s ในบัญชีให้ขาย" % symbol)
        filled, avg, _cid = self._order(symbol, "SELL", qty)
        return filled * avg * (1 - self.fee), avg

    # หุ้นเศษตั้งคำสั่ง stop บนโบรกไม่ได้ทุกตัว: บอทเฝ้าราคาเองเหมือน Binance TH (oco_id = None)
    def place_oco_exit(self, symbol, qty, tp_price, sl_price):
        return None

    def oco_status(self, list_id):
        return "open"

    def oco_fill(self, list_id):
        return 0.0, 0.0, "UNKNOWN"

    def cancel_oco(self, symbol, list_id):
        pass

    def check(self):
        out = {"broker": "Webull Thailand", "mode": self.mode, "symbols": len(self.load_filters())}
        snap = self.snapshots(["SPY", "AAPL"])
        out["SPY"] = _f(snap.get("SPY", {}).get("price"))
        out["AAPL"] = _f(snap.get("AAPL", {}).get("price"))
        out["bars_ok"] = len(self.klines("SPY", "1d", 5)) > 0
        out["market"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in market_clock(self.extra_holidays).items()}
        out["account_id"] = self.ensure_account()
        out["usd_buying_power"] = self.balances().get("USD", 0.0)
        out["positions"] = self.broker_positions()
        return out

    def check_order(self, symbol="SPY", usd=5):
        """ตรวจว่าบัญชีส่งคำสั่งได้ไหม ด้วย preview_order อย่างเดียว (ไม่ซื้อจริง)"""
        px = self.prices([symbol])[symbol]
        qty = self._qty(usd / px)
        order = [{"combo_type": "NORMAL", "client_order_id": uuid.uuid4().hex, "symbol": symbol, "instrument_type": "EQUITY",
                  "market": "US", "order_type": "MARKET", "quantity": ("%.5f" % qty).rstrip("0").rstrip("."),
                  "support_trading_session": "CORE", "side": "BUY", "time_in_force": "DAY", "entrust_type": "QTY"}]
        return self._call(self.trade.order_v2.preview_order, self.ensure_account(), order)
