#!/usr/bin/env python3
"""Public MOEX ISS quotations. No broker account, no token, no personal information."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

INSTRUMENTS = [
    {"symbol": "SBER", "isin": "RU0009029540", "board": "TQBR", "market": "shares", "price_unit": "RUB/share"},
    {"symbol": "RU000A1092T7", "isin": "RU000A1092T7", "board": "TQCB", "market": "bonds", "price_unit": "% of remaining nominal, excluding accrued coupon"},
]
ROOT = Path(__file__).resolve().parent
OUTFILE = ROOT / "public-quotes" / "latest.json"


def iss_table(raw, key):
    entry = raw.get(key)
    if not isinstance(entry, dict):
        return []
    cols = entry.get("columns", [])
    data = entry.get("data", [])
    return [dict(zip(cols, row)) for row in data]


def request_json(url, attempts=3):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TInvestPublicQuotes/1.0)", "Accept": "application/json"})
    for i in range(attempts):
        try:
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            if i == attempts - 1:
                raise RuntimeError(f"MOEX ISS could not be read: {type(exc).__name__}: {exc}") from exc
            time.sleep(1 + i * 2)


def fetch_one(spec):
    market = spec["market"]
    board = spec["board"]
    symbol = spec["symbol"]
    url = (f"https://iss.moex.com/iss/engines/stock/markets/{market}/boards/{board}/"
           f"securities/{symbol}.json?iss.meta=off&iss.only=securities%2Cmarketdata")
    raw = request_json(url)
    securities = iss_table(raw, "securities")
    marketdata = iss_table(raw, "marketdata")
    if not securities:
        raise RuntimeError("No securities data for " + symbol + " on board " + board)
    md = marketdata[0] if marketdata else {}
    sec = securities[0]
    result = {
        "symbol": symbol,
        "isin": spec["isin"],
        "board": board,
        "price_unit": spec["price_unit"],
        "last": md.get("LAST"),
        "best_bid": md.get("BID"),
        "best_ask": md.get("OFFER"),
        "previous_close": sec.get("PREVPRICE"),
        "market_price": md.get("MARKETPRICE"),
        "market_price_2": md.get("MARKETPRICE2"),
        "number_of_trades": md.get("NUMTRADES"),
        "volume": md.get("VOLUME"),
        "last_trade_time": md.get("TIME"),
        "update_time": md.get("UPDATETIME"),
        "last_change": md.get("LASTCHANGE"),
        "moex_systime": md.get("SYSTIME"),
        "source": url,
        "note": "Market data may be delayed or absent outside a trading session. Bid/ask are not guaranteed executable."
    }
    if market == "bonds":
        result["nominal"] = sec.get("FACEVALUE")
        result["accrued_coupon"] = sec.get("ACCRUEDINT")
        result["currency"] = sec.get("FACEUNIT")
    return result


def main():
    out = {"source": "MOEX ISS public data", "retrieved_utc": datetime.now(timezone.utc).isoformat(), "quotes": [], "errors": []}
    for spec in INSTRUMENTS:
        try:
            q = fetch_one(spec)
            print(f"{q['symbol']}: last={q['last']} {q['price_unit']}, bid={q['best_bid']}, ask={q['best_ask']}")
            out["quotes"].append(q)
        except Exception as e:
            error = f"{spec['symbol']}: {e}"
            print(error, file=sys.stderr)
            out["errors"].append(error)
    if not out["quotes"]:
        return 1
    OUTFILE.parent.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Saved public-quotes/latest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
