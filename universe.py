"""universe.py — รายชื่อหุ้น/ETF สหรัฐฯ ที่บอทสแกน (สภาพคล่องสูง ราคาไม่ปั่นง่าย)

นักเทรดมืออาชีพเทรดเฉพาะหุ้นที่ซื้อขายหนาแน่น: spread แคบ ราคาไม่กระโดดจากข่าวลือ
ไม่มีหุ้นเพนนี (ราคาต่ำกว่า $5) และไม่มี ETF เลเวอเรจ/กลับทิศ (ขาดทุนทบต้นเมื่อถือนาน)
แก้รายชื่อได้ที่ไฟล์นี้ หรือเพิ่มใน config.json: "extra_symbols": {"PLTR": "Technology"}
รูปแบบ: สัญลักษณ์: (กลุ่มอุตสาหกรรม, ประเภท)
"""

_S = {
    "Technology": "AAPL MSFT NVDA AVGO ORCL CRM ADBE AMD QCOM TXN INTC CSCO IBM NOW INTU AMAT MU LRCX KLAC "
                  "ANET PANW CRWD SNPS CDNS MRVL PLTR DELL ASML TSM SHOP UBER",
    "Communication": "GOOGL META NFLX DIS TMUS VZ T CMCSA SPOT",
    "Consumer": "AMZN TSLA HD MCD NKE SBUX LOW BKNG TJX CMG ABNB DPZ",
    "Staples": "WMT COST PG KO PEP PM MDLZ CL",
    "Financials": "JPM BAC WFC GS MS C BLK SCHW AXP V MA PYPL SPGI COIN",
    "Healthcare": "LLY UNH JNJ ABBV MRK PFE TMO ABT AMGN ISRG VRTX GILD NVO",
    "Industrials": "CAT DE GE HON UNP UPS BA LMT RTX ETN",
    "Energy": "XOM CVX COP SLB OXY",
    "Utilities": "NEE DUK SO",
    "RealEstate": "PLD AMT",
    "Materials": "LIN FCX NEM",
}
_ETF = {
    "Index": "SPY QQQ IWM DIA",
    "SectorETF": "XLK XLF XLE XLV XLI XLY XLP XLU SMH",
    "Defensive": "GLD TLT",
}

UNIVERSE = {}
for sec, syms in _S.items():
    for s in syms.split():
        UNIVERSE[s] = (sec, "stock")
for sec, syms in _ETF.items():
    for s in syms.split():
        UNIVERSE[s] = (sec, "etf")

BENCHMARK = "SPY"   # ใช้ดูภาวะตลาดรวมและความแข็งแกร่งเทียบตลาด (relative strength)
