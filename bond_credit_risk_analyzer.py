#!/usr/bin/env python3
"""Russian corporate bond credit and cash-flow audit. 100% offline/read-only.

Heuristic risk bands are NOT default probabilities, advice, or bond ratings.
Use sourced, dated issuer snapshots; restricted escrow is never treated as cash.
No broker, network, OpenAI, trading, secrets or side effects.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
MAX_QUOTE_AGE_DAYS = 4   # accommodates most weekends, not long exchange holidays
MAX_FINANCIAL_AGE_DAYS = 190


def _date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("Date must use YYYY-MM-DD")
    return date.fromisoformat(value)


def _number(value: Any, field: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field}: finite number required")
    x = float(value)
    if not math.isfinite(x) or (nonnegative and x < 0):
        raise ValueError(f"{field}: invalid or negative number")
    return x


def _money(obj: dict, key: str, missing: list[str], *, required=True):
    value = obj.get(key)
    if value is None:
        if required:
            missing.append(key)
        return None
    return _number(value, key, nonnegative=(key not in ("cfo_ltm_rub", "net_corporate_debt_rub")))


def _round(value: float | None, digits=3):
    return round(value, digits) if value is not None else None


def _cashflows(bond: dict, as_of: date) -> tuple[list[tuple[date, float]], list[str]]:
    issues = []
    if "cash_flows" not in bond or not isinstance(bond["cash_flows"], list):
        return [], ["missing_cash_flows"]
    flows = []
    total_principal = 0.0
    last = None
    for i, flow in enumerate(bond["cash_flows"]):
        when = _date(flow["date"])
        if when <= as_of:
            raise ValueError(f"cash_flows[{i}]: historic or today's flow (use future flows only)")
        if last and when < last:
            raise ValueError("cash_flows: dates must be sorted")
        last = when
        principal = _number(flow.get("principal_rub", 0), "principal_rub")
        coupon = _number(flow.get("coupon_rub", 0), "coupon_rub")
        total_principal += principal
        if principal + coupon > 0:
            flows.append((when, principal + coupon))
    principal_out = _number(bond["outstanding_principal_rub"], "outstanding_principal_rub")
    if principal_out == 0:
        raise ValueError("outstanding_principal_rub must be positive")
    if total_principal > principal_out + max(0.01, principal_out * .001):
        raise ValueError("Scheduled principal exceeds current outstanding principal")
    if total_principal < principal_out - max(0.01, principal_out * .001):
        issues.append("incomplete_principal_schedule")
    if not flows:
        issues.append("no_future_payments")
    return flows, issues


def _xirr(flows: list[tuple[date, float]], price: float, as_of: date) -> float | None:
    """Annual effective IRR by bisection for positive future cash flows.

    Returns fraction, e.g. 0.2 means 20% annualized. No guess of credit losses.
    """
    if price <= 0 or not flows:
        return None
    amounts = [(max(1, (d - as_of).days) / 365.25, amt) for d, amt in flows]
    if not any(amt > 0 for _, amt in amounts):
        return None

    def npv(rate: float) -> float:
        return sum(a * (1 + rate) ** -t for t, a in amounts) - price

    lo, hi = -.999999, 1.0
    while npv(hi) > 0 and hi < 1e8:
        hi = hi * 2 + 1
    if npv(hi) > 0 or npv(lo) < 0:
        return None
    for _ in range(150):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _offer_cashflows(bond: dict, flows: list[tuple[date, float]], as_of: date):
    offer = bond.get("put_offer")
    if not offer:
        return None, []
    if offer.get("holder_put_right") is not True:
        return None, ["offer_not_confirmed_as_holder_put"]
    when = _date(offer["date"])
    if when <= as_of:
        return None, ["put_offer_expired"]
    percentage = _number(offer["redemption_pct"], "redemption_pct")
    if percentage > 200:
        raise ValueError("redemption_pct > 200 is not supported")
    remaining = _number(bond["outstanding_principal_rub"], "outstanding_principal_rub")
    selected = []
    for f in bond["cash_flows"]:
        d = _date(f["date"])
        if d <= when:
            principal = _number(f.get("principal_rub", 0), "principal_rub")
            remaining -= principal
            # Coupon due ON put date is counted, redemption separately.
            selected.append((d, _number(f.get("coupon_rub", 0), "coupon_rub") + principal))
    if remaining < -0.01:
        raise ValueError("Principal amortization before put exceeds outstanding")
    selected.append((when, max(0, remaining) * percentage / 100))
    return selected, (["put_requires_notice"] if offer.get("requires_notice") else [])


def _valid_isin(isin: str) -> bool:
    if len(isin) != 12 or not isin[:2].isalpha() or not isin[2:].isalnum():
        return False
    digits = ''.join(str(ord(ch) - 55) if ch.isalpha() else ch for ch in isin)
    total = 0
    for index, digit in enumerate(reversed(digits)):
        n = int(digit) * (2 if index % 2 else 1)
        total += n // 10 + n % 10
    return total % 10 == 0


def analyze_bond(bond: dict, as_of: date) -> dict:
    isin = str(bond.get("isin", "")).strip().upper()
    if not _valid_isin(isin):
        raise ValueError("isin: invalid ISIN checksum or shape")
    instrument = {"isin": isin, "ticker": bond.get("ticker"), "board": bond.get("board")}
    age = None
    reasons = []
    source = bond.get("quote_source_url")
    if not source:
        reasons.append("missing_quote_source")
    if bond.get("quote_quality") != "valid":
        reasons.append("quote_not_verified")
    if bond.get("schedule_verified") is not True or not bond.get("cashflows_source_url"):
        reasons.append("cashflow_schedule_unverified")
    stamp = bond.get("quote_as_of")
    if not stamp:
        reasons.append("quote_date_missing")
    else:
        age = (as_of - _date(stamp)).days
        if age < 0:
            raise ValueError("quote_as_of cannot be in the future")
        if age > MAX_QUOTE_AGE_DAYS:
            reasons.append("stale_quote")
    outstanding = _number(bond["outstanding_principal_rub"], "outstanding_principal_rub")
    if outstanding <= 0:
        raise ValueError("outstanding_principal_rub must be positive")
    dirty = bond.get("dirty_price_rub")
    if dirty is None:
        if "clean_price_pct" in bond and "accrued_coupon_rub" in bond:
            pct = _number(bond["clean_price_pct"], "clean_price_pct")
            aci = _number(bond["accrued_coupon_rub"], "accrued_coupon_rub")
            dirty = pct / 100 * outstanding + aci
        else:
            reasons.append("missing_price_or_aci")
    if dirty is not None:
        dirty = _number(dirty, "dirty_price_rub")
        if dirty <= 0:
            raise ValueError("dirty_price_rub must be positive")
    flows, flow_reasons = _cashflows(bond, as_of)
    reasons += flow_reasons
    future_put, offer_reasons = _offer_cashflows(bond, flows, as_of)
    reasons += offer_reasons
    # Fail closed: never publish a market yield without source, fresh quote,
    # verified quality, price and a complete scheduled principal repayment.
    yield_ok = not any(r in reasons for r in (
        "missing_quote_source", "quote_not_verified", "quote_date_missing",
        "stale_quote", "missing_price_or_aci", "cashflow_schedule_unverified", "missing_cash_flows",
        "incomplete_principal_schedule", "no_future_payments"))
    ytm = _xirr(flows, dirty, as_of) if yield_ok else None
    ytp = _xirr(future_put, dirty, as_of) if yield_ok and future_put else None
    return {
        **instrument, "quote_date": stamp, "quote_age_days": age,
        "quote_source_url": source, "cashflows_source_url": bond.get("cashflows_source_url"), "dirty_price_rub": _round(dirty, 2),
        "yield_to_maturity_pct": _round(ytm * 100 if ytm is not None else None),
        "yield_to_put_pct": _round(ytp * 100 if ytp is not None else None),
        "put_date": (bond.get("put_offer") or {}).get("date"),
        "put_requires_notice": bool((bond.get("put_offer") or {}).get("requires_notice")),
        "yield_status": "calculated" if yield_ok else "blocked_incomplete_or_stale_data",
        "warnings": sorted(set(reasons)),
    }


def analyze_issuer(issuer: dict, as_of: date) -> dict:
    name = str(issuer.get("name", "")).strip()
    if not name:
        raise ValueError("issuer.name is required")
    financials = issuer.get("financials") or {}
    missing: list[str] = []
    required = ("cash_unrestricted_rub", "committed_undrawn_lines_rub",
                "net_corporate_debt_rub", "ebitda_ltm_rub", "interest_expense_ltm_rub",
                "principal_due_12m_rub", "cfo_ltm_rub")
    metrics = {k: _money(financials, k, missing) for k in required}
    restricted = _money(financials, "cash_restricted_escrow_rub", missing, required=False)
    project = _money(financials, "project_finance_debt_rub", missing, required=False)
    source = issuer.get("financials_source_url")
    if not source:
        missing.append("financials_source_url")
    fdate = issuer.get("financials_as_of")
    financial_age = None
    if not fdate:
        missing.append("financials_as_of")
    else:
        financial_age = (as_of - _date(fdate)).days
        if financial_age < 0:
            raise ValueError("financials_as_of cannot be in the future")
        if financial_age > MAX_FINANCIAL_AGE_DAYS:
            missing.append("stale_financials")
    verified_events = []
    unverified_events = []
    event_weight = 0
    weights = {"coupon_delay": 25, "principal_delay": 35, "unresolved_default": 45,
               "restructuring_proposal": 30, "covenant_breach": 20, "rating_downgrade": 10}
    for item in issuer.get("events", []):
        day = _date(item["date"])
        if day > as_of:
            raise ValueError("event cannot be future-dated")
        if item.get("verified") is not True or not item.get("source_url"):
            unverified_events.append(item)
            continue
        verified_events.append(item)
        if 0 <= (as_of - day).days <= 365:
            event_weight = max(event_weight, weights.get(item.get("type"), 0))
    ratio_leverage = ratio_interest = liquidity = None
    score = None
    risk_band = "insufficient_data"
    scenarios = {}
    if not missing:
        cash = metrics["cash_unrestricted_rub"]
        lines = metrics["committed_undrawn_lines_rub"]
        debt = metrics["net_corporate_debt_rub"]
        ebitda = metrics["ebitda_ltm_rub"]
        interest = metrics["interest_expense_ltm_rub"]
        principal = metrics["principal_due_12m_rub"]
        cfo = metrics["cfo_ltm_rub"]
        ratio_leverage = debt / ebitda if ebitda > 0 else None
        ratio_interest = ebitda / interest if interest > 0 else None
        # Liquidity excludes escrow and non-committed financing; CFO already
        # reflects interest cash classification, so do NOT subtract interest again.
        liquidity = (cash + lines) / principal if principal > 0 else None
        points = 5
        if ratio_leverage is None or ratio_leverage >= 6:
            points += 20
        elif ratio_leverage >= 4:
            points += 12
        elif ratio_leverage >= 2:
            points += 5
        if ratio_interest is None or ratio_interest < 1:
            points += 20
        elif ratio_interest < 1.5:
            points += 12
        elif ratio_interest < 2:
            points += 6
        if liquidity is not None:
            if liquidity < .5:
                points += 20
            elif liquidity < 1:
                points += 12
            elif liquidity < 1.5:
                points += 5
        if cfo < 0:
            points += 10
        rating = issuer.get("rating") or {}
        grade = str(rating.get("grade", "")).upper()
        if not grade:
            missing.append("rating_grade_missing_score_is_partial")
        elif any(x in grade for x in ("CCC", " CC", "DEFAULT", " D")):
            points += 15
        elif grade.startswith(("B", "BB")) and not grade.startswith("BBB"):
            points += 10
        elif grade.startswith("BBB"):
            points += 5
        if rating.get("outlook") == "negative":
            points += 5
        points += event_weight
        score = min(100, points)
        risk_band = ("low" if score < 25 else "elevated" if score < 50
                     else "high" if score < 75 else "critical")
        baseline = cash + lines + cfo - principal
        stressed_cfo = cfo * (.8 if cfo >= 0 else 1.2)
        scenarios = {
            "baseline_liquidity_surplus_12m_rub": _round(baseline, 2),
            "operating_stress_20pct_surplus_12m_rub": _round(cash + lines + stressed_cfo - principal, 2),
            "no_new_credit_lines_surplus_12m_rub": _round(cash + cfo - principal, 2),
        }
    return {
        "name": name, "inn": issuer.get("inn"),
        "financials_as_of": fdate, "financial_age_days": financial_age,
        "financials_source_url": source,
        "metrics": {**metrics, "cash_restricted_escrow_rub": restricted,
                    "project_finance_debt_rub": project},
        "net_corporate_debt_to_ebitda": _round(ratio_leverage),
        "ebitda_to_interest_expense": _round(ratio_interest),
        "liquid_assets_to_12m_principal": _round(liquidity),
        "risk_score_heuristic_0_100": score, "risk_band": risk_band,
        "cashflow_scenarios": scenarios,
        "missing_or_stale": sorted(set(missing)),
        "verified_events": verified_events,
        "unverified_events_count": len(unverified_events),
        "rating": issuer.get("rating"),
    }


def analyze(payload: dict) -> dict:
    if payload.get("schema_version") != "1":
        raise ValueError("schema_version must equal '1'")
    as_of = _date(payload["as_of"])
    issuers = payload.get("issuers")
    if not isinstance(issuers, list) or not issuers:
        raise ValueError("issuers must be a nonempty list")
    results = []
    for issuer in issuers:
        x = analyze_issuer(issuer, as_of)
        x["bonds"] = [analyze_bond(b, as_of) for b in issuer.get("bonds", [])]
        results.append(x)
    return {
        "module": "Bond Credit Risk Analyzer", "version": VERSION,
        "as_of": as_of.isoformat(), "issuers": results,
        "data_provenance": "user-supplied, not independently verified by this offline program",
        "risk_score_note": "Heuristic priority score only; NOT probability of default, rating or recommendation",
        "yield_note": "YTM/YTP assume every cash flow paid as scheduled; no taxes, fees, default losses or reinvestment assumptions",
        "network_requests": 0, "broker_orders": 0,
    }


def to_markdown(result: dict) -> str:
    lines = ["# Bond Credit Risk Analyzer — отчёт", "",
             f"Дата среза: {result['as_of']}. Все цены и финансовые метрики требуют первичных источников.", "",
             "> Риск-баллы — эвристика приоритизации, **не** вероятность дефолта. Доходность предполагает исполнение всех выплат.", ""]
    for x in result["issuers"]:
        lines.extend([f"## {x['name']}", "",
            f"- Риск: **{x['risk_band']}**, баллы: {x['risk_score_heuristic_0_100'] if x['risk_score_heuristic_0_100'] is not None else 'н/д'} / 100",
            f"- Чистый корпоративный долг / EBITDA: {x['net_corporate_debt_to_ebitda']}",
            f"- EBITDA / проценты: {x['ebitda_to_interest_expense']}",
            f"- Ликвидность / погашения 12 мес.: {x['liquid_assets_to_12m_principal']}",
            f"- Финансовый источник: {x['financials_source_url'] or 'не указан'} (на {x['financials_as_of'] or 'н/д'})",
            f"- Недостающие/устаревшие данные: {', '.join(x['missing_or_stale']) or 'нет критических пропусков'}", ""])
        if x["cashflow_scenarios"]:
            lines.extend(["**Стресс ликвидности на 12 месяцев (руб., сценарный баланс):**", ""])
            for k, v in x["cashflow_scenarios"].items():
                lines.append(f"- {k}: {v:,.2f}")
            lines.append("")
        for b in x["bonds"]:
            lines.append(f"- **{b['isin']}** ({b['ticker'] or 'н/д'}, {b['board'] or 'н/д'}): "
                         f"dirty {b['dirty_price_rub']} руб., YTM {b['yield_to_maturity_pct']}%, "
                         f"YTP {b['yield_to_put_pct']}%, статус {b['yield_status']}, "
                         f"предупреждения: {', '.join(b['warnings']) or 'нет'}, "
                         f"котировка: {b['quote_source_url'] or 'нет'} ({b['quote_date'] or 'н/д'})")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Sourced JSON issuer/bond snapshot")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path, help="Optional explicit output file")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = analyze(payload)
        content = (json.dumps(result, ensure_ascii=False, indent=2)
                   if args.format == "json" else to_markdown(result)) + "\n"
        if args.output:
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content, end="")
        return 0
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())