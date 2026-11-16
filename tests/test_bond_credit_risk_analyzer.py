"""Offline standard-library regression tests; no network or orders."""
import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bond_credit_risk_analyzer import analyze, analyze_bond, analyze_issuer, _xirr, to_markdown

DAY = date(2026, 11, 16)


def full_issuer():
    return {
        'name': 'ДЕМО: синтетический девелопер', 'inn': '0000000000',
        'financials_as_of': '2026-09-30', 'financials_source_url': 'https://example.org/fictional-report',
        'financials': {'cash_unrestricted_rub': 100_000_000,
                       'cash_restricted_escrow_rub': 9_000_000_000,
                       'committed_undrawn_lines_rub': 200_000_000,
                       'net_corporate_debt_rub': 800_000_000,
                       'project_finance_debt_rub': 10_000_000_000,
                       'ebitda_ltm_rub': 200_000_000,
                       'interest_expense_ltm_rub': 150_000_000,
                       'principal_due_12m_rub': 1_000_000_000,
                       'cfo_ltm_rub': -50_000_000},
        'rating': {'grade': 'BBB-', 'outlook': 'negative'}, 'events': [], 'bonds': []
    }


def bond(years=1):
    maturity = DAY.replace(year=DAY.year + years)
    return {'isin': 'RU000A1092T7', 'ticker': 'DEMO', 'board': 'TQCB',
            'outstanding_principal_rub': 1000,
            'quote_as_of': '2026-11-13', 'quote_quality': 'valid',
            'quote_source_url': 'https://example.org/fictional-quote',
            'schedule_verified': True, 'cashflows_source_url': 'https://example.org/fictional-schedule',
            'dirty_price_rub': 1000,
            'cash_flows': [{'date': maturity.isoformat(), 'principal_rub': 1000,
                            'coupon_rub': 100}]}


