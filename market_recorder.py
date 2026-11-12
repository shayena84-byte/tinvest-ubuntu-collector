#!/usr/bin/env python3
"""Persist T-Invest Sandbox market quotes and 10-level order books in local SQLite.

READ ONLY: reads only InstrumentsService and MarketDataService via the existing
sandbox.py client. Does not send orders, create accounts, or call OpenAI.
No credentials, account IDs, or token values are stored in SQLite.
Python standard library only; intended to run every 5 minutes via systemd.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any

import sandbox as sandbox_module

c = sandbox_module.c

CONFIG_PATH = Path.home() / ".config" / "tinvest-readonly" / "market-recorder.json"
DB_DIR = Path.home() / ".local" / "share" / "tinvest-market"
DB_PATH = DB_DIR / "market.sqlite3"
EXPORT_DIR = Path.home() / "tinvest-reports" / "market"
READ_METHODS = frozenset({"find", "instrument", "last", "book"})
SCHEMA_VERSION = 2
DEFAULT_CONFIG = {
    "depth": 10,
    "instruments": [
        {"symbol": "SBER", "board": "TQBR", "kind": "share"},
        {"symbol": "RU000A1092T7", "board": "TQCB", "kind": "bond"}
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    uid TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    isin TEXT NOT NULL,
    board TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    currency TEXT,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    UNIQUE(uid, board)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL REFERENCES instruments(uid),
    slot_utc TEXT NOT NULL,
    collected_utc TEXT NOT NULL,
    last_price TEXT,
    last_trade_utc TEXT,
    book_time_utc TEXT,
    best_bid TEXT,
    best_ask TEXT,
    spread_pct REAL,
    bid_top_lots INTEGER NOT NULL DEFAULT 0,
    ask_top_lots INTEGER NOT NULL DEFAULT 0,
    bids_total INTEGER NOT NULL DEFAULT 0,
    asks_total INTEGER NOT NULL DEFAULT 0,
    book_stale_seconds INTEGER,
    last_stale_seconds INTEGER,
    quality_status TEXT NOT NULL DEFAULT 'unverified',
    quality_reason TEXT NOT NULL DEFAULT 'not_checked',
    UNIQUE(uid, slot_utc)
);
CREATE TABLE IF NOT EXISTS book_levels (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK(side IN ('bid','ask')),
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 50),
    price TEXT NOT NULL,
    lots INTEGER NOT NULL CHECK(lots >= 0),
    PRIMARY KEY(snapshot_id, side, level)
);
CREATE TABLE IF NOT EXISTS fetch_errors (
    symbol TEXT NOT NULL,
    board TEXT NOT NULL,
    slot_utc TEXT NOT NULL,
    collected_utc TEXT NOT NULL,
    error_type TEXT NOT NULL,
    safe_message TEXT NOT NULL,
    PRIMARY KEY(symbol, board, slot_utc)
);
CREATE TABLE IF NOT EXISTS anomalies (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('invalid','stale','unverified')),
    reason TEXT NOT NULL,
    detected_utc TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_anomalies_time ON anomalies(detected_utc);
CREATE INDEX IF NOT EXISTS idx_snapshots_uid_time ON snapshots(uid, collected_utc);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(collected_utc);
"""


class RecorderError(Exception):
    pass


