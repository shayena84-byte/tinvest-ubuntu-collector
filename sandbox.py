#!/usr/bin/env python3
"""T-Invest Sandbox mode. Isolated endpoint & token; never uses real brokerage accounts.

Commands: setup, accounts, snapshot, quote (from collector.py), open, fund.
Sandbox virtual-account changes only occur on explicit open / fund commands.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import sys

import collector as c

SANDBOX_BASE = "https://sandbox-invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."

# Override regular routes only inside sandbox.py. No production API calls.
c.METHODS.update({
    "accounts": "SandboxService/GetSandboxAccounts",
    "portfolio": "SandboxService/GetSandboxPortfolio",
    "positions": "SandboxService/GetSandboxPositions",
    "operations": "SandboxService/GetSandboxOperationsByCursor",
    "sandbox_open": "SandboxService/OpenSandboxAccount",
    "sandbox_payin": "SandboxService/SandboxPayIn",
})
c.TOKEN_PATH = c.CONFIG / "sandbox-token"
c.OUTDIR = c.OUTDIR / "sandbox"

_OriginalAPI = c.API

class SandboxAPI(_OriginalAPI):
    def __init__(self, token, opener=c.urlopen, base=SANDBOX_BASE):
        if base != SANDBOX_BASE:
            raise ValueError("Sandbox API must always use the Sandbox endpoint")
        super().__init__(token, opener=opener, base=base)

c.API = SandboxAPI


def _accounts(api):
    return api.call("accounts", {"status": "ACCOUNT_STATUS_OPEN"}).get("accounts", [])


def _get_account_id(api, account_ref):
    salt = c.salt_read()
    for a in _accounts(api):
        if c.sha_ref(str(a["id"]), salt) == account_ref:
            return str(a["id"])
    raise c.APIError("Виртуальный счёт с этим псевдонимом не найден. Выполните accounts.")


def open_account(name):
    api = SandboxAPI(c.token_read())
    resp = api.call("sandbox_open", {"name": name})
    account_id = resp.get("accountId") or resp.get("account_id")
    if not account_id:
        print("Метод вызван, но ответ не содержит ожидаемого ID. Проверьте список через accounts.")
    else:
        print("Виртуальный счёт открыт:", c.sha_ref(str(account_id), c.salt_read()))
    print("Этот счёт не связан с реальными инвестициями.")


def fund_account(account_ref, rubles):
    try:
        amount = Decimal(str(rubles))
    except InvalidOperation:
        raise c.APIError("Некорректная сумма")
    if not amount.is_finite() or amount != amount.to_integral_value() or not Decimal(1) <= amount <= Decimal(30_000_000):
        raise c.APIError("Сумма пополнения: целое число рублей от 1 до 30 000 000")
    api = SandboxAPI(c.token_read())
    account_id = _get_account_id(api, account_ref)
    api.call("sandbox_payin", {"accountId": account_id,
                              "amount": {"currency": "rub", "units": str(int(amount)), "nano": 0}})
    print(f"Зачисление {amount:.0f} виртуальных рублей запрошено для {account_ref}.")
    print("Проверьте остатки через snapshot; реальных денег операция не затрагивает.")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("open", "fund"):
        p = argparse.ArgumentParser(description="Virtual Sandbox account management, no real money")
        sp = p.add_subparsers(dest="command", required=True)
        opener = sp.add_parser("open", help="Создать виртуальный счет песочницы")
        opener.add_argument("--name", default="Ubuntu T-Invest Demo")
        funder = sp.add_parser("fund", help="Пополнить виртуальный счет рублями")
        funder.add_argument("account_ref", help="Псевдоним из accounts (tb_...)")
        funder.add_argument("rubles", help="Виртуальные рубли, максимум 30 млн")
        args = p.parse_args()
        try:
            if args.command == "open":
                open_account(args.name)
            else:
                fund_account(args.account_ref, args.rubles)
            return 0
        except (c.APIError, ValueError, PermissionError) as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
    return c.main()


if __name__ == "__main__":
    raise SystemExit(main())
