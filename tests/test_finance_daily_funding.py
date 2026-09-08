from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from wb_manager.finance_daily_funding import build_daily_funding_basis
from wb_manager.finance_running_account import RunningAccount, cash_event, refund_event, sale_event


DAY = date(2026, 9, 6)
SOURCES = ('finance_detailed', 'statistics_sales', 'statistics_orders', 'goods_returns',
           'advertising_costs', 'advertising_payments')


def successful_state(through='2026-09-06'):
    return {'through': through, 'issues': [], 'sourceStatuses': [], 'sources': {'sourceCoverage': [
        {'store_alias': 'first', 'source': source, 'period_start': '2026-08-01',
         'period_end': through, 'coverage_status': 'complete', 'snapshot_hash': source + '-facts',
         'metadata_json': '{"httpStatus":200}'} for source in SOURCES]}}


class Ledger:
    def __init__(self, path):
        self.path, self.actions = path, []

    def query_funding_actions(self, **kwargs):
        return [row for row in self.actions if row['store_alias'] == kwargs['store_alias']]

    def find_reports(self, **kwargs):
        raise AssertionError('Weekly reports must never be read')


@pytest.fixture
def setup(tmp_path):
    ledger = Ledger(tmp_path / 'fixture.sqlite3')
    path = tmp_path / 'fixture.running.sqlite3'
    account = RunningAccount(path)
    account.register_store('first', '2026-08-01')
    account.register_store('second', '2026-08-01')
    path.with_suffix('.state.json').write_text(json.dumps(successful_state()), encoding='utf-8')
    for metric in ('promotion_accrued', 'shared_pool_return_cost', 'cash_ad_spend'):
        account.set_coverage('first', 'CNY', metric, '2026-08-01', '2026-09-06', True, ('coverage',))
    now = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)
    service = SimpleNamespace(
        repo_root=tmp_path, ledger=ledger, store_aliases=['first', 'second'],
        periodic_standard=SimpleNamespace(raw={'runningAccounts': {'database': str(path)}}),
        policy=SimpleNamespace(reporting_policy_id='policy-current', reporting_currency='CNY',
                               advertising_funding_source='balance'),
        _now=lambda: now,
        _funding_activation_info=lambda alias, required=False: {
            'at': datetime(2026, 8, 1, tzinfo=timezone.utc), 'source': 'fixture-platform-event'},
        _confirmed_deposit_effective_at=lambda action: datetime.fromisoformat(action['requested_at']))
    yield service, account, path
    account.close()


def sale(identity='sale', amount='100', day='2026-08-20', store='first'):
    return sale_event(store, identity, day, order_id=identity, evidence=(identity,), amounts={
        'gross_sales': '1000', 'seller_proceeds': '900', 'product_cost': '600',
        'agency_accrued': '20', 'promotion_accrued': amount, 'shared_pool_accrued': amount,
        'return_reserve_accrued': '0'}, metadata={'basis': 'platform_settlement',
        'policyId': 'historical-policy', 'costVersionId': 'historical-cost',
        'reserveAllocationMode': 'LEGACY_SHARED_POOL'})


def action(identity, amount, status='readback_confirmed', **evidence):
    return {'action_id': identity, 'store_alias': 'first', 'action_type': 'advertising_top_up',
            'requested_amount_cny': str(amount), 'requested_at': '2026-09-07T08:00:00+00:00',
            'lifecycle_status': status,
            'evidence_json': json.dumps({'schemaVersion': 2, 'currency': 'CNY', 'source': 'balance',
                                         'amountCny': str(amount), **evidence})}


def calculate(setup, **kwargs):
    return build_daily_funding_basis(setup[0], store_alias='first', through=DAY, **kwargs)


def test_daily_cash_allowance_not_double_deducting_consumption(setup):
    service, account, _ = setup
    account.upsert_event(sale())
    account.upsert_event(cash_event('first', 'freight', '2026-08-21', metric='shared_pool_return_cost',
        amount=10, evidence=('rrd',), metadata={'basis': 'platform_settlement'}))
    account.upsert_event(cash_event('first', 'spend', '2026-09-05', metric='cash_ad_spend',
        amount=20, evidence=('ad-1',), metadata={'basis': 'official_ad_expense'}))
    service.ledger.actions = [action('fund', 30)]
    result = calculate(setup)
    assert result['availableCashCny'] == '60.00'
    assert result['unspentBookBudgetCny'] == '70.00'
    assert result['cashSpendCny'] == '20.00'
    assert result['fundingAllowed']
    assert result['currency'] == 'CNY'


