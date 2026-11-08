"""
binance_client.py — ตัวเชื่อม Binance Spot (ใช้แค่ไลบรารีมาตรฐานของ Python)

3 โหมด:
  paper   : ใช้ราคาจริง แต่ซื้อขายจำลองในเครื่อง ไม่ต้องมี API key
  testnet : ส่งคำสั่งจริงไปที่ testnet.binance.vision (เงินปลอม) ต้องมี testnet key
  live    : ส่งคำสั่งจริงไปที่ Binance เงินจริง
"""

import hashlib
import hmac
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

LIVE_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"
DATA_URL = "https://data-api.binance.vision"  # ข้อมูลราคาสาธารณะ
TH_URL = "https://api.binance.th"               # Binance TH (Gulf Binance) สำหรับบัญชีคนไทย


class BinanceError(Exception):
    pass


def _round_down(x, step):
    if step <= 0:
        return x
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(x / step + 1e-9) * step, decimals)


class Binance:
    def __init__(self, mode="paper", api_key="", api_secret="", fee=0.001, paper_balance=1000.0,
                 exchange="binance", quote=None):
        self.mode = mode
        self.exchange = exchange
        self.th = exchange == "binance_th"
        self.key, self.secret = api_key, api_secret
        if self.th:
            if mode == "testnet":
                raise BinanceError("Binance TH ไม่มี testnet ใช้โหมด paper หรือ live")
            self.base = self.data_base = TH_URL
            self.v = "/api/v1"
            self.quote = quote or "THB"
        else:
            self.base = {"live": LIVE_URL, "testnet": TESTNET_URL}.get(mode, LIVE_URL)
            self.data_base = TESTNET_URL if mode == "testnet" else DATA_URL
            self.v = "/api/v3"
            self.quote = quote or "USDT"
        self.fee = fee
        self.time_offset = 0
        self.filters = {}
        # สมุดบัญชีจำลองสำหรับโหมด paper
        self.paper = {self.quote: paper_balance}
        self._oid = 0
        if mode in ("live", "testnet") and not (api_key and api_secret):
            raise BinanceError("โหมด %s ต้องตั้ง BINANCE_API_KEY และ BINANCE_API_SECRET" % mode)

    # ------------------------------------------------------------ http
    def _request(self, method, path, params=None, signed=False, base=None):
        params = dict(params or {})
        headers = {"User-Agent": "ai-trading-company/1.0"}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.time_offset
            params["recvWindow"] = 10000
            q = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            q += "&signature=" + sig
            headers["X-MBX-APIKEY"] = self.key
        else:
            q = urllib.parse.urlencode(params)
        url = (base or self.base) + path
        data = None
        if method == "GET" or method == "DELETE":
            url += ("?" + q) if q else ""
        else:
            data = q.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code in (429, 418):
                    time.sleep(10 * (attempt + 1))
                    continue
                if e.code == 451:
                    raise BinanceError("Binance ปฏิเสธเพราะตำแหน่ง IP (451) ต้องรันจากประเทศที่ Binance อนุญาต")
                raise BinanceError("HTTP %s %s: %s" % (e.code, path, body))
            except urllib.error.URLError as e:
                if attempt == 2:
                    raise BinanceError("เชื่อมต่อไม่ได้: %s" % e)
                time.sleep(3)
        raise BinanceError("ถูกจำกัดความถี่ (rate limit) ซ้ำหลายครั้ง")

    def public(self, path, params=None):
        path = path.replace("/api/v3", self.v)
        try:
            return self._request("GET", path, params, base=self.data_base)
        except BinanceError:
            if self.mode == "testnet" or self.th:
                raise
            return self._request("GET", path, params, base=LIVE_URL)

    def private(self, method, path, params=None):
        return self._request(method, path.replace("/api/v3", self.v), params, signed=True)

    # ------------------------------------------------------------ market data
    def sync_time(self):
        t = self.public("/api/v3/time")["serverTime"]
        self.time_offset = t - int(time.time() * 1000)

    def load_filters(self):
        info = self.public("/api/v3/exchangeInfo")
        for s in info["symbols"]:
            if s.get("status") != "TRADING" or s.get("quoteAsset") != self.quote:
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("LOT_SIZE", {})
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            price = f.get("PRICE_FILTER", {})
            self.filters[s["symbol"]] = {
                "base": s["baseAsset"],
                "step": float(lot.get("stepSize", 0) or 0),
                "min_qty": float(lot.get("minQty", 0) or 0),
                "tick": float(price.get("tickSize", 0) or 0),
                "min_notional": float(notional.get("minNotional") or (5 if self.quote == "USDT" else 100)),
                "oco": bool(s.get("ocoAllowed")) and not self.th,
            }
        return self.filters

    def tickers_24h(self):
        if not self.th:
            return self.public("/api/v3/ticker/24hr")
        out = []  # Binance TH ต้องขอทีละเหรียญ
        for sym in self.filters:
            try:
                out.append(self.public("/api/v3/ticker/24hr", {"symbol": sym}))
            except BinanceError:
                pass
            time.sleep(0.05)
        return out

    def order_book(self, symbol, limit=20):
        """สมุดคำสั่งซื้อขาย: คืน (bids, asks) เป็นลิสต์ [(ราคา, จำนวน)] เรียงจากราคาดีสุด"""
        d = self.public("/api/v3/depth", {"symbol": symbol, "limit": limit})
        d = d.get("data", d) if isinstance(d, dict) else d
        conv = lambda rows: [(float(r[0]), float(r[1])) if isinstance(r, (list, tuple))
                             else (float(r["price"]), float(r.get("qty", r.get("quantity", 0)))) for r in rows]
        return conv(d.get("bids", [])), conv(d.get("asks", []))

    def klines(self, symbol, interval, limit=250):
        return self.public("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def prices(self, symbols=None):
        if self.th:
            out = {}
            for sym in (symbols or list(self.filters)):
                d = self.public("/api/v3/ticker/24hr", {"symbol": sym})
                out[sym] = float(d["lastPrice"])
            return out
        if symbols:
            data = self.public("/api/v3/ticker/price", {"symbols": json.dumps(list(symbols), separators=(",", ":"))})
        else:
            data = self.public("/api/v3/ticker/price")
        return {d["symbol"]: float(d["price"]) for d in data}

    # ------------------------------------------------------------ account
    def balances(self):
        if self.mode == "paper":
            return dict(self.paper)
        acc = self.private("GET", "/api/v1/accountV2" if self.th else "/api/v3/account")
        return {b["asset"]: float(b["free"]) for b in _find_balances(acc) if float(b.get("free", 0)) > 0}

    def usdt_free(self):
        """เงินสดในสกุลที่ใช้เทรด (USDT สำหรับ binance.com, THB สำหรับ Binance TH)"""
        return self.balances().get(self.quote, 0.0)

    # ------------------------------------------------------------ orders
    def market_buy(self, symbol, usdt):
        """ซื้อด้วยเงินจำนวนหนึ่ง คืนค่า (จำนวนเหรียญสุทธิหลังหักค่าธรรมเนียม, ราคาเฉลี่ย, order id, เงินที่จ่ายจริง)"""
        f = self.filters[symbol]
        if self.mode == "paper":
            px = self.prices([symbol])[symbol]
            if self.paper.get(self.quote, 0) < usdt:
                raise BinanceError("เงิน %s จำลองไม่พอ" % self.quote)
            qty = _round_down(usdt / px * (1 - self.fee), f["step"])
            self.paper[self.quote] -= usdt
            self.paper[f["base"]] = self.paper.get(f["base"], 0) + qty
            self._oid += 1
            return qty, px, "paper-%d" % self._oid, usdt
        if self.th:
            # Binance TH ไม่รองรับ quoteOrderQty: คำนวณจำนวนเหรียญจากราคาล่าสุด เผื่อราคาขยับ 1%
            px = self.prices([symbol])[symbol]
            want = _round_down(usdt / px * 0.99, f["step"])
            if want < f["min_qty"] or want * px < f["min_notional"]:
                raise BinanceError("จำนวนเงินน้อยกว่าขั้นต่ำของ %s" % symbol)
            r = self.private("POST", "/api/v3/order", {
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quantity": self._fmt(want, f["step"]), "newOrderRespType": "FULL"})
            r = _order_data(r)
            qty = float(r.get("executedQty") or want)
            spent = float(r.get("cummulativeQuoteQty") or qty * px)
        else:
            usdt = math.floor(usdt * 100) / 100
            r = self.private("POST", "/api/v3/order", {
                "symbol": symbol, "side": "BUY", "type": "MARKET",
                "quoteOrderQty": "%.2f" % usdt, "newOrderRespType": "FULL"})
            qty = float(r["executedQty"])
            spent = float(r["cummulativeQuoteQty"])
        fee_in_base = sum(float(x["commission"]) for x in r.get("fills", []) if x["commissionAsset"] == f["base"])
        net = _round_down(qty - fee_in_base, f["step"])
        return net, (spent / qty if qty else 0), str(r.get("orderId", "")), spent

    def market_sell(self, symbol, qty):
        f = self.filters[symbol]
        qty = _round_down(qty, f["step"])
        if self.mode == "paper":
            px = self.prices([symbol])[symbol]
            have = self.paper.get(f["base"], 0)
            qty = min(qty, have)
            self.paper[f["base"]] = have - qty
            got = qty * px * (1 - self.fee)
            self.paper[self.quote] = self.paper.get(self.quote, 0) + got
            return got, px
        if self.th:  # ใช้ยอดคงเหลือจริง กันขายเกินที่มี (ค่าธรรมเนียมอาจถูกหักเป็นเหรียญ)
            have = self.balances().get(f["base"], 0.0)
            qty = _round_down(min(qty, have), f["step"])
        r = _order_data(self.private("POST", "/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": self._fmt(qty, f["step"]), "newOrderRespType": "FULL"}))
        q = float(r.get("executedQty") or qty) or 1
        got = float(r.get("cummulativeQuoteQty") or 0)
        if not got:
            got = q * self.prices([symbol])[symbol]
        fee_usdt = sum(float(x["commission"]) for x in r.get("fills", []) if x["commissionAsset"] == self.quote)
        return got - fee_usdt, got / q

    def place_oco_exit(self, symbol, qty, tp_price, sl_price):
        """ตั้งคำสั่งขายล่วงหน้าบนกระดาน: ทำกำไร (LIMIT_MAKER) + ตัดขาดทุน (STOP_LOSS_LIMIT)
        ข้อดี: ถึงโปรแกรมปิด/เน็ตหลุด คำสั่งก็ยังอยู่บน Binance"""
        if self.mode == "paper":
            return None
        f = self.filters[symbol]
        if not f["oco"]:
            return None
        qty = _round_down(qty, f["step"])
        tp = _round_down(tp_price, f["tick"])
        sl_stop = _round_down(sl_price, f["tick"])
        sl_limit = _round_down(sl_price * 0.995, f["tick"])  # เผื่อราคาไหลตอนหลุด stop
        r = self._request("POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": self._fmt(qty, f["step"]),
            "aboveType": "LIMIT_MAKER", "abovePrice": self._fmt(tp, f["tick"]),
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": self._fmt(sl_stop, f["tick"]),
            "belowPrice": self._fmt(sl_limit, f["tick"]), "belowTimeInForce": "GTC"}, signed=True)
        return str(r["orderListId"])

    def oco_status(self, list_id):
        """คืน 'open' ถ้ายังรออยู่, 'done' ถ้าขายไปแล้ว (โดน TP หรือ SL)"""
        r = self._request("GET", "/api/v3/orderList", {"orderListId": list_id}, signed=True)
        return "done" if r.get("listOrderStatus") == "ALL_DONE" else "open"

    def oco_fill(self, list_id):
        """ราคาและเงินที่ได้จริงจากคำสั่ง OCO ที่ขายไปแล้ว"""
        r = self._request("GET", "/api/v3/orderList", {"orderListId": list_id}, signed=True)
        sym = r["symbol"]
        for o in r["orders"]:
            od = self._request("GET", "/api/v3/order", {"symbol": sym, "orderId": o["orderId"]}, signed=True)
            if od["status"] == "FILLED":
                got = float(od["cummulativeQuoteQty"])
                q = float(od["executedQty"]) or 1
                return got * (1 - self.fee), got / q, od["type"]
        return 0.0, 0.0, "UNKNOWN"

    def cancel_oco(self, symbol, list_id):
        if self.mode == "paper" or not list_id:
            return
        self._request("DELETE", "/api/v3/orderList", {"symbol": symbol, "orderListId": list_id}, signed=True)

    def list_markets(self):
        """สรุปคู่เหรียญทั้งหมดที่ API ของกระดานเปิดให้ แยกตามสกุลเงินที่ใช้ซื้อและสถานะ (ไม่ซื้อขาย)"""
        info = self.public("/api/v3/exchangeInfo")
        groups = {}
        for s in info.get("symbols", []):
            k = "%s | %s" % (s.get("quoteAsset", "?"), s.get("status", "?"))
            groups.setdefault(k, []).append(s.get("baseAsset", s.get("symbol", "?")))
        out = {"total": len(info.get("symbols", []))}
        for k in sorted(groups, key=lambda k: -len(groups[k])):
            out[k] = "%d: %s" % (len(groups[k]), " ".join(sorted(groups[k])))
        sample = (info.get("symbols") or [{}])[0]
        out["fields"] = " ".join(sorted(sample))  # ช่องข้อมูลที่ API ส่งมา ไว้ดูว่ามีแยกตลาดไหม
        return out

    def check(self):
        """ตรวจการเชื่อมต่อแบบไม่ส่งคำสั่งซื้อขาย"""
        self.sync_time()
        self.load_filters()
        out = {"exchange": self.exchange, "mode": self.mode, "quote": self.quote, "symbols": len(self.filters)}
        btc = "BTC" + self.quote
        if btc in self.filters:
            out["btc_price"] = self.prices([btc])[btc]
            out["btc_filters"] = self.filters[btc]
            try:
                bids, asks = self.order_book(btc, 5)
                out["order_book"] = "ใช้ได้ (spread %.3f%%)" % ((asks[0][0] / bids[0][0] - 1) * 100)
            except Exception as e:
                out["order_book"] = "ใช้ไม่ได้: %s" % e
        if self.key and self.secret:  # มี key: ลองอ่านยอดเงินจริง (ไม่ซื้อขาย)
            acc = self.private("GET", "/api/v1/accountV2" if self.th else "/api/v3/account")
            bal = {b["asset"]: float(b["free"]) for b in _find_balances(acc) if float(b.get("free", 0)) > 0}
            out["balance_" + self.quote] = bal.get(self.quote, 0.0)
            out["assets"] = sorted(bal)[:20]
        return out

    @staticmethod
    def _fmt(x, step):
        if step <= 0 or step >= 1:
            return ("%f" % x).rstrip("0").rstrip(".")
        d = max(0, -int(math.floor(math.log10(step))))
        return ("%.*f" % (d, x))


def _find_balances(obj):
    """หา list ของ {asset, free} ในคำตอบจาก API (รูปแบบของแต่ละกระดานต่างกัน)"""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "asset" in obj[0]:
        return obj
    if isinstance(obj, dict):
        for k in ("balances", "data", "result"):
            if k in obj:
                r = _find_balances(obj[k])
                if r:
                    return r
        for v in obj.values():
            r = _find_balances(v)
            if r:
                return r
    return []


def _order_data(r):
    """บางกระดานห่อผลคำสั่งไว้ใน data"""
    if isinstance(r, dict) and "executedQty" not in r and isinstance(r.get("data"), dict):
        return r["data"]
    return r