class AnalyzerTests(unittest.TestCase):
    def test_bond_par_near_ten_percent(self):
        b = analyze_bond(bond(), DAY)
        self.assertEqual(b['yield_status'], 'calculated')
        self.assertAlmostEqual(b['yield_to_maturity_pct'], 10, delta=.04)

    def test_discount_yield_positive(self):
        b = bond()
        b['dirty_price_rub'] = 800
        x = analyze_bond(b, DAY)
        self.assertGreater(x['yield_to_maturity_pct'], 30)

    def test_clean_price_aci_uses_outstanding_nominal(self):
        b = bond()
        del b['dirty_price_rub']
        b['outstanding_principal_rub'] = 500
        b['cash_flows'][0]['principal_rub'] = 500
        b['clean_price_pct'] = 96.5
        b['accrued_coupon_rub'] = 11
        self.assertEqual(analyze_bond(b, DAY)['dirty_price_rub'], 493.5)

    def test_amortization_cash_flows(self):
        b = bond(2)
        b['cash_flows'] = [
            {'date': '2027-11-16', 'principal_rub': 400, 'coupon_rub': 100},
            {'date': '2028-11-16', 'principal_rub': 600, 'coupon_rub': 60}]
        x = analyze_bond(b, DAY)
        self.assertEqual(x['yield_status'], 'calculated')
        self.assertAlmostEqual(x['yield_to_maturity_pct'], 10, delta=.1)

    def test_put_yield_and_notice(self):
        b = bond(2)
        b['cash_flows'] = [
            {'date': '2027-11-16', 'principal_rub': 0, 'coupon_rub': 100},
            {'date': '2028-11-16', 'principal_rub': 1000, 'coupon_rub': 100}]
        b['put_offer'] = {'date': '2027-11-16', 'redemption_pct': 100,
                          'holder_put_right': True, 'requires_notice': True}
        x = analyze_bond(b, DAY)
        self.assertAlmostEqual(x['yield_to_put_pct'], 10, delta=.04)
        self.assertIn('put_requires_notice', x['warnings'])

    def test_no_issuer_call_option_as_holder_put(self):
        b = bond(2)
        b['put_offer'] = {'date': '2027-11-16', 'redemption_pct': 100,
                          'holder_put_right': False}
        x = analyze_bond(b, DAY)
        self.assertIsNone(x['yield_to_put_pct'])
        self.assertIn('offer_not_confirmed_as_holder_put', x['warnings'])

    def test_missing_schedule_source_blocks_yield(self):
        b = bond()
        b.pop('schedule_verified')
        self.assertIsNone(analyze_bond(b, DAY)['yield_to_maturity_pct'])

    def test_isin_checksum(self):
        b = bond()
        b['isin'] = 'RU000A1092T8'
        with self.assertRaises(ValueError):
            analyze_bond(b, DAY)

    def test_stale_quote_never_claims_yield(self):
        b = bond()
        b['quote_as_of'] = '2026-10-01'
        x = analyze_bond(b, DAY)
        self.assertIsNone(x['yield_to_maturity_pct'])
        self.assertEqual(x['yield_status'], 'blocked_incomplete_or_stale_data')

    def test_incomplete_amortization_blocks_yield(self):
        b = bond()
        b['cash_flows'][0]['principal_rub'] = 300
        self.assertIn('incomplete_principal_schedule', analyze_bond(b, DAY)['warnings'])
        self.assertIsNone(analyze_bond(b, DAY)['yield_to_maturity_pct'])

    def test_overpay_schedule_rejected(self):
        b = bond()
        b['cash_flows'][0]['principal_rub'] = 2000
        with self.assertRaises(ValueError):
            analyze_bond(b, DAY)

    def test_escrow_excluded_from_available_cash(self):
        x = analyze_issuer(full_issuer(), DAY)
        self.assertEqual(x['liquid_assets_to_12m_principal'], 0.3)
        self.assertEqual(x['cashflow_scenarios']['baseline_liquidity_surplus_12m_rub'], -750_000_000)
        self.assertEqual(x['cashflow_scenarios']['operating_stress_20pct_surplus_12m_rub'], -760_000_000)

    def test_negative_cashflow_accepted(self):
        x = analyze_issuer(full_issuer(), DAY)
        self.assertEqual(x['metrics']['cfo_ltm_rub'], -50_000_000)
        self.assertIn(x['risk_band'], ('high', 'critical'))

    def test_verified_payment_delay_increases_score(self):
        baseline = full_issuer()
        old = analyze_issuer(baseline, DAY)['risk_score_heuristic_0_100']
        baseline['events'] = [{'date': '2026-11-01', 'type': 'coupon_delay',
                               'verified': True, 'source_url': 'https://example.org/source'}]
        new = analyze_issuer(baseline, DAY)['risk_score_heuristic_0_100']
        self.assertGreater(new, old)

    def test_unsourced_event_does_not_change_score(self):
        original = full_issuer()
        old = analyze_issuer(original, DAY)['risk_score_heuristic_0_100']
        original['events'] = [{'date': '2026-11-01', 'type': 'unresolved_default',
                               'verified': True}]
        x = analyze_issuer(original, DAY)
        self.assertEqual(old, x['risk_score_heuristic_0_100'])
        self.assertEqual(x['unverified_events_count'], 1)

    def test_missing_and_old_financials_block_score(self):
        obj = full_issuer()
        del obj['financials']['cash_unrestricted_rub']
        self.assertEqual(analyze_issuer(obj, DAY)['risk_band'], 'insufficient_data')
        obj = full_issuer()
        obj['financials_as_of'] = '2025-12-31'
        self.assertEqual(analyze_issuer(obj, DAY)['risk_score_heuristic_0_100'], None)

    def test_case_study_reports(self):
        obj = {'schema_version': '1', 'as_of': DAY.isoformat(), 'issuers': [full_issuer()]}
        obj['issuers'][0]['bonds'] = [bond()]
        result = analyze(obj)
        self.assertEqual(result['broker_orders'], 0)
        self.assertIn('RU000A1092T7', to_markdown(result))

    def test_invalid_non_finite_rejected(self):
        b = bond()
        b['dirty_price_rub'] = float('nan')
        with self.assertRaises(ValueError):
            analyze_bond(b, DAY)

    def test_date_guard(self):
        b = bond()
        b['quote_as_of'] = '2026-12-01'
        with self.assertRaises(ValueError):
            analyze_bond(b, DAY)


if __name__ == '__main__':
    unittest.main()