def test_signed_partial_refunds_original_policy_and_negative_preserved(setup):
    _, account, _ = setup
    account.upsert_event(sale(amount='10.01'))
    old = calculate(setup)
    for index, fraction in enumerate(('0.333333', '0.333333', '0.333334')):
        account.upsert_event(refund_event('first', f'return-{index}', '2026-09-02',
            original_sale_id='sale', refund_fraction=fraction, saleable_return=True, evidence=('return',)))
    setup[0].ledger.actions = [action('fund', 3)]
    result = calculate(setup)
    assert result['grossConfirmedAccrualCny'] == '0.00'
    assert result['availableCashCny'] == '-3.00'
    assert result['fingerprint'] != old['fingerprint']
    assert any(x['code'] == 'NEGATIVE_CONFIRMED_CASH_ALLOWANCE' for x in result['blockers'])


def test_unsettled_and_forged_estimates_do_not_add_cash_or_block_confirmed_subset(setup):
    _, account, _ = setup
    account.upsert_event(sale())
    account.upsert_event(replace(sale('stat', amount='900'), metadata={'basis': 'management_estimate'}))
    account.upsert_event(replace(sale('unsettled'), amounts={'promotion_accrued': None}))
    account.set_coverage('first', 'CNY', 'promotion_accrued', '2026-09-01', '2026-09-06', False)
    result = calculate(setup)
    assert result['availableCashCny'] == '100.00'
    assert result['completeness']['excludedEventCount'] == 2
    assert not result['completeness']['complete']
    assert result['fundingAllowed']


def test_seven_actual_awaiting_settlement_shapes_are_excluded_not_signed_adjustments(setup):
    _, account, _ = setup
    account.upsert_event(sale())
    # Canonical adapter shape: a CNY placeholder with known original cost,
    # but no official CNY income, proceeds or reserve. Not a provisional copy.
    for index in range(7):
        event = sale(f'statistics-line-{index}', day='2026-09-05')
        account.upsert_event(replace(event, amounts={
            'agency_accrued': None, 'gross_sales': None, 'product_cost': '2500.00',
            'promotion_accrued': None, 'return_reserve_accrued': None,
            'seller_proceeds': None, 'shared_pool_accrued': None},
            evidence=(f'WB statistics saleID:synthetic-{index}',), metadata={
                **event.metadata, 'basis': 'awaiting_settlement', 'quantity': 1,
                'createdAt': '2026-08-31T08:00:00Z', 'srid': f'exact-line-{index}'}))
    result = calculate(setup)
    assert result['availableCashCny'] == '100.00'
    assert result['completeness']['excludedEventCount'] == 7
    assert result['blockers'] == []
    assert result['fundingAllowed']
    assert any(row['code'] == 'UNSETTLED_POSITIVE_ACCRUALS_EXCLUDED' for row in result['warnings'])
    assert any(row['code'] == 'NEW_BASIS_REQUIRES_NEW_EXACT_APPROVAL' for row in result['warnings'])


def test_missing_refund_original_blocks_other_known_positive_facts(setup):
    _, account, _ = setup
    account.upsert_event(sale())
    account.upsert_event(refund_event('first', 'unknown-return', '2026-09-02', original_sale_id='missing'))
    result = calculate(setup)
    assert result['availableCashCny'] == '100.00'
    assert not result['fundingAllowed']
    assert any(x['code'] == 'ORIGINAL_SALE_MISSING' for x in result['blockers'])


def test_store_currency_and_future_event_isolation(setup):
    _, account, _ = setup
    account.upsert_event(sale())
    baseline = calculate(setup)
    account.upsert_event(sale('other', store='second'))
    account.upsert_event(sale('future', day='2026-09-07'))
    assert calculate(setup)['fingerprint'] == baseline['fingerprint']
    account.upsert_event(replace(sale('rub'), currency='RUB'))
    result = calculate(setup)
    assert result['availableCashCny'] == '100.00'
    assert result['completeness']['excludedEventCount'] == 1


