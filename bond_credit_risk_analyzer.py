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
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

VERSION = "0.2.0"
SCORING_VERSION = "early-warning-1"
MAX_REVIEW_AGE_DAYS = 30
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
    return _number(value, key, nonnegative=(key not in ("cfo_ltm_rub", "net_corporate_debt_rub", "ebitda_ltm_rub")))


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
    if not 0 < percentage <= 200:
        raise ValueError("redemption_pct must be in (0, 200]")
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
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
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
    if not _source(source):
        reasons.append("missing_quote_source")
    if bond.get("quote_quality") != "valid":
        reasons.append("quote_not_verified")
    if bond.get("schedule_verified") is not True or not _source(bond.get("cashflows_source_url")):
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
    future_put, offer_reasons = _offer_cashflows(bond, flows, as_of) if isinstance(bond.get("cash_flows"), list) else (None, [])
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


# Domain ceilings sum to 100. These are explicit expert rules, not a fitted PD.
DOMAIN_WEIGHTS = {"liquidity": 25, "cashflow": 20, "debt": 20, "governance": 15,
                  "tax_legal": 10, "payment": 5, "rating": 5}
REVIEW_DOMAINS = ("governance", "tax_legal", "payment")
# type -> domain, points, independent critical alert if still open
EVENT_RULES = {
    "coupon_delay": ("payment", 5, True),
    "principal_delay": ("payment", 5, True),
    "unresolved_default": ("payment", 5, True),
    "restructuring_proposal": ("payment", 5, True),
    "covenant_breach": ("payment", 4, False),
    "funding_gap_uncovered": ("liquidity", 25, True),
    "material_account_freeze": ("tax_legal", 10, True),
    "bankruptcy_filing": ("tax_legal", 10, True),
    "material_tax_claim": ("tax_legal", 8, False),
    "material_enforcement": ("tax_legal", 8, False),
    "material_audit_concern": ("governance", 15, True),
    "audit_qualification": ("governance", 10, False),
    "opaque_related_party_transaction": ("governance", 10, False),
    "material_related_party_funding": ("governance", 8, False),
    "rating_withdrawn_at_issuer_request": ("governance", 5, False),
    "rating_downgrade": ("rating", 3, False),
}
CREDIT_EVENT_TYPES = {"coupon_delay", "principal_delay", "unresolved_default",
                      "restructuring_proposal"}


def _source(value):
    return isinstance(value, str) and bool(re.match(r"^https?://[^\s/]+", value))


def _fresh_evidence(obj, as_of, max_days):
    if not isinstance(obj, dict) or obj.get("verified") is not True or not _source(obj.get("source_url")):
        return False
    if not obj.get("date"):
        return False
    age = (as_of - _date(obj["date"])).days
    if age < 0:
        raise ValueError("evidence cannot be future-dated")
    return age <= max_days