class ReadOnlySandboxMarket:
    """Routes only four read methods to the official T-Invest Sandbox hostname."""
    def __init__(self, client):
        if not isinstance(client, sandbox_module.SandboxAPI):
            raise RecorderError("Market recorder accepts only T-Invest Sandbox API clients")
        if client.base != sandbox_module.SANDBOX_BASE:
            raise RecorderError("Production API cannot be used for this recorder")
        self.client = client

    def call(self, method, request):
        if method not in READ_METHODS:
            raise RecorderError("Trading/account-changing method blocked in read-only recorder")
        return self.client.call(method, request)


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    if not value:
        return None
    if not isinstance(value, str):
        raise RecorderError("Timestamp is not a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecorderError("Invalid timestamp returned from exchange") from exc
    if dt.tzinfo is None:
        raise RecorderError("Exchange timestamp has no timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def slot_5min(now):
    dt = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt.replace(minute=(dt.minute // 5) * 5).isoformat(timespec="seconds")


def seconds_old(now, exchange_time):
    if not exchange_time:
        return None
    parsed = datetime.fromisoformat(exchange_time)
    return max(0, int((now - parsed).total_seconds()))


def decimal_quote(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        value = c.val_quote(obj)
    else:
        value = str(obj)
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def clean_levels(raw, descending, depth):
    if not isinstance(raw, list):
        return []
    result = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        price = decimal_quote(row.get("price"))
        try:
            lots = int(row.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if price is None or price <= 0 or lots < 0:
            continue
        result.append((price, lots))
    result.sort(key=lambda x: x[0], reverse=descending)
    return result[:depth]


def spread_percent(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return float((ask - bid) / ((ask + bid) / Decimal(2)) * Decimal(100))


def normalize_payload(instrument, last_resp, book_resp, depth, now):
    uid = instrument["uid"]
    if book_resp.get("instrumentUid") and book_resp["instrumentUid"] != uid:
        raise RecorderError("Order book UID does not match selected instrument")
    last_rows = last_resp.get("lastPrices", [])
    if not isinstance(last_rows, list):
        raise RecorderError("GetLastPrices returned invalid lastPrices")
    last_row = None
    if len(last_rows) == 1:
        last_row = last_rows[0]
    else:
        match = [p for p in last_rows if p.get("instrumentUid") == uid]
        if len(match) == 1:
            last_row = match[0]
    if last_row is not None and last_row.get("instrumentUid") not in (None, "", uid):
        raise RecorderError("Last-price UID does not match selected instrument")
    lp = decimal_quote(last_row.get("price")) if last_row else None
    if lp is not None and lp <= 0:
        lp = None
    last_trade = iso(last_row.get("time")) if last_row else None
    # REST GetOrderBook uses orderbookTs; streaming orderbooks may use time.
    book_time = iso(book_resp.get("orderbookTs") or book_resp.get("time"))
    bids = clean_levels(book_resp.get("bids"), True, depth)
    asks = clean_levels(book_resp.get("asks"), False, depth)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    result = {
        "last_price": str(lp) if lp is not None else None,
        "last_trade_utc": last_trade,
        "book_time_utc": book_time,
        "best_bid": str(best_bid) if best_bid is not None else None,
        "best_ask": str(best_ask) if best_ask is not None else None,
        "spread_pct": spread_percent(best_bid, best_ask),
        "bid_top_lots": bids[0][1] if bids else 0,
        "ask_top_lots": asks[0][1] if asks else 0,
        "bids_total": sum(item[1] for item in bids),
        "asks_total": sum(item[1] for item in asks),
        "book_stale_seconds": seconds_old(now, book_time),
        "last_stale_seconds": seconds_old(now, last_trade),
        "bids": bids,
        "asks": asks,
    }
    quality, reason = classify_snapshot(result, now)
    result["quality_status"] = quality
    result["quality_reason"] = reason
    return result


def classify_snapshot(payload, now):
    """Only 'valid' may feed reliable liquidity statistics or execution estimates."""
    bid = decimal_quote(payload.get("best_bid"))
    ask = decimal_quote(payload.get("best_ask"))
    if bid is not None and ask is not None and bid >= ask:
        return "invalid", "crossed_or_locked_book"
    if bid is None or ask is None or not payload.get("bids") or not payload.get("asks"):
        return "unverified", "missing_book_side_or_price"
    timestamp = payload.get("book_time_utc")
    if not timestamp:
        return "unverified", "orderbook_timestamp_missing"
    try:
        book_at = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return "unverified", "orderbook_timestamp_invalid"
    if book_at - now > timedelta(minutes=2):
        return "unverified", "orderbook_timestamp_in_future"
    age = (now - book_at).total_seconds()
    if age > 900:
        return "stale", "orderbook_older_than_15m"
    # Do not discard illiquid bonds just because the last trade is old.
    return "valid", "ok"


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecorderError(f"Не удалось прочитать конфигурацию {CONFIG_PATH}: {type(exc).__name__}") from exc
    if not isinstance(cfg, dict) or set(cfg) != {"depth", "instruments"}:
        raise RecorderError("Конфигурация должна содержать только depth и instruments")
    depth = cfg["depth"]
    if type(depth) is not int or not 1 <= depth <= 20:
        raise RecorderError("Глубина стакана должна быть числом от 1 до 20")
    entries = cfg["instruments"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
        raise RecorderError("Укажите от 1 до 10 инструментов")
    seen = set()
    for spec in entries:
        if not isinstance(spec, dict) or set(spec) != {"symbol", "board", "kind"}:
            raise RecorderError("У инструмента должны быть symbol, board и kind")
        sym, board, kind = spec["symbol"], spec["board"], spec["kind"]
        if (not isinstance(sym,str) or not sym.isalnum() or len(sym) > 20
                or sym != sym.upper() or not isinstance(board,str)
                or not board.isalnum() or board != board.upper()
                or len(board) > 10 or kind not in ("share", "bond")):
            raise RecorderError("Неверный symbol/board/kind")
        if (sym,board) in seen:
            raise RecorderError("Один инструмент указан дважды")
        seen.add((sym,board))
    return cfg


def connect(db_path=DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path.parent.chmod(0o700)
    # Keep all SQLite sidecar files private too.
    previous = os.umask(0o077)
    try:
        db = sqlite3.connect(str(db_path), timeout=15)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        with db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise RecorderError(f"Неподдерживаемая версия схемы БД: {version}")
            if version == 1:
                # In-place upgrade; preserve all existing snapshots and order-book levels.
                db.execute("ALTER TABLE snapshots ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unverified'")
                db.execute("ALTER TABLE snapshots ADD COLUMN quality_reason TEXT NOT NULL DEFAULT 'historical_unverified'")
                db.execute("""UPDATE snapshots
                              SET quality_status='invalid',
                                  quality_reason='crossed_or_locked_book'
                              WHERE best_bid IS NOT NULL AND best_ask IS NOT NULL
                                AND CAST(best_bid AS REAL)>=CAST(best_ask AS REAL)""")
                db.execute("""UPDATE snapshots
                              SET quality_status='stale',
                                  quality_reason='orderbook_older_than_15m'
                              WHERE quality_status!='invalid' AND book_stale_seconds > 900""")
                db.execute("""INSERT OR IGNORE INTO anomalies
                            (snapshot_id,status,reason,detected_utc)
                            SELECT id,quality_status,quality_reason,collected_utc
                            FROM snapshots WHERE quality_status!='valid'""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_quality ON snapshots(quality_status, collected_utc)")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        os.chmod(db_path, 0o600)
    finally:
        os.umask(previous)
    db.row_factory = sqlite3.Row
    return db


def store_snapshot(db, instrument, row, now):
    slot = slot_5min(now)
    fetched_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    params = (
        instrument["uid"], slot, fetched_at, row["last_price"], row["last_trade_utc"],
        row["book_time_utc"], row["best_bid"], row["best_ask"], row["spread_pct"],
        row["bid_top_lots"], row["ask_top_lots"], row["bids_total"], row["asks_total"],
        row["book_stale_seconds"], row["last_stale_seconds"]
    )
    with db:
        db.execute("""INSERT INTO instruments
            (uid,ticker,isin,board,name,kind,currency,first_seen_utc,last_seen_utc)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
              ticker=excluded.ticker, isin=excluded.isin, board=excluded.board,
              name=excluded.name, kind=excluded.kind,
              currency=excluded.currency, last_seen_utc=excluded.last_seen_utc""",
            (instrument["uid"],instrument["ticker"],instrument["isin"],
             instrument["board"],instrument["name"],instrument["kind"],
             instrument["currency"],fetched_at,fetched_at))
        db.execute("""INSERT INTO snapshots
            (uid,slot_utc,collected_utc,last_price,last_trade_utc,book_time_utc,
             best_bid,best_ask,spread_pct,bid_top_lots,ask_top_lots,
             bids_total,asks_total,book_stale_seconds,last_stale_seconds)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uid,slot_utc) DO UPDATE SET
             collected_utc=excluded.collected_utc,
             last_price=excluded.last_price,
             last_trade_utc=excluded.last_trade_utc,
             book_time_utc=excluded.book_time_utc,
             best_bid=excluded.best_bid, best_ask=excluded.best_ask,
             spread_pct=excluded.spread_pct,
             bid_top_lots=excluded.bid_top_lots,ask_top_lots=excluded.ask_top_lots,
             bids_total=excluded.bids_total,asks_total=excluded.asks_total,
             book_stale_seconds=excluded.book_stale_seconds,
             last_stale_seconds=excluded.last_stale_seconds""", params)
        snap = db.execute("SELECT id FROM snapshots WHERE uid=? AND slot_utc=?",
                          (instrument["uid"], slot)).fetchone()[0]
        db.execute("UPDATE snapshots SET quality_status=?, quality_reason=? WHERE id=?",
                   (row["quality_status"], row["quality_reason"], snap))
        db.execute("DELETE FROM anomalies WHERE snapshot_id=?", (snap,))
        if row["quality_status"] != "valid":
            db.execute("""INSERT INTO anomalies(snapshot_id,status,reason,detected_utc)
                          VALUES (?,?,?,?)""",
                       (snap,row["quality_status"],row["quality_reason"],fetched_at))
        db.execute("DELETE FROM book_levels WHERE snapshot_id=?", (snap,))
        for side, values in (("bid",row["bids"]),("ask",row["asks"])):
            db.executemany("""INSERT INTO book_levels(snapshot_id,side,level,price,lots)
                VALUES (?,?,?,?,?)""",
                [(snap,side,i,str(price),lots) for i,(price,lots) in enumerate(values,1)])
        db.execute("DELETE FROM fetch_errors WHERE symbol=? AND board=? AND slot_utc=?",
                   (instrument["ticker"], instrument["board"],slot))
    return snap


def store_failure(db, spec, now, err):
    # Do not persist raw HTTP bodies or request headers.
    msg = str(err).replace("\r", " ").replace("\n", " ")
    with db:
        db.execute("""INSERT INTO fetch_errors
         (symbol,board,slot_utc,collected_utc,error_type,safe_message)
         VALUES (?,?,?,?,?,?)
         ON CONFLICT(symbol,board,slot_utc) DO UPDATE SET
          collected_utc=excluded.collected_utc,
          error_type=excluded.error_type, safe_message=excluded.safe_message""",
         (spec["symbol"],spec["board"],slot_5min(now),now.isoformat(timespec="seconds"),
          type(err).__name__,msg[:240]))


def resolve(api, spec):
    inst = c.quote_instrument(api,spec["symbol"],board=spec["board"])
    # If the query used a full ISIN, validate it was not resolved to a different security.
    if len(spec["symbol"])==12 and inst.get("isin","").upper()!=spec["symbol"]:
        raise RecorderError("ISIN mismatch")
    if str(inst.get("classCode","")).upper()!=spec["board"]:
        raise RecorderError("Trading board mismatch")
    uid=inst.get("uid")
    if not uid:
        raise RecorderError("Instrument UID missing")
    if spec["kind"]=="share" and inst.get("instrumentType","share").lower() not in (
            "share","shares","instrument_type_share"):
        raise RecorderError("Security kind mismatch: not a share")
    return {"uid":uid, "ticker":str(inst.get("ticker",spec["symbol"])),
            "isin":str(inst.get("isin","")), "board":spec["board"],
            "kind":spec["kind"],"name":str(inst.get("name",spec["symbol"])),
            "currency":str(inst.get("currency",""))}


def read_one(api, spec, depth, now):
    inst=resolve(api,spec)
    uid=inst["uid"]
    last=api.call("last",{"instrumentId":[uid],"lastPriceType":"LAST_PRICE_EXCHANGE"})
    book=api.call("book",{"instrumentId":uid,"depth":depth})
    payload=normalize_payload(inst,last,book,depth,now)
    return inst,payload


def collect(api=None, db_path=DB_PATH, config=None, now=None):
    now = now or utc_now()
    cfg = config or load_config()
    if api is None:
        if not c.TOKEN_PATH.name == "sandbox-token":
            raise RecorderError("Only Sandbox token is allowed")
        api=ReadOnlySandboxMarket(sandbox_module.SandboxAPI(c.token_read()))
    db=connect(db_path)
    okay=0
    failure=0
    try:
        for spec in cfg["instruments"]:
            try:
                inst,row=read_one(api,spec,cfg["depth"],now)
                store_snapshot(db,inst,row,now)
                okay+=1
                status=row["quality_status"] + "/" + row["quality_reason"]
                print(f"{spec['symbol']} ({inst['board']}): "
                      f"last={row['last_price'] or '—'}, "
                      f"bid={row['best_bid'] or '—'}, ask={row['best_ask'] or '—'}, "
                      f"spread={row['spread_pct'] if row['spread_pct'] is not None else '—'}%, "
                      f"book={status}, "
                      f"bid10={row['bids_total']} ask10={row['asks_total']}")
            except (c.APIError, RecorderError, KeyError, ValueError, TypeError) as exc:
                failure+=1
                store_failure(db,spec,now,exc)
                print(f"{spec['symbol']}: ERROR {type(exc).__name__} {str(exc)[:200]}",
                      file=sys.stderr)
    finally:
        db.close()
    print(f"Market DB: {db_path}; saved={okay}; failed={failure}; "
          f"slot={slot_5min(now)}")
    return 0 if failure == 0 else 1


def recent(db, hours, symbol=None):
    boundary=(utc_now()-timedelta(hours=hours)).isoformat(timespec="seconds")
    sql="""SELECT i.ticker,i.isin,i.board,s.* FROM snapshots s
          JOIN instruments i ON i.uid=s.uid WHERE s.collected_utc>=?"""
    args=[boundary]
    if symbol:
        sql+=" AND (i.ticker=? OR i.isin=?)"
        args.extend([symbol,symbol])
    sql+=" ORDER BY i.ticker,s.slot_utc"
    return [dict(r) for r in db.execute(sql,args).fetchall()]


def report(db_path=DB_PATH, hours=24):
    if not db_path.exists():
        raise RecorderError("База пока не создана. Выполните collect")
    db=connect(db_path)
    try:
        data=recent(db,hours)
        failures=db.execute(
          "SELECT symbol,board,collected_utc,safe_message FROM fetch_errors "
          "ORDER BY collected_utc DESC LIMIT 5").fetchall()
    finally:
        db.close()
    by_key={}
    for r in data:
        by_key.setdefault((r["ticker"],r["board"]),[]).append(r)
    if not by_key:
        print("За выбранный период нет успешных замеров.")
    for (ticker,board),rows in by_key.items():
        first,last=rows[0],rows[-1]
        qualified=[r for r in rows if r["quality_status"]=="valid"]
        spreads=[r["spread_pct"] for r in qualified if r["spread_pct"] is not None]
        statuses={k:sum(r["quality_status"]==k for r in rows)
                  for k in ("valid","stale","unverified","invalid")}
        print(f"\n{ticker} [{board}] — {len(rows)} уникальных 5-минутных записей")
        print(f"  Последний замер: {last['collected_utc']} | качество: "
              f"{last['quality_status']} ({last['quality_reason']})")
        print(f"  Качество за период: {statuses}")
        print(f"  Последняя сделка: {last['last_price'] or '—'}; "
              f"best bid/ask: {last['best_bid'] or '—'} / {last['best_ask'] or '—'}")
        print(f"  Объём первых 10 уровней (лоты): "
              f"{last['bids_total']} / {last['asks_total']}")
        print(f"  Медианный спред (только пригодные): "
              f"{round(statistics.median(spreads),3) if spreads else '—'}%")
        print(f"  Возраст последнего стакана: "
              f"{last['book_stale_seconds'] if last['book_stale_seconds'] is not None else 'неизвестен'} с")
        if len(qualified) >= 2 and qualified[0]["last_price"] and qualified[-1]["last_price"]:
            a,b=Decimal(qualified[0]["last_price"]),Decimal(qualified[-1]["last_price"])
            if a>0:
                print(f"  Изменение подтверждённой цены: {((b-a)/a*100):.3f}%")
        else:
            print("  Динамика подтверждённой цены: недостаточно пригодных снимков")
    if failures:
        print("\nПоследние ошибки сбора (могут относиться к предыдущим периодам):")
        for x in failures:
            print(f"  {x['collected_utc']} {x['symbol']}: {x['safe_message'][:160]}")


def export_csv(db_path=DB_PATH, hours=168, symbol=None):
    if not db_path.exists():
        raise RecorderError("База пока не создана")
    db=connect(db_path)
    try:
        rows=recent(db,hours,symbol)
        if not rows:
            raise RecorderError("Нет записей в выбранном интервале")
        headers=["ticker","isin","board","slot_utc","collected_utc",
                 "last_price","last_trade_utc","book_time_utc",
                 "best_bid","best_ask","spread_pct","bid_top_lots",
                 "ask_top_lots","bids_total","asks_total","book_stale_seconds",
                 "last_stale_seconds"]
        c.private_dir(EXPORT_DIR)
        name="market_"+utc_now().strftime("%Y%m%d_%H%M%S")+".csv"
        out=EXPORT_DIR/name
        # private output, no token/account data
        fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w",newline="",encoding="utf-8-sig") as fp:
            writer=csv.DictWriter(fp,fieldnames=headers,extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print("Экспорт CSV:",out)
        print("Записей:",len(rows))
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description="T-Invest Sandbox read-only market historian")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("collect",help="Собрать котировки и стаканы в SQLite")
    rp=sub.add_parser("report",help="Статистика спреда и ликвидности за N часов")
    rp.add_argument("--hours",type=int,default=24)
    ex=sub.add_parser("export",help="Экспорт исторических срезов в CSV")
    ex.add_argument("--hours",type=int,default=168)
    ex.add_argument("--symbol",default=None)
    args=parser.parse_args()
    try:
        if args.command=="collect":
            return collect()
        if not 1<=args.hours<=24*365:
            raise RecorderError("hours должно быть от 1 до 8760")
        if args.command=="report":
            report(hours=args.hours)
        elif args.command=="export":
            export_csv(hours=args.hours,symbol=args.symbol)
        return 0
    except (RecorderError,c.APIError,sqlite3.Error,PermissionError,OSError) as exc:
        print("Ошибка:",type(exc).__name__,str(exc)[:230],file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