def test_mixed_cashback_allocation_and_promotion_expense_excluded(setup):
    service, account, _ = setup
    account.upsert_event(sale())
    account.upsert_event(cash_event('first', 'promo', '2026-09-01', metric='promo_credit_spend',
        amount='300', evidence=('promo',), metadata={'basis': 'official_ad_expense'}))
    service.ledger.actions = [action('mixed', 50, schemaVersion=3, cashAmountCny='15', cashbackAmountCny='35')]
    result = calculate(setup)
    assert result['availableCashCny'] == '85.00'
    assert result['confirmedCashAllocatedCny'] == '15.00'
    assert result['confirmedCashbackAllocatedCny'] == '35.00'


@pytest.mark.parametrize('status', ['executing', 'unknown', 'executed', 'readback_pending'])
def test_unknown_attempt_locks_cash_and_promo_even_expired_or_excluded(setup, status):
    service, account, _ = setup
    account.upsert_event(sale())
    service.ledger.actions = [action('mixed', 50, status, schemaVersion=3, cashAmountCny='15',
        cashbackAmountCny='35', expiresAt='2026-09-01T00:00:00+00:00')]
    result = calculate(setup, exclude_action_id='mixed')
    assert result['lockedCashCny'] == '15.00'
    assert result['lockedCashbackCny'] == '35.00'
    assert result['availableCashCny'] == '85.00'
    assert not result['fundingAllowed']


def test_expired_unattempted_approval_releases_but_current_approval_reserves(setup):
    service, account, _ = setup
    account.upsert_event(sale())
    service.ledger.actions = [action('expired', 50, 'approved', expiresAt='2026-09-07T09:00:00+00:00'),
        action('active', 20, 'approved', expiresAt='2026-09-07T11:00:00+00:00')]
    result = calculate(setup)
    assert result['availableCashCny'] == '80.00'
    assert result['lockedCashCny'] == '20.00'
    assert calculate(setup, exclude_action_id='active')['availableCashCny'] == '100.00'


@pytest.mark.parametrize('evidence', [
    {'schemaVersion': 3}, {'schemaVersion': 3, 'cashAmountCny': '10', 'cashbackAmountCny': '10'},
    {'source': 'unknown'}, {'currency': 'RUB'}, {'store': 'second'}])
def test_bad_funding_split_fails_closed_not_assumed_cash_or_promo(setup, evidence):
    service, account, _ = setup
    account.upsert_event(sale())
    service.ledger.actions = [action('bad', 50, **evidence)]
    result = calculate(setup)
    assert not result['fundingAllowed']
    assert any(x['code'] == 'FUNDING_CASH_SPLIT_UNRESOLVED' for x in result['blockers'])


def test_revisions_replace_and_repeat_reads_do_not_modify_database(setup):
    _, account, path = setup
    account.upsert_event(sale(), recorded_at='2026-09-01T00:00:00Z')
    first = calculate(setup)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert calculate(setup)['fingerprint'] == first['fingerprint']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    account.upsert_event(sale(amount='120'), expected_revision=1, correction_reason='official correction',
                         recorded_at='2026-09-02T00:00:00Z')
    second = calculate(setup)
    assert second['availableCashCny'] == '120.00'
    assert first['fingerprint'] != second['fingerprint']


def test_coverage_recapture_same_facts_not_new_fingerprint_and_changed_policy_is(setup):
    service, account, _ = setup
    account.upsert_event(sale())
    first = calculate(setup)
    account.set_coverage('first', 'CNY', 'promotion_accrued', '2026-08-01', '2026-09-06', True, ('recapture',))
    assert calculate(setup)['fingerprint'] == first['fingerprint']
    service.policy.reporting_policy_id = 'new-policy'
    assert calculate(setup)['fingerprint'] != first['fingerprint']


def test_separate_return_pool_never_consumes_promotion_budget(setup):
    _, account, _ = setup
    account.upsert_event(sale())
    account.upsert_event(cash_event('first', 'risk', '2026-09-02', metric='separate_return_cost', amount=80,
        evidence=('rrd',), metadata={'basis': 'platform_settlement'}))
    assert calculate(setup)['availableCashCny'] == '100.00'


def test_unknown_return_pool_and_unclassified_ad_cost_block(setup):
    _, account, _ = setup
    account.upsert_event(sale())
    account.upsert_event(cash_event('first', 'return', '2026-09-02', metric='logistics_cost', amount=10,
        evidence=('rrd',), metadata={'basis': 'platform_settlement', 'description': 'return_logistics'}))
    account.upsert_event(cash_event('first', 'unknown-ad', '2026-09-02', metric='cash_ad_spend', amount=None,
        evidence=('ad',), metadata={'basis': 'official_ad_expense'}))
    codes = {x['code'] for x in calculate(setup)['blockers']}
    assert 'RETURN_POOL_POLICY_UNRESOLVED' in codes
    assert 'UNRESOLVED_CASH_AD_SPEND' in codes


