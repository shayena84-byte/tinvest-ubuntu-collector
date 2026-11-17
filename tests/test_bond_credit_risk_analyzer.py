"""Offline standard-library regression tests; no network or orders."""
import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bond_credit_risk_analyzer import analyze, analyze_bond, analyze_issuer, _xirr, to_markdown, DOMAIN_WEIGHTS

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
                       'cfo_ltm_rub': -50_000_000, 'cfo_includes_cash_interest': True},
        'rating': {'grade': 'BBB-', 'outlook': 'negative', 'date': '2026-11-10', 'verified': True, 'source_url': 'https://example.org/rating'},
        'risk_reviews': {k: {'date': '2026-11-15', 'verified': True, 'complete': True, 'source_url': 'https://example.org/review'} for k in ('governance', 'tax_legal', 'payment')},
        'events': [], 'bonds': []
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


class EarlyWarningTests(unittest.TestCase):
    def event(self, kind, **extra):
        return {'date': '2026-11-01', 'type': kind, 'verified': True,
                'source_url': 'https://example.org/disclosure', **extra}

    def test_seven_weighted_domains(self):
        x = analyze_issuer(full_issuer(), DAY)
        self.assertEqual(sum(DOMAIN_WEIGHTS.values()), 100)
        self.assertEqual(x['score_coverage_weight_pct'], 100)
        self.assertEqual(x['score_status'], 'complete')
        self.assertEqual(sum(v['points'] for v in x['domain_scores'].values()), x['risk_score_heuristic_0_100'])
        self.assertTrue(all(v['points'] <= v['weight'] for v in x['domain_scores'].values()))

    def test_critical_event_with_missing_financials(self):
        for kind in ('coupon_delay', 'principal_delay', 'unresolved_default', 'material_account_freeze',
                     'bankruptcy_filing', 'funding_gap_uncovered', 'material_audit_concern'):
            with self.subTest(kind=kind):
                x = analyze_issuer({'name': 'Synthetic', 'events': [self.event(kind)]}, DAY)
                self.assertEqual(x['risk_band'], 'critical')
                self.assertEqual(x['review_priority'], 'critical_review')
                self.assertIsNone(x['risk_score_heuristic_0_100'])
                self.assertEqual(x['critical_alerts'][0]['code'], kind)

    def test_stale_financials_do_not_hide_deferral(self):
        a = full_issuer()
        a['financials_as_of'] = '2025-01-01'
        a['events'] = [self.event('principal_delay')]
        x = analyze_issuer(a, DAY)
        self.assertEqual(x['risk_band'], 'critical')
        self.assertIn('stale_financials', x['missing_or_stale'])

    def test_old_unresolved_never_ages_out(self):
        a = full_issuer()
        a['events'] = [self.event('unresolved_default', date='2024-10-01')]
        self.assertEqual(analyze_issuer(a, DAY)['risk_band'], 'critical')

    def test_verified_cure_removes_override_not_history(self):
        a = full_issuer()
        a['events'] = [self.event('coupon_delay', status='resolved',
                     resolved_date='2026-11-12', resolution_verified=True,
                     resolution_source_url='https://example.org/paid')]
        x = analyze_issuer(a, DAY)
        self.assertEqual(x['critical_alerts'], [])
        self.assertGreater(x['domain_scores']['payment']['points'], 0)

    def test_unsupported_cure_remains_alert(self):
        a = full_issuer()
        a['events'] = [self.event('principal_delay', status='resolved')]
        self.assertEqual(analyze_issuer(a, DAY)['critical_alerts'][0]['status'], 'unknown')

    def test_future_resolution_rejected(self):
        a = full_issuer()
        a['events'] = [self.event('coupon_delay', status='resolved',
                     resolved_date='2026-11-18', resolution_verified=True,
                     resolution_source_url='https://example.org/paid')]
        with self.assertRaises(ValueError):
            analyze_issuer(a, DAY)

    def test_absent_reviews_never_imply_low_risk(self):
        a = full_issuer()
        a['financials'].update(cash_unrestricted_rub=2_000_000_000, net_corporate_debt_rub=0,
                              cfo_ltm_rub=100_000_000, interest_expense_ltm_rub=0)
        a.pop('risk_reviews')
        a.pop('rating')
        x = analyze_issuer(a, DAY)
        self.assertEqual(x['risk_band'], 'insufficient_data')
        self.assertEqual(x['score_coverage_weight_pct'], 65)
        self.assertEqual(x['review_priority'], 'enhanced_review')

    def test_governance_and_tax_flags_without_duplicate_inflation(self):
        a = full_issuer()
        a['events'] = [self.event('opaque_related_party_transaction')]*3 + [self.event('material_tax_claim')]
        x = analyze_issuer(a, DAY)
        self.assertEqual(x['domain_scores']['governance']['points'], 10)
        self.assertEqual(x['domain_scores']['tax_legal']['points'], 8)

    def test_rating_national_scale_and_small_weight(self):
        for grade in ('ruB', 'B.ru', 'B(RU)', 'B[ru]', 'B-|ru|'):
            a = full_issuer()
            a['rating']['grade'] = grade
            x = analyze_issuer(a, DAY)
            self.assertEqual(x['domain_scores']['rating']['points'], 4, grade)
        a['rating']['grade'] = 'C[ru]'
        self.assertEqual(analyze_issuer(a, DAY)['domain_scores']['rating']['points'], 5)

    def test_negative_ebitda_and_no_interest(self):
        a = full_issuer()
        a['financials'].update(ebitda_ltm_rub=-100, interest_expense_ltm_rub=0)
        x = analyze_issuer(a, DAY)
        self.assertEqual(x['domain_scores']['debt']['points'], 20)
        self.assertIsNone(x['net_corporate_debt_to_ebitda'])

    def test_interest_not_double_counted(self):
        a = full_issuer()
        f = a['financials']
        f['cash_interest_due_12m_rub'] = 100_000_000
        self.assertEqual(analyze_issuer(a, DAY)['cashflow_scenarios']['baseline_liquidity_surplus_12m_rub'], -750_000_000)
        f['cfo_includes_cash_interest'] = False
        self.assertEqual(analyze_issuer(a, DAY)['cashflow_scenarios']['baseline_liquidity_surplus_12m_rub'], -850_000_000)
        del f['cfo_includes_cash_interest']
        x = analyze_issuer(a, DAY)
        self.assertEqual(x['cashflow_scenario_status'], 'conditional_interest_assumption')
        self.assertFalse(x['domain_scores']['liquidity']['covered'])

    def test_model_gap_not_confirmed_default(self):
        x = analyze_issuer(full_issuer(), DAY)
        self.assertIn('modeled_12m_funding_gap', x['early_warning_observations'])
        self.assertFalse(x['critical_alerts'])

    def test_affected_bond_yield_block_other_preserved(self):
        a = full_issuer()
        a['bonds'] = [bond(), {**bond(), 'isin': 'RU000A106631'}]
        a['events'] = [self.event('coupon_delay', isin='RU000A1092T7')]
        x = analyze({'schema_version': '2', 'as_of': DAY.isoformat(), 'issuers': [a]})['issuers'][0]
        self.assertEqual(x['bonds'][0]['yield_status'], 'blocked_active_or_unresolved_credit_event')
        self.assertIsNone(x['bonds'][0]['yield_to_maturity_pct'])
        self.assertEqual(x['bonds'][1]['yield_status'], 'calculated')

    def test_unknown_scope_restructuring_blocks_all_contractual_yields(self):
        a = full_issuer()
        a['bonds'] = [bond()]
        a['events'] = [self.event('restructuring_proposal')]
        self.assertIsNone(analyze({'schema_version': '1', 'as_of': DAY.isoformat(), 'issuers': [a]})['issuers'][0]['bonds'][0]['yield_to_maturity_pct'])

    def test_missing_cashflow_with_put_does_not_crash(self):
        b = bond()
        del b['cash_flows']
        b['put_offer'] = {'holder_put_right': True, 'date': '2027-01-01', 'redemption_pct': 100}
        self.assertIsNone(analyze_bond(b, DAY)['yield_to_put_pct'])

    def test_old_rating_is_unknown_not_safe(self):
        a = full_issuer()
        a['rating']['date'] = '2025-01-01'
        x = analyze_issuer(a, DAY)
        self.assertFalse(x['domain_scores']['rating']['covered'])
        self.assertIn('rating_unverified_or_unavailable', x['missing_or_stale'])


if __name__ == '__main__':
    unittest.main()
