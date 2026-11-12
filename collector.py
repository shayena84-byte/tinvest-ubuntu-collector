#!/usr/bin/env python3
"""Read-only T-Invest REST collector. No trade endpoints; Python standard library only."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import getpass
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

BASE_URL = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
HOME = Path.home()
CONFIG = HOME / ".config" / "tinvest-readonly"
TOKEN_PATH = CONFIG / "token"
SALT_PATH = CONFIG / "salt"
CACHE_PATH = CONFIG / "instrument_cache.json"
OUTDIR = HOME / "tinvest-reports"

METHODS = {
    "accounts": "UsersService/GetAccounts",
    "portfolio": "OperationsService/GetPortfolio",
    "positions": "OperationsService/GetPositions",
    "operations": "OperationsService/GetOperationsByCursor",
    "instrument": "InstrumentsService/GetInstrumentBy",
    "bond": "InstrumentsService/BondBy",
    "find": "InstrumentsService/FindInstrument",
    "last": "MarketDataService/GetLastPrices",
    "book": "MarketDataService/GetOrderBook",
}

class APIError(Exception):
    pass


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def private_write(path, data: bytes):
    private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
    except BaseException:
        try: os.close(fd)
        except OSError: pass
        raise


def setup():
    private_dir(CONFIG)
    private_dir(OUTDIR)
    print("Введите отдельный токен T-Invest API с правами ТОЛЬКО ЧТЕНИЯ (ввод не отображается).")
    token = getpass.getpass("T-Invest token: ").strip()
    if not token or "\n" in token or "\r" in token or " " in token:
        raise ValueError("Токен не получен или содержит недопустимые символы")
    private_write(TOKEN_PATH, (token + "\n").encode())
    if not SALT_PATH.exists():
        private_write(SALT_PATH, os.urandom(32))
    print(f"Токен сохранён локально: {TOKEN_PATH} (права 600). Не публикуйте этот файл.")


def token_read():
    if not TOKEN_PATH.is_file():
        raise APIError("Нет токена. Сначала выполните: python3 collector.py setup")
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        raise APIError("Файл токена пуст")
    return token


class API:
    def __init__(self, token, opener=urlopen, base=BASE_URL):
        self.token, self.opener, self.base = token, opener, base

    def call(self, method, payload=None, tries=3):
        if method not in METHODS:
            raise ValueError("Неизвестный или запрещённый метод")
        url = self.base + METHODS[method]
        data = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data,
                      headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": "application/json"},
                      method="POST")
        for attempt in range(tries):
            try:
                with self.opener(req, timeout=25) as resp:
                    result = json.load(resp)
                if not isinstance(result, dict):
                    raise APIError("Некорректный тип ответа API")
                return result
            except HTTPError as exc:
                status = exc.code
                if status in (401,403):
                    raise APIError(f"Ошибка авторизации / недостаточно прав (HTTP {status}). Проверьте read-only токен.") from None
                if status in (429,500,502,503,504) and attempt < tries - 1:
                    time.sleep(2 ** attempt + 1)
                    continue
                # Surface only the API error code and short validation message,
                # never print request headers or the locally stored bearer token.
                detail = ""
                if status == 400:
                    try:
                        err = json.loads(exc.read(4096).decode("utf-8", "replace"))
                        if isinstance(err, dict):
                            msg = str(err.get("message") or err.get("error") or "")
                            code = str(err.get("code") or "")
                            detail = ("; " + code + " " + msg)[:400] if (code or msg) else ""
                    except (ValueError, TypeError, UnicodeError):
                        pass
                    detail = detail.replace(self.token, "[REDACTED]")
                raise APIError(f"T-Invest API вернул HTTP {status} для {method}{detail}") from None
            except (URLError, TimeoutError, socket.timeout):
                if attempt < tries - 1:
                    time.sleep(2 ** attempt + 1)
                    continue
                raise APIError(f"Сетевая ошибка обращения к T-Invest ({method}). Проверьте DNS, TLS и связь.") from None
        raise APIError("Исчерпаны попытки")


def sha_ref(account_id: str, salt: bytes) -> str:
    return "tb_" + hmac.new(salt, account_id.encode(), hashlib.sha256).hexdigest()[:12]


def salt_read():
    if not SALT_PATH.is_file():
        private_write(SALT_PATH, os.urandom(32))
    return SALT_PATH.read_bytes()


def val_quote(obj):
    """T-Invest Quotation / MoneyValue, preserving values as string to avoid FP rounding."""
    if obj is None:
        return ""
    if not isinstance(obj, dict) or "units" not in obj:
        return str(obj) if isinstance(obj, (int, float, str)) else ""
    try:
        units = Decimal(str(obj.get("units", "0")))
        nanos = Decimal(str(obj.get("nano", 0)))
        return str(units + nanos / Decimal(1_000_000_000))
    except (InvalidOperation, TypeError, ValueError):
        return ""


def money(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {"value": val_quote(obj), "currency": obj.get("currency", "")}
    return None


def portfolio_transform(portfolio):
    positions = []
    for p in portfolio.get("positions", []):
        positions.append({
            "figi": p.get("figi", ""), "instrument_uid": p.get("instrumentUid", ""),
            "position_uid": p.get("positionUid", ""), "instrument_type": p.get("instrumentType", ""),
            "quantity": val_quote(p.get("quantity")), "quantity_lots": val_quote(p.get("quantityLots")),
            "current_price": money(p.get("currentPrice")),
            "average_purchase_price": money(p.get("averagePositionPrice")),
            "expected_yield": money(p.get("expectedYield")),
            "current_nkd": money(p.get("currentNkd")),
            "blocked": bool(p.get("blocked", False)),
        })
    total_keys = ("totalAmountShares", "totalAmountBonds", "totalAmountEtf", "totalAmountCurrencies", "totalAmountFutures", "totalAmountPortfolio", "totalAmountSp", "totalAmountOptions")
    return {"totals": {k: money(portfolio.get(k)) for k in total_keys if k in portfolio},
            "positions": positions, "position_count": len(positions)}


def positions_transform(raw):
    return {
        "money": [money(o) for o in raw.get("money", [])],
        "blocked": [money(o) for o in raw.get("blocked", [])],
        "securities": [{"figi": p.get("figi", ""), "instrument_uid": p.get("instrumentUid", ""),
                        "instrument_type": p.get("instrumentType", ""), "balance": p.get("balance"),
                        "blocked": p.get("blocked"), "exchange_blocked": p.get("exchangeBlocked")}
                       for p in raw.get("securities", [])],
    }


def operation_transform(op, salt):
    opid = str(op.get("id", ""))
    return {
        "operation_ref": sha_ref(opid, salt) if opid else None,
        "type": op.get("type", ""), "state": op.get("state", ""),
        "date": op.get("date", ""),
        "instrument_uid": op.get("instrumentUid", ""), "figi": op.get("figi", ""),
        "quantity": op.get("quantity", 0), "payment": money(op.get("payment")),
        "price": money(op.get("price")), "parent_operation_ref": (
            sha_ref(str(op.get("parentOperationId")), salt) if op.get("parentOperationId") else None
        ),
    }


def cursor_operations(api, account_id, start, end, salt, page_limit=100):
    cursor = None
    visited = set()
    rows = []
    for page in range(page_limit):
        body = {"accountId": account_id, "from": start, "to": end, "limit": 200}
        if cursor:
            body["cursor"] = cursor
        resp = api.call("operations", body)
        items = resp.get("items", resp.get("operations", []))
        rows.extend(operation_transform(o, salt) for o in items)
        has_next = bool(resp.get("hasNext", False))
        if not has_next:
            return rows, False
        new_cursor = resp.get("nextCursor", "")
        if not new_cursor or new_cursor == cursor or new_cursor in visited:
            raise APIError("API сообщил о следующей странице, но cursor пустой или повторяется")
        visited.add(new_cursor)
        cursor = new_cursor
        time.sleep(0.15)
    return rows, True


def load_cache():
    try:
        data = json.loads(CACHE_PATH.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def enrich_instruments(api, uids, max_new=160):
    cache = load_cache()
    new = 0
    failures = []
    for uid in sorted(set(filter(None, uids))):
        if uid in cache:
            continue
        if new >= max_new:
            failures.append("Лимит справочника исчерпан; часть UID осталась без описания")
            break
        try:
            result = api.call("instrument", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid})
            inst = result.get("instrument", {})
            cache[uid] = {k: inst.get(k, "") for k in ("name", "ticker", "isin", "figi", "instrumentType", "currency", "classCode")}
            new += 1
            time.sleep(.35)
        except APIError as e:
            failures.append(f"Не удалось получить справочник для UID {uid[:8]}: {str(e)}")
    private_write(CACHE_PATH, json.dumps(cache, ensure_ascii=False, indent=2).encode())
    return {uid: cache[uid] for uid in uids if uid in cache}, failures


def portfolio_rows(account_ref, portfolio, lookup):
    rows = []
    for p in portfolio.get("positions", []):
        meta = lookup.get(p["instrument_uid"], {})
        rows.append({"account_ref": account_ref,
                     "ticker": meta.get("ticker", ""), "name": meta.get("name", ""), "isin": meta.get("isin", ""),
                     "instrument_type": p["instrument_type"], "currency": (p.get("current_price") or {}).get("currency", ""),
                     "quantity": p["quantity"], "current_price": (p.get("current_price") or {}).get("value", ""),
                     "average_purchase_price": (p.get("average_purchase_price") or {}).get("value", ""),
                     "expected_yield": (p.get("expected_yield") or {}).get("value", ""), "blocked": p["blocked"]})
    return rows


def export_zip(path, result):
    """ZIP is not encrypted. Restrict permissions and share only intentionally."""
    filelist = {"snapshot.json": json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")}
    flat = []
    ops = []
    for a in result["accounts"]:
        flat += portfolio_rows(a["account_ref"], a["portfolio"], result.get("instruments", {}))
        for o in a.get("operations", []):
            ops.append({"account_ref": a["account_ref"], **o})
    for filename, rows in (("positions.csv", flat), ("operations.csv", ops)):
        if rows:
            f = io.StringIO()
            writer = csv.DictWriter(f, fieldnames=list(rows[0]), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            filelist[filename] = f.getvalue().encode("utf-8-sig")
    filelist["README.txt"] = ("T-Invest read-only snapshot. Contains financial information. "
                                   "No access token and no real account IDs. "
                                   "ZIP NOT ENCRYPTED; don't publish publicly. "
                                   "Bond quotation is % of outstanding nominal; check NKD and amortization.\n").encode()
    private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "wb") as dst:
            with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for name, data in filelist.items():
                    z.writestr(name, data)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def run_accounts(api, salt):
    resp = api.call("accounts", {"status":"ACCOUNT_STATUS_OPEN"})
    accounts = resp.get("accounts", [])
    for a in accounts:
        print(f"{sha_ref(a['id'], salt)} | {a.get('type', '')} | {a.get('status', '')}")
    if not accounts:
        print("Открытые счета не найдены (проверьте область действия токена).")


def snapshot(api, days, acc_filter=None, no_ops=False, no_instruments=False):
    salt = salt_read()
    now = datetime.now(timezone.utc)
    begin = now - timedelta(days=days)
    accounts = api.call("accounts", {"status":"ACCOUNT_STATUS_OPEN"}).get("accounts", [])
    if acc_filter:
        accounts = [a for a in accounts if sha_ref(a["id"], salt) == acc_filter]
    if not accounts:
        raise APIError("Открытые счета не найдены или указанный account_ref не совпадает")
    output = {"source": "T-Invest API read-only", "generated_utc": now.isoformat(),
              "operations_window_from": begin.isoformat(), "operations_window_to": now.isoformat(),
              "accounts": [], "instruments": {}, "warnings": []}
    uids = []
    for a in accounts:
        account_id = a["id"]
        ref = sha_ref(account_id, salt)
        print(f"Получаю данные по {ref}…")
        p = portfolio_transform(api.call("portfolio", {"accountId": account_id, "currency": "RUB"}))
        pos = positions_transform(api.call("positions", {"accountId": account_id}))
        uids += [x.get("instrument_uid", "") for x in p["positions"]]
        entry = {"account_ref": ref, "account_type": a.get("type", ""),
                 "portfolio": p, "balances": pos}
        if not no_ops:
            try:
                operations, truncated = cursor_operations(api, account_id, begin.isoformat(), now.isoformat(), salt)
                entry["operations"] = operations
                if truncated:
                    output["warnings"].append(f"{ref}: история операций обрезана на 100 страницах")
            except APIError as e:
                entry["operations"] = []
                output["warnings"].append(f"{ref}: загрузка операций не удалась: {e}")
        output["accounts"].append(entry)
    if not no_instruments:
        output["instruments"], errs = enrich_instruments(api, uids)
        output["warnings"].extend(errs)
    else:
        output["warnings"].append("Описание инструментов отключено (--no-instruments)")
    name = now.strftime("tinvest_snapshot_%Y%m%d_%H%M%S_utc.zip")
    path = OUTDIR / name
    export_zip(path, output)
    print(f"Сохранён отчёт: {path}")
    print("Счетов: {}, позиций: {}, операций: {}".format(
        len(output["accounts"]), sum(len(a["portfolio"]["positions"]) for a in output["accounts"]),
        sum(len(a.get("operations", [])) for a in output["accounts"])))
    if output["warnings"]:
        print("ПРЕДУПРЕЖДЕНИЯ:")
        for w in output["warnings"][:12]: print(" - " + w)
    print("Файл содержит финансовые данные, не публикуйте его в открытом доступе. "
          "Для анализа загрузите именно ZIP в приватный чат.")
    return output


def quote_instrument(api, search):
    """Choose a precise instrument. For ordinary equity tickers prefer TQBR lookup.

    Same ticker/ISIN may be returned under several venues with different UIDs.
    Never guess one from an unfiltered list.
    """
    symbol = search.strip().upper()
    # Ticker+classCode is unique for the main Moscow Exchange equity board.
    if symbol in {"SBER", "MOEX", "YDEX", "ROSN", "LKOH", "NVTK", "T", "VTBR", "TATN", "SBERP", "GAZP", "GMKN", "PLZL", "POSI", "DOMRF"}:
        resp = api.call("instrument", {
            "idType": "INSTRUMENT_ID_TYPE_TICKER",
            "classCode": "TQBR",
            "id": symbol,
        })
        inst = resp.get("instrument") or {}
        if (str(inst.get("ticker", "")).upper() != symbol
                or str(inst.get("classCode", "")).upper() != "TQBR"
                or not inst.get("uid")):
            raise APIError("Не удалось подтвердить UID акции " + symbol + " на доске TQBR")
        return inst

    res = api.call("find", {"query": search})
    instruments = res.get("instruments", [])
    candidates = [x for x in instruments if search.casefold() in
                  {str(x.get(k, "")).casefold() for k in ("isin", "ticker", "uid", "figi")}]
    if not candidates:
        print("Точных совпадений не найдено. Ближайшие варианты:")
        for x in instruments[:10]:
            print(f"- {x.get('name','')} | {x.get('ticker','')} | {x.get('isin','')} | {x.get('uid','')}")
        raise APIError("Для запроса котировок используйте точный ISIN, UID или тикер")
    if len(candidates) > 1:
        print("Найдено несколько совпадений — укажите UID и проверьте доску торгов:")
        for x in candidates:
            print(f"- {x.get('name','')} | {x.get('ticker','')} | "
                  f"{x.get('isin','')} | {x.get('classCode','')} | {x.get('uid','')}")
        raise APIError("Неоднозначный запрос; выбор инструмента остановлен")
    return candidates[0]


def quote(api, search, depth=10):
    inst = quote_instrument(api, search)
    uid = inst.get("uid") or inst.get("figi")
    if not uid:
        raise APIError("Для найденного инструмента не получен UID/FIGI")
    last = api.call("last", {"instrumentId": [uid]})
    book = api.call("book", {"instrumentId": uid, "depth": depth})
    print(f"{inst.get('name','')} | {inst.get('ticker','')} | {inst.get('isin','')} | "
          f"доска {inst.get('classCode','')} | UID {uid}")
    print("ВАЖНО: цена облигаций в API указана в % от ОСТАТОЧНОГО номинала, без НКД.")
    for p in last.get("lastPrices", []):
        print("Последняя цена:", val_quote(p.get("price")), "| время:", p.get("time", ""))
    print("Покупка (bids):")
    for lvl in book.get("bids", [])[:depth]:
        print(f"  цена {val_quote(lvl.get('price'))}, лотов {lvl.get('quantity','')}")
    print("Продажа (asks):")
    for lvl in book.get("asks", [])[:depth]:
        print(f"  цена {val_quote(lvl.get('price'))}, лотов {lvl.get('quantity','')}")
    print("Обновление стакана:", book.get("time", ""))
    print("Исторические/устаревшие цены возможны вне торгового времени.")


def main():
    parser = argparse.ArgumentParser(description="T-Invest read-only data collector, Ubuntu")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Безопасно записать read-only API токен")
    sub.add_parser("accounts", help="Показать открытые счета с псевдонимами")
    sn = sub.add_parser("snapshot", help="Выгрузить все доступные счета в приватный ZIP")
    sn.add_argument("--days", type=int, default=30, help="Операции за последние N дней (1-3650)")
    sn.add_argument("--account", help="account_ref из команды accounts")
    sn.add_argument("--no-ops", action="store_true", help="Не получать историю операций")
    sn.add_argument("--no-instruments", action="store_true", help="Не запрашивать названия/ISIN инструментов")
    qu = sub.add_parser("quote", help="Рыночная цена и стакан по точному ISIN/UID/тикеру")
    qu.add_argument("id")
    qu.add_argument("--depth", type=int, default=10)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup()
            return 0
        private_dir(CONFIG)
        private_dir(OUTDIR)
        api = API(token_read())
        if args.command == "accounts":
            run_accounts(api, salt_read())
        elif args.command == "snapshot":
            if not 1 <= args.days <= 3650:
                raise APIError("--days: число от 1 до 3650")
            snapshot(api, args.days, args.account, args.no_ops, args.no_instruments)
        elif args.command == "quote":
            if not 1 <= args.depth <= 50:
                raise APIError("--depth: число от 1 до 50")
            quote(api, args.id, args.depth)
        return 0
    except (APIError, ValueError, PermissionError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
