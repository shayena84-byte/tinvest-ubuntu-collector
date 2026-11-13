#!/usr/bin/env python3
"""Local, read-only market-quality dashboard for the T-Invest Sandbox SQLite DB.

No trading, API token, proxy, OpenAI, accounts, external JS/CSS, or writes.
Accessible on localhost only; use an SSH tunnel from a trusted workstation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import statistics
import market_quality
import sys
from urllib.parse import parse_qs, quote, urlsplit

DEFAULT_DB = Path.home() / ".local" / "share" / "tinvest-market" / "market.sqlite3"
WEB_DIR = Path(__file__).resolve().parent
ALLOWED_HOURS = frozenset((6,24,72,168))
MAX_HISTORY_ROWS = 18000


def db_readonly(db_path=DEFAULT_DB):
    if not db_path.is_file():
        raise FileNotFoundError("Market Recorder DB not found")
    uri="file:" + quote(str(db_path.resolve()),safe="/") + "?mode=ro"
    conn=sqlite3.connect(uri,uri=True,timeout=8)
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def dashboard_data(db_path=DEFAULT_DB,hours=24,now=None):
    """Returns only public market metadata/market measurements, NEVER account information."""
    if hours not in ALLOWED_HOURS:
        raise ValueError("Allowed hours: 6, 24, 72, 168")
    now=now or datetime.now(timezone.utc)
    cutoff=(now-timedelta(hours=hours)).isoformat(timespec="seconds")
    with db_readonly(db_path) as db:
        instruments=[dict(r) for r in db.execute(
            "SELECT uid,ticker,isin,board,name,kind FROM instruments ORDER BY ticker,board LIMIT 30"
        ).fetchall()]
        rows=[dict(r) for r in db.execute("""
            SELECT i.ticker,i.board,i.name,i.isin,s.slot_utc,s.collected_utc,
              s.last_price,s.last_trade_utc,s.book_time_utc,s.best_bid,s.best_ask,
              s.spread_pct,s.bid_top_lots,s.ask_top_lots,s.bids_total,s.asks_total,
              s.book_stale_seconds,s.last_stale_seconds,s.quality_status,s.quality_reason
            FROM snapshots s JOIN instruments i ON i.uid=s.uid
            WHERE s.collected_utc>=?
            ORDER BY s.collected_utc DESC LIMIT ?
        """,(cutoff,MAX_HISTORY_ROWS)).fetchall()]
        anomalies=[dict(r) for r in db.execute("""
            SELECT i.ticker,i.board,s.collected_utc,a.status,a.reason,
                   s.best_bid,s.best_ask,s.book_stale_seconds
            FROM anomalies a JOIN snapshots s ON s.id=a.snapshot_id
            JOIN instruments i ON i.uid=s.uid
            WHERE a.detected_utc>=?
            ORDER BY a.detected_utc DESC LIMIT 120
        """,(cutoff,)).fetchall()]
    groups={}
    for inst in instruments:
        key=inst["ticker"]+"@"+inst["board"]
        groups[key]={"symbol":inst["ticker"],"board":inst["board"],
                     "name":inst["name"],"isin":inst["isin"],"data":[]}
    for row in reversed(rows):
        key=row["ticker"]+"@"+row["board"]
        if key not in groups:
            groups[key]={"symbol":row["ticker"],"board":row["board"],
                         "name":row["name"],"isin":row["isin"],"data":[]}
        groups[key]["data"].append({k:v for k,v in row.items() if k not in (
            "ticker","board","name","isin")})
    summaries=[]
    for group in groups.values():
        data=group["data"]
        statuses={key:sum(r["quality_status"]==key for r in data)
                  for key in ("valid","stale","unverified","invalid")}
        latest=data[-1] if data else None
        good=[r for r in data if r["quality_status"]=="valid"]
        spreads=[r["spread_pct"] for r in good if r["spread_pct"] is not None]
        if len(good)>=2:
            first,last=good[0].get("last_price"),good[-1].get("last_price")
            try:
                change=round((float(last)/float(first)-1)*100,3) if first and last else None
            except (ValueError,TypeError,ZeroDivisionError):
                change=None
        else:
            change=None
        summaries.append({
            "symbol":group["symbol"],"board":group["board"],"name":group["name"],
            "isin":group["isin"],"measurements":len(data),"statuses":statuses,
            "last":latest,"last_confirmed":good[-1] if good else None,
            "median_valid_spread_pct":round(statistics.median(spreads),4) if spreads else None,
            "validated_price_change_pct":change,
        })
    return {"generated_utc":now.isoformat(timespec="seconds"),"hours":hours,
            "source":"local read-only SQLite, T-Invest Sandbox MarketDataService",
            "quality_rules":{"valid":"Two sides, bid<ask, orderbook timestamp <=15m old",
                             "stale":"Orderbook timestamp older than 15m",
                             "unverified":"Missing/invalid/future orderbook timestamp or missing side",
                             "invalid":"Crossed or locked book (bid >= ask)"},
            "summary":summaries,"history":groups,"anomalies":anomalies,
            "truncated":len(rows)>=MAX_HISTORY_ROWS}


def simulate_depth(levels, requested_lots):
    """Walk published levels; return quote-unit weighted average and depth shortage."""
    if not isinstance(requested_lots,int) or not 1<=requested_lots<=10000:
        raise ValueError("Invalid quantity")
    if not levels:
        return {"requested_lots":requested_lots,"filled_lots":0,
                "unfilled_lots":requested_lots,"average_quote":None,
                "worst_quote":None,"estimated":True}
    from decimal import Decimal
    remaining=requested_lots
    notion=Decimal(0)
    last=None
    for entry in levels:
        available=int(entry["lots"])
        if available<0: continue
        qty=min(remaining,available)
        notion+=Decimal(str(entry["price"]))*qty
        remaining-=qty
        if qty:last=Decimal(str(entry["price"]))
        if not remaining:break
    filled=requested_lots-remaining
    return {"requested_lots":requested_lots,"filled_lots":filled,
            "unfilled_lots":remaining,
            "average_quote":str((notion/filled).quantize(Decimal("0.000001"))) if filled else None,
            "worst_quote":str(last) if last is not None else None,
            "estimated":True}


def depth_data(db_path=DEFAULT_DB, symbol="SBER", board="TQBR", hours=24, now=None):
    """Read full 10-level snapshot and historical top-10 order imbalance.

    No account details. Prices are stored in instrument quote units.
    Does not infer nominal value, RUB cash amounts, or true fill guarantees.
    """
    if hours not in ALLOWED_HOURS:
        raise ValueError("Allowed hours")
    if not symbol.isalnum() or not board.isalnum() or len(symbol)>20 or len(board)>10:
        raise ValueError("Invalid instrument")
    now=now or datetime.now(timezone.utc)
    boundary=(now-timedelta(hours=hours)).isoformat(timespec="seconds")
    with db_readonly(db_path) as db:
        ident=db.execute(
           "SELECT uid,ticker,board,kind FROM instruments WHERE ticker=? AND board=?",
           (symbol,board)).fetchone()
        if ident is None:
            return {"instrument":symbol+"@"+board,"snapshot":None,"series":[],
                    "simulation":{},"error":"not_found"}
        snaps=[dict(r) for r in db.execute(
           """SELECT id, slot_utc, collected_utc, quality_status, quality_reason,
                     best_bid, best_ask, book_time_utc, bids_total, asks_total,
                     spread_pct
              FROM snapshots WHERE uid=? AND collected_utc>=?
              ORDER BY collected_utc DESC LIMIT 2016""",
           (ident["uid"],boundary)).fetchall()]
        if not snaps:
            return {"instrument":symbol+"@"+board,"snapshot":None,"series":[],
                    "simulation":{},"error":"empty"}
        latest=snaps[0]
        sides={}
        for side in ("bid","ask"):
            sides[side]=[dict(r) for r in db.execute(
                 """SELECT level,price,lots FROM book_levels
                    WHERE snapshot_id=? AND side=? ORDER BY level LIMIT 20""",
                 (latest["id"],side)).fetchall()]
    col_time=datetime.fromisoformat(latest["collected_utc"])
    # The simulated execution is informative only on fresh, internally consistent snapshots.
    usable=(latest["quality_status"]=="valid" and
            market_quality.freshness(latest["collected_utc"], latest["book_time_utc"], now)[0])
    series=[]
    for row in reversed(snaps):
        b,a=row["bids_total"],row["asks_total"]
        denom=b+a
        series.append({"time":row["collected_utc"],"quality":row["quality_status"],
                       "bid_lots":b,"ask_lots":a,
                       "imbalance":round((b-a)/denom,5) if denom else None})
    scenarios={}
    for quantity in (1,10,20,50,100):
        scenarios[str(quantity)]={
            "sell":simulate_depth(sides["bid"],quantity) if usable else None,
            "buy":simulate_depth(sides["ask"],quantity) if usable else None
        }
    return {"instrument":symbol+"@"+board,"kind":ident["kind"],
            "quote_unit":"% of residual nominal (without accrued interest)" if ident["kind"]=="bond" else "RUB/share",
            "snapshot":{k:v for k,v in latest.items() if k!="id"},
            "levels":sides,"series":series,"simulation":scenarios,
            "executable_quality":usable,
            "warning":"Simulation consumes displayed depth only. No tax, coupons, fees, slippage, hidden liquidity or market impact. Sandbox execution differs from exchange."}


class LocalHandler(BaseHTTPRequestHandler):
    server_version="SandboxMarketDashboard/1.0"

    def log_message(self,fmt,*args):
        sys.stderr.write("%s - %s\n"%(self.address_string(),fmt%args))

    def _headers(self,code,type_,length):
        self.send_response(code)
        self.send_header("Content-Type",type_)
        self.send_header("Content-Length",str(length))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Content-Security-Policy",
          "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
          "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
          "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()

    def send_data(self,code,mime,body):
        self._headers(code,mime,len(body))
        self.wfile.write(body)

    def do_GET(self):
        p=urlsplit(self.path)
        if p.path in ("/api/system", "/api/trading"):
            try:
                if p.path == "/api/system":
                    from system_status import report
                    payload = report(self.server.database_path)
                else:
                    from trade_ledger import summary
                    limit = int(parse_qs(p.query).get("limit", ["30"])[0])
                    payload = summary(limit=limit)
                self.send_data(200, "application/json; charset=utf-8",
                               json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except (ValueError, OSError, sqlite3.Error):
                self.send_data(503, "application/json; charset=utf-8", b'{"error":"status_unavailable"}')
            return
        if p.path=="/":
            content=WEB_DIR/"dashboard.html"
            if not content.is_file():
                self.send_data(503,"text/plain; charset=utf-8",b"dashboard.html not installed")
                return
            self.send_data(200,"text/html; charset=utf-8",content.read_bytes())
            return
        if p.path=="/dashboard.js":
            content=WEB_DIR/"dashboard.js"
            if not content.is_file():
                self.send_data(503,"text/plain; charset=utf-8",b"dashboard.js not installed")
                return
            self.send_data(200,"text/javascript; charset=utf-8",content.read_bytes())
            return
        if p.path=="/api/book":
            try:
                q=parse_qs(p.query)
                symbol=q.get("symbol",["SBER"])[0]
                board=q.get("board",["TQBR"])[0]
                hours=int(q.get("hours",["24"])[0])
                payload=depth_data(self.server.database_path,symbol,board,hours)
                self.send_data(200,"application/json; charset=utf-8",
                    json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8"))
            except (ValueError,FileNotFoundError,sqlite3.Error):
                self.send_data(400,"application/json; charset=utf-8",
                    b'{"error":"invalid_request"}')
            return
        if p.path=="/api/market":
            try:
                raw=parse_qs(p.query).get("hours",["24"])[0]
                hours=int(raw)
                payload=dashboard_data(self.server.database_path,hours)
                self.send_data(200,"application/json; charset=utf-8",
                  json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8"))
            except (ValueError,FileNotFoundError,sqlite3.Error) as e:
                self.send_data(503,"application/json; charset=utf-8",
                  json.dumps({"error":type(e).__name__,
                              "message":"База недоступна либо неверный период"},ensure_ascii=False).encode("utf-8"))
            return
        self.send_data(404,"text/plain; charset=utf-8",b"Not found")

    def do_POST(self):
        self.send_data(405,"text/plain; charset=utf-8",b"Read-only dashboard")

    def do_PUT(self):
        self.do_POST()

    def do_DELETE(self):
        self.do_POST()


def main():
    p=argparse.ArgumentParser(description="Local-only read-only dashboard for T-Invest Sandbox market data")
    p.add_argument("--port",type=int,default=8787)
    p.add_argument("--db",type=Path,default=DEFAULT_DB)
    args=p.parse_args()
    if not 1024<=args.port<=65535:
        raise SystemExit("Port must be 1024..65535")
    # Intentionally bind to loopback only. No option to expose over the network.
    server=ThreadingHTTPServer(("127.0.0.1",args.port),LocalHandler)
    server.database_path=args.db
    print(f"Read-only market dashboard: http://127.0.0.1:{args.port} "
          "(SSH tunnel required for remote access)",flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__=="__main__":
    raise SystemExit(main())