def _classify_events(issuer, as_of):
    verified, unverified, alerts = [], [], []
    points = dict.fromkeys(DOMAIN_WEIGHTS, 0)
    observations = []
    for item in issuer.get("events", []):
        if not isinstance(item, dict):
            raise ValueError("events must be objects")
        day = _date(item["date"])
        if day > as_of:
            raise ValueError("event cannot be future-dated")
        if item.get("verified") is not True or not _source(item.get("source_url")):
            unverified.append(item)
            continue
        kind = item.get("type")
        rule = EVENT_RULES.get(kind)
        status = item.get("status", "unknown")
        if status not in ("open", "resolved", "unknown"):
            raise ValueError("event status must be open, resolved or unknown")
        if item.get("isin") and not _valid_isin(str(item["isin"]).upper()):
            raise ValueError("event ISIN invalid")
        # Never accept an unsupported assertion that a payment or legal issue was cured.
        resolved = False
        if status == "resolved" and item.get("resolution_verified") is True and _source(item.get("resolution_source_url")):
            resolution = _date(item["resolved_date"])
            if not day <= resolution <= as_of:
                raise ValueError("resolution date outside event/as_of window")
            resolved = True
        effective = "resolved" if resolved else ("open" if status == "open" else "unknown")
        record = {**item, "effective_status": effective}
        verified.append(record)
        if not rule:
            observations.append("unclassified_event:" + str(kind))
            continue
        domain, severity, critical = rule
        active = not resolved
        # Open or unconfirmed critical events do not age out. Resolved events
        # provide historical risk context for 365 days after verified cure.
        if active or (as_of - resolution).days <= 365:
            contribution = severity if active else max(1, severity // 2)
            points[domain] = max(points[domain], contribution)
        if critical and active:
            alerts.append({"code": kind, "date": item["date"], "isin": item.get("isin"),
                           "source_url": item["source_url"], "status": effective})
    return verified, unverified, alerts, points, observations


def _rating_points(rating, as_of):
    if not _fresh_evidence(rating, as_of, MAX_FINANCIAL_AGE_DAYS):
        return None
    grade = str(rating.get("grade", "")).upper().strip()
    # Accept common national-scale forms: ruBBB-, BBB-.ru, BBB-|ru|, BB(RU).
    grade = re.sub(r"^RU", "", grade)
    grade = re.sub(r"(?:[.]?RU|\(RU\)|\[RU\]|\|RU\|)$", "", grade).strip()
    match = re.fullmatch(r"(AAA|AA|A|BBB|BB|B|CCC|CC|C|RD|SD|D)([+-]?)", grade)
    if not match:
        return None
    base, modifier = match.groups()
    points = {"AAA": 0, "AA": 0, "A": 0, "BBB": 1, "BB": 2, "B": 3,
              "CCC": 4, "CC": 5, "C": 5, "RD": 5, "SD": 5, "D": 5}[base]
    if rating.get("outlook") == "negative":
        points += 1
    return min(5, points)


def _band(score):
    return ("low" if score < 25 else "elevated" if score < 50
            else "high" if score < 75 else "critical")


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
    if not _source(source):
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
    verified, unverified, alerts, event_points, observations = _classify_events(issuer, as_of)
    points = dict(event_points)
    coverage = dict.fromkeys(DOMAIN_WEIGHTS, False)
    ratio_leverage = ratio_interest = liquidity = None
    scenarios = {}
    financials_complete = not missing
    if financials_complete:
        cash = metrics["cash_unrestricted_rub"]
        credit = metrics["committed_undrawn_lines_rub"]
        debt = metrics["net_corporate_debt_rub"]
        ebitda = metrics["ebitda_ltm_rub"]
        interest = metrics["interest_expense_ltm_rub"]
        principal = metrics["principal_due_12m_rub"]
        cfo = metrics["cfo_ltm_rub"]
        ratio_leverage = debt / ebitda if ebitda > 0 else None
        ratio_interest = ebitda / interest if interest > 0 else None
        liquidity = (cash + credit) / principal if principal > 0 else None
        # Separate the two debt indicators; zero interest with positive EBITDA
        # is not evidence of weak coverage. Negative EBITDA is allowed.
        lev_points = (12 if ratio_leverage is None or ratio_leverage >= 6 else
                      9 if ratio_leverage >= 4 else 4 if ratio_leverage >= 2 else 0)
        cover_points = (8 if ebitda <= 0 or (ratio_interest is not None and ratio_interest < 1)
                        else 5 if ratio_interest is not None and ratio_interest < 1.5
                        else 3 if ratio_interest is not None and ratio_interest < 2 else 0)
        points["debt"] = max(points["debt"], lev_points + cover_points)
        coverage["debt"] = True
        # The 12m model expects post-interest CFO. When interest is classified
        # as financing, deduct documented forward cash interest once.
        treatment = financials.get("cfo_includes_cash_interest")
        cash_interest = _money(financials, "cash_interest_due_12m_rub", missing, required=False)
        interest_known = treatment is True or (treatment is False and cash_interest is not None)
        interest_adjustment = cash_interest if treatment is False and cash_interest is not None else 0
        if not interest_known:
            missing.append("cash_interest_treatment_unverified")
        post_interest_cfo = cfo - interest_adjustment
        cfo_measure = post_interest_cfo if interest_known else cfo
        cfo_points = (20 if cfo_measure < 0 and (debt <= 0 or -cfo_measure / debt >= .1)
                      else 15 if cfo_measure < 0 else 5 if debt > 0 and cfo_measure / debt < .05 else 0)
        points["cashflow"] = max(points["cashflow"], cfo_points)
        coverage["cashflow"] = interest_known
        baseline = cash + credit + cfo - interest_adjustment - principal
        stressed_cfo = cfo * (.8 if cfo >= 0 else 1.2)
        stressed = cash + credit + stressed_cfo - interest_adjustment - principal
        no_credit = cash + cfo - interest_adjustment - principal
        scenarios = {
            "baseline_liquidity_surplus_12m_rub": _round(baseline, 2),
            "operating_stress_20pct_surplus_12m_rub": _round(stressed, 2),
            "no_new_credit_lines_surplus_12m_rub": _round(no_credit, 2),
        }
        # A modeled gap is a warning, not proof of a confirmed unpaid obligation.
        liquidity_points = (25 if baseline < 0 else 20 if stressed < 0
                            else 15 if liquidity is not None and liquidity < .5
                            else 10 if liquidity is not None and liquidity < 1
                            else 5 if no_credit < 0 else 0)
        points["liquidity"] = max(points["liquidity"], liquidity_points)
        coverage["liquidity"] = interest_known
        if baseline < 0:
            observations.append("modeled_12m_funding_gap" if interest_known else "conditional_12m_funding_gap")
        elif stressed < 0:
            observations.append("modeled_12m_stress_gap" if interest_known else "conditional_12m_stress_gap")
    reviews = issuer.get("risk_reviews") or {}
    for domain in REVIEW_DOMAINS:
        # A dated explicit review is needed before zero findings are meaningful.
        review = reviews.get(domain)
        coverage[domain] = _fresh_evidence(review, as_of, MAX_REVIEW_AGE_DAYS) and review.get("complete") is True
        if not coverage[domain]:
            missing.append("risk_review_missing_or_stale:" + domain)
    rating = issuer.get("rating") or {}
    rp = _rating_points(rating, as_of)
    coverage["rating"] = rp is not None
    if rp is None:
        missing.append("rating_unverified_or_unavailable")
    else:
        points["rating"] = max(points["rating"], rp)
    observed = sum(points.values())
    covered_weight = sum(weight for d, weight in DOMAIN_WEIGHTS.items() if coverage[d])
    score_status = "complete" if covered_weight == 100 else "partial_lower_bound"
    # Financials missing: no authoritative overall numeric score in the legacy field.
    score = observed if financials_complete else None
    band = _band(observed)
    if alerts:
        risk_band = "critical"
    elif band in ("critical", "high"):
        risk_band = band
    elif score_status != "complete":
        risk_band = "insufficient_data"
    else:
        risk_band = band
    control = ("critical_review" if alerts or risk_band == "critical" else
               "enhanced_review" if risk_band == "high" or not all(coverage.values()) or observations
               else "routine_review")
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
        "score_status": score_status, "observed_risk_points_lower_bound": observed,
        "score_coverage_weight_pct": covered_weight,
        "domain_scores": {d: {"points": points[d], "weight": w, "covered": coverage[d]}
                          for d, w in DOMAIN_WEIGHTS.items()},
        "critical_alerts": alerts, "review_priority": control,
        "early_warning_observations": observations,
        "cashflow_scenarios": scenarios,
        "cashflow_scenario_status": ("conditional_interest_assumption" if "cash_interest_treatment_unverified" in missing
                                     else "scenario_not_forecast") if scenarios else "unavailable",
        "missing_or_stale": sorted(set(missing)),
        "verified_events": verified, "unverified_events_count": len(unverified),
        "rating": issuer.get("rating"),
    }