def test_missing_account_never_creates_a_database(setup):
    service, _, path = setup
    absent = path.with_name('does-not-exist.sqlite3')
    service.periodic_standard.raw['runningAccounts']['database'] = str(absent)
    result = calculate(setup)
    assert not result['fundingAllowed']
    assert not absent.exists()


def test_confirmed_allocation_after_business_cutoff_still_consumes_current_allowance(setup):
    service, account, _ = setup
    account.upsert_event(sale())
    service.ledger.actions = [action('after-cutoff', 30)]
    result = calculate(setup, exclude_action_id='after-cutoff')
    assert result['availableCashCny'] == '70.00'
    assert result['confirmedCashAllocatedCny'] == '30.00'


def test_state_cutoff_is_explicit_and_unrelated_future_issues_do_not_change_basis(setup):
    _, account, path = setup
    account.upsert_event(sale())
    state_path = path.with_suffix('.state.json')
    state_path.write_text(json.dumps(successful_state('2026-09-05')), encoding='utf-8')
    stale = calculate(setup)
    assert stale['cutoffView'] == 'stale'
    assert any(x['code'] == 'RUNNING_ACCOUNT_CUTOFF_STALE' for x in stale['blockers'])
    state_path.write_text(json.dumps(successful_state()), encoding='utf-8')
    current = calculate(setup)
    future = successful_state()
    future.update({'through': '2026-09-07', 'issues': [
        {'storeAlias': 'first', 'eventDate': '2026-09-07', 'reason': 'multiple_refunds_require_explicit_partial_linkage'},
        {'storeAlias': 'second', 'eventDate': '2026-09-02', 'reason': 'multiple_refunds_require_explicit_partial_linkage'}]})
    state_path.write_text(json.dumps(future), encoding='utf-8')
    historical = calculate(setup)
    assert historical['cutoffView'] == 'historical'
    assert historical['fingerprint'] == current['fingerprint']


def test_changed_original_cost_fact_invalidates_same_reserve_approval(setup):
    _, account, _ = setup
    original = sale()
    account.upsert_event(original, recorded_at='2026-09-01T00:00:00Z')
    first = calculate(setup)
    changed = replace(original, amounts={**original.amounts, 'product_cost': '650'})
    account.upsert_event(changed, expected_revision=1, correction_reason='correct cost evidence',
                         recorded_at='2026-09-02T00:00:00Z')
    second = calculate(setup)
    assert first['availableCashCny'] == second['availableCashCny']
    assert first['fingerprint'] != second['fingerprint']


def test_real_failed_collection_shape_cannot_masquerade_as_refreshed_allowance(setup):
    _, account, path = setup
    account.upsert_event(sale())
    state = successful_state()
    state['sourceStatuses'] = [{'storeAlias': 'first', 'requestedFrom': '2026-08-01',
        'requestedThrough': '2026-09-06', 'primaryComplete': False, 'sources': [
        {'source': source, 'complete': False, 'status': None, 'rows': 0, 'inserted': 0,
         'blocker': 'WbApiError: Network error [WinError 10013]', 'changed': False,
         'coverageChanged': False, 'contentChanged': False} for source in SOURCES]}]
    # Even retained older coverage marked complete must not mask the failed refresh.
    path.with_suffix('.state.json').write_text(json.dumps(state), encoding='utf-8')
    result = calculate(setup)
    assert result['availableCashCny'] == '100.00'
    assert not result['fundingAllowed']
    failures = [x for x in result['blockers'] if x['code'] == 'REQUIRED_SOURCE_COLLECTION_FAILED']
    assert {x['source'] for x in failures} == set(SOURCES)


@pytest.mark.parametrize('source', SOURCES)
def test_each_negative_sensitive_collection_must_cover_cutoff(setup, source):
    _, account, path = setup
    account.upsert_event(sale())
    state = successful_state()
    for row in state['sources']['sourceCoverage']:
        if row['source'] == source:
            row['period_end'] = '2026-09-05'
    path.with_suffix('.state.json').write_text(json.dumps(state), encoding='utf-8')
    result = calculate(setup)
    assert not result['fundingAllowed']
    assert any(x.get('source') == source and x['code'] == 'REQUIRED_SOURCE_NOT_COVERED' for x in result['blockers'])