def analyze(payload: dict) -> dict:
    if payload.get("schema_version") not in ("1", "2"):
        raise ValueError("schema_version must equal '1' or '2'")
    as_of = _date(payload["as_of"])
    issuers = payload.get("issuers")
    if not isinstance(issuers, list) or not issuers:
        raise ValueError("issuers must be a nonempty list")
    results = []
    for issuer in issuers:
        x = analyze_issuer(issuer, as_of)
        x["bonds"] = [analyze_bond(b, as_of) for b in issuer.get("bonds", [])]
        for bond in x["bonds"]:
            events = [a for a in x["critical_alerts"] if a["code"] in CREDIT_EVENT_TYPES
                      and (not a["isin"] or a["isin"].upper() == bond["isin"])]
            if events:
                bond["yield_to_maturity_pct"] = None
                bond["yield_to_put_pct"] = None
                bond["yield_status"] = "blocked_active_or_unresolved_credit_event"
                bond["warnings"] = sorted(set(bond["warnings"] + ["contractual_yield_not_reliable_due_to_credit_event"]))
        results.append(x)
    return {
        "module": "Bond Credit Risk Analyzer", "version": VERSION,
        "as_of": as_of.isoformat(), "issuers": results,
        "scoring_version": SCORING_VERSION, "domain_weights": DOMAIN_WEIGHTS,
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
        lines.extend([f"- Приоритет: **{x['review_priority']}**; покрытие модели: {x['score_coverage_weight_pct']}%; статус: {x['score_status']}",
                      f"- Наблюдаемые баллы (нижняя граница): {x['observed_risk_points_lower_bound']} / 100",
                      "- Факторы: " + "; ".join(f"{k} {v['points']}/{v['weight']}" + ("" if v["covered"] else " (данные неполные)") for k, v in x["domain_scores"].items()), ""])
        for alert in x["critical_alerts"]:
            lines.append(f"- **КРИТИЧЕСКИЙ СИГНАЛ:** {alert['code']}, {alert['date']}, {alert['status']}; источник: {alert['source_url']}")
        for obs in x["early_warning_observations"]:
            lines.append(f"- Предупреждение: {obs}")
        if x["cashflow_scenarios"]:
            lines.extend([f"**Стресс ликвидности на 12 месяцев (руб., сценарный баланс): {x['cashflow_scenario_status']}**", ""])
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
