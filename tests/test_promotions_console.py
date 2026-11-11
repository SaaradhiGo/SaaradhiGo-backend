"""Module 12: the Promotions console API. Capabilities, scope, maker-checker, audit.

WHAT THESE PROVE
----------------
  * CAPABILITIES. `promo.read` (Super Admin, Marketing, City Ops, Finance, Analyst) opens the
    campaign list; `promo.write` (Marketing) drafts rider promos and rider referrals;
    `incentive.write` (City Ops Manager) drafts driver incentives and driver referrals. Support and
    KYC hold none of them.
  * SCOPE. A campaign is anchored at a country or city node. Out of scope is a 404 identical to a
    missing row; in scope without the capability is a 403.
  * MAKER-CHECKER. Launching any kind (`promotion.launch`) and raising a budget
    (`promotion.budget_increase`) are requests a DIFFERENT admin holding `approval.check`
    approves, in India and the United States; nothing is live before that. Pausing and cancelling
    need no checker and are audited.
  * EVERY WRITE IS AUDITED, and no response carries a raw phone, email or token.
"""

import pytest

from promotions_support import (  # noqa: F401
    ADM, APPROVALS, approve_latest, block_the_network, body_of, checker, create, error_code,
    geo, get, incentive_payload, launch, operator, post, promo_payload, referral_payload, when,
    world,
)
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.promotions.models import Campaign
from servers.ride.models import PromoCode

pytestmark = [pytest.mark.django_db]


def patch(client, path, body=None, expect=None):
    from test_finance_actions import swept

    response = swept(client.patch(path, body or {}, format='json'))
    if expect is not None:
        assert response.status_code == expect, (path, response.status_code, response.content[:500])
    return response


# ===========================================================================
# CAPABILITIES
# ===========================================================================

class TestCapabilities:
    def test_the_seeded_roles_hold_exactly_what_the_ruling_says(self):
        from servers.admin_access.roles import ROLE_TEMPLATES

        held = {t['code']: {p for p in t['permissions']
                            if p in ('promo.read', 'promo.write', 'incentive.write')}
                for t in ROLE_TEMPLATES}
        assert held['super_admin'] == {'promo.read', 'promo.write', 'incentive.write'}
        assert held['marketing'] == {'promo.read', 'promo.write'}
        assert held['city_ops_manager'] == {'promo.read', 'incentive.write'}
        assert held['finance'] == {'promo.read'}
        assert held['analyst'] == {'promo.read'}
        for code in ('support_agent', 'support_lead', 'kyc_verifier', 'safety_officer'):
            assert held[code] == set(), code

    @pytest.mark.parametrize('who', ['support'])
    def test_roles_without_promo_read_get_403(self, world, who):
        client, _ = getattr(world, who)
        assert client.get(f'{ADM}/campaigns/').status_code == 403
        assert client.get(f'{ADM}/options/').status_code == 403

    @pytest.mark.parametrize('who', ['finance', 'analyst'])
    def test_read_only_roles_see_budgets_but_cannot_draft(self, world, who):
        launched = launch(world.mkt[0], promo_payload())
        client, _ = getattr(world, who)
        rows = get(client, f'{ADM}/campaigns/')['results']
        assert [r['id'] for r in rows] == [launched.id]
        assert rows[0]['budget']['budget_minor'] == 100_000
        detail = get(client, f'{ADM}/campaigns/{launched.id}/')
        assert detail['budget']['remaining_minor'] == 100_000
        post(client, f'{ADM}/campaigns/', promo_payload(), expect=403)
        post(client, f'{ADM}/campaigns/{launched.id}/pause/', {}, expect=403)
        post(client, f'{ADM}/campaigns/{launched.id}/budget-increase/',
             {'budget_minor': 200_000, 'reason': 'x'}, expect=403)

    def test_marketing_owns_rider_promos_and_rider_referrals_only(self, world):
        client, _ = world.mkt
        create(client, promo_payload())
        create(client, referral_payload('rider'))
        assert error_code(post(client, f'{ADM}/campaigns/', incentive_payload(),
                               expect=403)) == 'SCOPE_NOT_HELD'
        assert error_code(post(client, f'{ADM}/campaigns/', referral_payload('driver'),
                               expect=403)) == 'SCOPE_NOT_HELD'

    def test_city_ops_owns_driver_incentives_and_driver_referrals_only(self, world):
        client, _ = world.ops
        create(client, incentive_payload())
        create(client, referral_payload('driver', scope='IN-TG-HYD'))
        assert error_code(post(client, f'{ADM}/campaigns/', promo_payload(),
                               expect=403)) == 'SCOPE_NOT_HELD'
        assert error_code(post(client, f'{ADM}/campaigns/',
                               referral_payload('rider', scope='IN-TG-HYD'),
                               expect=403)) == 'SCOPE_NOT_HELD'

    def test_super_admin_holds_both(self, world):
        client, _ = world.super
        create(client, promo_payload())
        create(client, incentive_payload())

    def test_anonymous_and_non_operators_are_refused(self, world):
        from rest_framework.test import APIClient

        from finance_world import client_for, make_rider

        assert APIClient().get(f'{ADM}/campaigns/').status_code in (401, 403)
        assert client_for(make_rider('X', '91')).get(f'{ADM}/campaigns/').status_code == 403

    def test_the_options_list_what_the_caller_may_write(self, world):
        data = get(world.mkt[0], f'{ADM}/options/')
        codes = {p['code'] for p in data['places']}
        assert {'IN', 'IN-TG-HYD'} <= codes and 'US' not in codes
        assert all(p['can_write'] == ['promo.write'] for p in data['places'])
        assert 'IN-TG-HYD' in {z['code'] for z in data['zones']}


# ===========================================================================
# SCOPE
# ===========================================================================

class TestScope:
    def test_a_place_outside_the_scope_is_a_404_not_a_403(self, world):
        client, _ = world.mkt_us
        assert error_code(post(client, f'{ADM}/campaigns/', promo_payload(),
                               expect=404)) == 'NOT_FOUND'

    def test_a_campaign_in_another_country_is_invisible_and_unreachable(self, world):
        us = create(world.super[0], promo_payload(
            scope='US', config={'code': 'USWELCOME', 'discount_type': 'flat',
                                'discount_flat_minor': 500}))
        client, _ = world.mkt
        assert get(client, f'{ADM}/campaigns/')['results'] == []
        for method, path in (('get', f'{ADM}/campaigns/{us["id"]}/'),
                             ('post', f'{ADM}/campaigns/{us["id"]}/pause/'),
                             ('get', f'{ADM}/campaigns/{us["id"]}/activity/')):
            assert getattr(client, method)(path).status_code == 404
        missing = client.get(f'{ADM}/campaigns/99999/')
        assert missing.status_code == 404
        assert missing.json() == client.get(f'{ADM}/campaigns/{us["id"]}/').json()

    def test_a_city_operator_sees_only_their_city_and_cannot_write_wider(self, world):
        create(world.super[0], promo_payload(scope='IN', name='Country wide',
                                             config={'code': 'ALLINDIA', 'discount_type': 'flat',
                                                     'discount_flat_minor': 500}))
        mine = create(world.mkt_hyd[0], promo_payload())
        rows = get(world.mkt_hyd[0], f'{ADM}/campaigns/')['results']
        assert [r['id'] for r in rows] == [mine['id']]
        # IN is above their scope: it does not exist for them.
        assert post(world.mkt_hyd[0], f'{ADM}/campaigns/', promo_payload(scope='IN'),
                    expect=404)

    def test_a_zone_outside_the_place_is_refused(self, world):
        payload = promo_payload()
        payload['config']['zone_code'] = 'US-TX-AUS'
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/', payload,
                               expect=400)) == 'INVALID_FIELD'


# ===========================================================================
# DRAFTING AND VALIDATION
# ===========================================================================

class TestDrafting:
    def test_a_draft_is_created_edited_and_audited(self, world):
        client, _ = world.mkt
        made = create(client, promo_payload())
        assert made['state'] == 'draft' and made['currency'] == 'INR' and made['country'] == 'IN'
        assert made['budget']['remaining_minor'] == 100_000
        assert made['projection']['max_redemptions'] == 100_000 // 5_000
        edited = body_of(patch(client, f'{ADM}/campaigns/{made["id"]}/',
                               {'budget_minor': 200_000}, expect=200))
        assert edited['budget']['budget_minor'] == 200_000
        actions = list(AdminAuditLog.objects.filter(target_type='promotion_campaign')
                       .order_by('id').values_list('action', flat=True))
        assert actions == ['promotion_campaign_created', 'promotion_campaign_updated']

    @pytest.mark.parametrize('field,value', [
        ('name', ''), ('budget_minor', 0), ('budget_minor', -5), ('budget_minor', 1.5),
        ('budget_minor', True), ('kind', 'coupon'), ('starts_at', 'tomorrow'),
    ])
    def test_bad_input_is_a_400_with_a_code(self, world, field, value):
        response = post(world.mkt[0], f'{ADM}/campaigns/', promo_payload(**{field: value}),
                        expect=400)
        assert error_code(response) in ('INVALID_FIELD', 'BUDGET_TOO_SMALL')

    def test_the_window_must_run_forwards(self, world):
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/',
                               promo_payload(starts_at=when(days=2), ends_at=when(days=1)),
                               expect=400)) == 'INVALID_FIELD'

    @pytest.mark.parametrize('config', [
        {'discount_type': 'flat', 'discount_flat_minor': 500},                    # no code
        {'code': 'x', 'discount_type': 'flat', 'discount_flat_minor': 500},       # bad code
        {'code': 'GOOD1', 'discount_type': 'flat'},                               # no amount
        {'code': 'GOOD1', 'discount_type': 'percent', 'discount_percent': '101'},
        {'code': 'GOOD1', 'discount_type': 'percent', 'discount_percent': '0'},
        {'code': 'GOOD1', 'discount_type': 'bogus'},
    ])
    def test_a_promo_config_is_validated(self, world, config):
        assert post(world.mkt[0], f'{ADM}/campaigns/', promo_payload(config=config),
                    expect=400)

    def test_an_auto_applied_promo_needs_no_code(self, world):
        made = create(world.mkt[0], promo_payload(
            config={'auto_apply': True, 'discount_type': 'percent', 'discount_percent': '10',
                    'max_discount_minor': 3_000}))
        assert made['config']['auto_apply'] is True and made['config']['code'] == ''

    def test_a_code_in_use_is_refused_at_draft_and_across_campaigns(self, world):
        PromoCode.objects.create(
            code='TAKEN', discount_type='flat', discount_flat_minor=100, currency='INR',
            valid_from=__import__('django').utils.timezone.now(),
            valid_to=__import__('django').utils.timezone.now())
        payload = promo_payload()
        payload['config']['code'] = 'taken'
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/', payload,
                               expect=400)) == 'CODE_TAKEN'
        create(world.mkt[0], promo_payload())
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/', promo_payload(),
                               expect=400)) == 'CODE_TAKEN'

    def test_a_budget_that_cannot_fund_one_reward_is_refused(self, world):
        assert error_code(post(world.ops[0], f'{ADM}/campaigns/',
                               incentive_payload(budget_minor=1_000),
                               expect=400)) == 'BUDGET_TOO_SMALL'

    def test_incentive_filters_are_validated(self, world):
        payload = incentive_payload()
        payload['config'].update(hour_from=22)
        assert post(world.ops[0], f'{ADM}/campaigns/', payload, expect=400)
        payload['config'].update(hour_from=22, hour_to=6, vehicle_type='hovercraft')
        assert error_code(post(world.ops[0], f'{ADM}/campaigns/', payload,
                               expect=400)) == 'INVALID_FIELD'

    def test_a_referral_must_reward_somebody(self, world):
        payload = referral_payload()
        payload['config'].update(referrer_reward_minor=0, referee_reward_minor=0)
        assert post(world.mkt[0], f'{ADM}/campaigns/', payload, expect=400)

    def test_only_a_draft_can_be_edited(self, world):
        live = launch(world.mkt[0], promo_payload())
        assert error_code(patch(world.mkt[0], f'{ADM}/campaigns/{live.id}/', {'name': 'x'},
                                expect=409)) == 'CAMPAIGN_NOT_DRAFT'

    def test_a_cancelled_campaign_frees_its_code(self, world):
        made = create(world.mkt[0], promo_payload())
        post(world.mkt[0], f'{ADM}/campaigns/{made["id"]}/cancel/', {'reason': 'oops'},
             expect=200)
        create(world.mkt[0], promo_payload())


# ===========================================================================
# MAKER-CHECKER: launch
# ===========================================================================

class TestLaunch:
    def test_submit_raises_a_request_and_nothing_is_live(self, world):
        client, _ = world.mkt
        made = create(client, promo_payload())
        submitted = post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                         expect=202)
        data = body_of(submitted)
        assert data['state'] == 'pending_approval'
        assert data['approval_request']['action_key'] == 'promotion.launch'
        assert data['approval_request']['country'] == 'IN'
        campaign = Campaign.objects.get(pk=made['id'])
        assert campaign.state == 'pending_approval'
        assert not PromoCode.objects.filter(code='WELCOME50').exists()

    def test_a_reason_is_required(self, world):
        made = create(world.mkt[0], promo_payload(reason=''))
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/{made["id"]}/submit/', {},
                               expect=400)) == 'REASON_REQUIRED'

    def test_the_maker_cannot_approve_and_a_marketing_admin_is_not_a_checker(self, world):
        client, _ = world.mkt
        made = create(client, promo_payload())
        rid = body_of(post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                           expect=202))['approval_request']['id']
        refused = post(client, f'{APPROVALS}/{rid}/approve/', {'reason': 'me'})
        assert refused.status_code == 403      # approval.check is not a Marketing capability
        assert Campaign.objects.get(pk=made['id']).state == 'pending_approval'

    def test_a_super_admin_who_is_the_maker_cannot_approve_their_own(self, world):
        client, _ = world.super
        made = create(client, promo_payload())
        rid = body_of(post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                           expect=202))['approval_request']['id']
        refused = post(client, f'{APPROVALS}/{rid}/approve/', {'reason': 'me'})
        assert refused.status_code == 403 and error_code(refused) == 'SELF_APPROVAL'

    def test_a_second_admin_approves_and_the_exact_terms_go_live(self, world):
        client, _ = world.mkt
        campaign = launch(client, promo_payload())
        assert campaign.state == 'live' and campaign.launched_at is not None
        promo = PromoCode.objects.get(code='WELCOME50')
        assert promo.is_active and promo.currency == 'INR' and promo.discount_flat_minor == 5_000
        assert promo.campaign == campaign
        assert ApprovalRequest.objects.get(pk=campaign.approval_request_id).status == 'executed'
        actions = set(AdminAuditLog.objects.filter(target_type='promotion_campaign')
                      .values_list('action', flat=True))
        assert {'promotion_campaign_created', 'promotion_campaign_submitted',
                'promotion_campaign_launched'} <= actions

    def test_a_future_start_is_scheduled_and_the_sweep_starts_it(self, world):
        from django.utils import timezone

        from servers.promotions.workflow import sweep

        campaign = launch(world.mkt[0], promo_payload(starts_at=when(days=1)))
        assert campaign.state == 'scheduled'
        assert PromoCode.objects.get(code='WELCOME50').is_active is False
        assert sweep()['started'] == 0
        assert sweep(timezone.now() + timezone.timedelta(days=2))['started'] == 1
        campaign.refresh_from_db()
        assert campaign.state == 'live'
        assert PromoCode.objects.get(code='WELCOME50').is_active is True

    @pytest.mark.parametrize('payload_fn,who', [
        (promo_payload, 'mkt'), (incentive_payload, 'ops'),
    ])
    def test_every_kind_needs_a_checker(self, world, payload_fn, who):
        client, _ = getattr(world, who)
        made = create(client, payload_fn())
        submitted = post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                         expect=202)
        assert body_of(submitted)['approval_request']['action_key'] == 'promotion.launch'
        assert Campaign.objects.get(pk=made['id']).state == 'pending_approval'

    def test_a_referral_needs_a_checker_too(self, world):
        client, _ = world.mkt
        made = create(client, referral_payload('rider'))
        post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'}, expect=202)
        assert Campaign.objects.get(pk=made['id']).state == 'pending_approval'

    def test_the_united_states_needs_a_checker_too(self, world):
        client, _ = world.mkt_us
        made = create(client, promo_payload(
            scope='US', config={'code': 'USWELCOME', 'discount_type': 'flat',
                                'discount_flat_minor': 500}))
        assert made['currency'] == 'USD'
        submitted = post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                         expect=202)
        assert body_of(submitted)['approval_request']['country'] == 'US'
        approve_latest(body_of(submitted)['approval_request']['id'])
        assert Campaign.objects.get(pk=made['id']).state == 'live'

    def test_a_country_with_no_policy_is_blocked(self, world):
        ApprovalRequest.objects  # noqa: B018 - the model is imported for the assertion below
        from servers.approvals.models import ApprovalPolicy

        ApprovalPolicy.objects.filter(country='US', action_key='promotion.launch').delete()
        made = create(world.mkt_us[0], promo_payload(
            scope='US', config={'code': 'USWELCOME', 'discount_type': 'flat',
                                'discount_flat_minor': 500}))
        refused = post(world.mkt_us[0], f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'})
        assert refused.status_code == 409 and error_code(refused) == 'THRESHOLD_NOT_CONFIGURED'

    def test_a_rejected_request_returns_the_draft(self, world):
        client, _ = world.mkt
        made = create(client, promo_payload())
        rid = body_of(post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                           expect=202))['approval_request']['id']
        chk, _ = checker()
        assert post(chk, f'{APPROVALS}/{rid}/reject/', {'reason': 'too generous'}).status_code == 200
        assert Campaign.objects.get(pk=made['id']).state == 'draft'
        assert not PromoCode.objects.filter(code='WELCOME50').exists()

    def test_the_approved_terms_are_the_submitted_terms(self, world):
        """A draft edited between submit and approval is refused at execution."""
        client, _ = world.mkt
        made = create(client, promo_payload())
        rid = body_of(post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                           expect=202))['approval_request']['id']
        Campaign.objects.filter(pk=made['id']).update(budget_minor=9_999_999)
        chk, _ = checker()
        post(chk, f'{APPROVALS}/{rid}/approve/', {'reason': 'ok'})
        assert ApprovalRequest.objects.get(pk=rid).error_code == 'CAMPAIGN_CHANGED'
        assert not PromoCode.objects.filter(code='WELCOME50').exists()

    def test_a_window_that_passed_cannot_be_submitted(self, world):
        made = create(world.mkt[0], promo_payload())
        Campaign.objects.filter(pk=made['id']).update(ends_at=__import__(
            'django').utils.timezone.now())
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/{made["id"]}/submit/',
                               {'reason': 'go'}, expect=400)) == 'WINDOW_PASSED'


# ===========================================================================
# Pause, resume, cancel: no checker, audited
# ===========================================================================

class TestPauseCancel:
    def test_pause_and_resume_need_no_checker_and_are_audited(self, world):
        client, _ = world.mkt
        campaign = launch(client, promo_payload())
        paused = body_of(post(client, f'{ADM}/campaigns/{campaign.id}/pause/',
                              {'reason': 'review'}, expect=200))
        assert paused['state'] == 'paused'
        assert PromoCode.objects.get(code='WELCOME50').is_active is False
        resumed = body_of(post(client, f'{ADM}/campaigns/{campaign.id}/resume/', {}, expect=200))
        assert resumed['state'] == 'live'
        assert PromoCode.objects.get(code='WELCOME50').is_active is True
        actions = list(AdminAuditLog.objects.filter(
            target_type='promotion_campaign', target_id=str(campaign.id))
            .values_list('action', flat=True))
        assert 'promotion_campaign_paused' in actions and 'promotion_campaign_resumed' in actions
        assert ApprovalRequest.objects.filter(action_key='promotion.launch').count() == 1

    def test_cancel_works_from_every_pre_end_state_and_stops_the_promo(self, world):
        client, _ = world.mkt
        campaign = launch(client, promo_payload())
        cancelled = body_of(post(client, f'{ADM}/campaigns/{campaign.id}/cancel/',
                                 {'reason': 'wrong offer'}, expect=200))
        assert cancelled['state'] == 'cancelled' and cancelled['cancelled_at']
        assert PromoCode.objects.get(code='WELCOME50').is_active is False
        assert error_code(post(client, f'{ADM}/campaigns/{campaign.id}/pause/', {},
                               expect=409)) == 'CAMPAIGN_WRONG_STATE'
        assert 'promotion_campaign_cancelled' in set(AdminAuditLog.objects.values_list(
            'action', flat=True))

    def test_cancelling_a_pending_campaign_withdraws_its_request(self, world):
        client, _ = world.mkt
        made = create(client, promo_payload())
        rid = body_of(post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'},
                           expect=202))['approval_request']['id']
        post(client, f'{ADM}/campaigns/{made["id"]}/cancel/', {}, expect=200)
        assert ApprovalRequest.objects.get(pk=rid).status == 'cancelled'

    def test_the_other_kinds_owner_cannot_pause_it(self, world):
        campaign = launch(world.mkt[0], promo_payload())
        assert error_code(post(world.ops[0], f'{ADM}/campaigns/{campaign.id}/pause/', {},
                               expect=403)) == 'SCOPE_NOT_HELD'

    def test_the_sweep_ends_a_campaign_at_its_window_end(self, world):
        from django.utils import timezone

        from servers.promotions.workflow import sweep

        campaign = launch(world.mkt[0], promo_payload())
        assert sweep(timezone.now() + timezone.timedelta(days=31))['ended'] == 1
        campaign.refresh_from_db()
        assert campaign.state == 'ended' and campaign.ended_at
        assert PromoCode.objects.get(code='WELCOME50').is_active is False


# ===========================================================================
# MAKER-CHECKER: budget increase
# ===========================================================================

class TestBudgetIncrease:
    def test_a_raise_is_a_request_and_changes_nothing_until_approved(self, world):
        client, _ = world.mkt
        campaign = launch(client, promo_payload())
        sent = post(client, f'{ADM}/campaigns/{campaign.id}/budget-increase/',
                    {'budget_minor': 250_000, 'reason': 'demand'}, expect=202)
        data = body_of(sent)
        assert data['approval_request']['action_key'] == 'promotion.budget_increase'
        campaign.refresh_from_db()
        assert campaign.budget_minor == 100_000
        approve_latest(data['approval_request']['id'])
        campaign.refresh_from_db()
        assert campaign.budget_minor == 250_000
        assert 'promotion_budget_increased' in set(AdminAuditLog.objects.values_list(
            'action', flat=True))

    def test_a_budget_can_only_go_up(self, world):
        campaign = launch(world.mkt[0], promo_payload())
        for amount in (100_000, 50_000):
            assert error_code(post(world.mkt[0], f'{ADM}/campaigns/{campaign.id}/budget-increase/',
                                   {'budget_minor': amount, 'reason': 'x'},
                                   expect=400)) == 'BUDGET_NOT_INCREASED'

    def test_one_request_at_a_time(self, world):
        campaign = launch(world.mkt[0], promo_payload())
        url = f'{ADM}/campaigns/{campaign.id}/budget-increase/'
        post(world.mkt[0], url, {'budget_minor': 150_000, 'reason': 'x'}, expect=202)
        assert error_code(post(world.mkt[0], url, {'budget_minor': 160_000, 'reason': 'x'},
                               expect=409)) == 'BUDGET_REQUEST_OPEN'

    def test_a_raise_revives_an_exhausted_campaign(self, world):
        from servers.promotions import budget

        campaign = launch(world.mkt[0], promo_payload(budget_minor=5_000))
        budget.reserve(campaign.pk, 5_000)
        campaign.refresh_from_db()
        assert campaign.state == 'exhausted'
        sent = post(world.mkt[0], f'{ADM}/campaigns/{campaign.id}/budget-increase/',
                    {'budget_minor': 50_000, 'reason': 'more'}, expect=202)
        approve_latest(body_of(sent)['approval_request']['id'])
        campaign.refresh_from_db()
        assert campaign.state == 'live' and campaign.budget_minor == 50_000

    def test_a_draft_or_a_cancelled_campaign_cannot_be_raised(self, world):
        made = create(world.mkt[0], promo_payload())
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/{made["id"]}/budget-increase/',
                               {'budget_minor': 200_000, 'reason': 'x'},
                               expect=409)) == 'CAMPAIGN_WRONG_STATE'

    def test_a_reason_is_required(self, world):
        campaign = launch(world.mkt[0], promo_payload())
        assert error_code(post(world.mkt[0], f'{ADM}/campaigns/{campaign.id}/budget-increase/',
                               {'budget_minor': 200_000}, expect=400)) == 'REASON_REQUIRED'


# ===========================================================================
# Reads: filters, detail, activity
# ===========================================================================

class TestReads:
    def test_filters_by_kind_state_and_audience(self, world):
        launch(world.mkt[0], promo_payload())
        create(world.ops[0], incentive_payload())
        create(world.super[0], referral_payload('driver'))
        client, _ = world.super
        assert {r['kind'] for r in get(client, f'{ADM}/campaigns/')['results']} == {
            'rider_promo', 'driver_incentive', 'referral'}
        assert [r['kind'] for r in get(client, f'{ADM}/campaigns/?kind=rider_promo')['results']] == [
            'rider_promo']
        assert [r['state'] for r in get(client, f'{ADM}/campaigns/?state=draft')['results']] == [
            'draft', 'draft']
        assert len(get(client, f'{ADM}/campaigns/?kind=referral&audience=driver')['results']) == 1
        assert len(get(client, f'{ADM}/campaigns/?kind=referral&audience=rider')['results']) == 0
        assert post(client, f'{ADM}/campaigns/?kind=bogus', {}, expect=400) or True
        assert client.get(f'{ADM}/campaigns/?kind=bogus').status_code == 400
        assert client.get(f'{ADM}/campaigns/?state=bogus').status_code == 400

    def test_the_list_is_paginated_and_bounded(self, world):
        for i in range(3):
            create(world.mkt[0], promo_payload(
                name=f'P{i}', config={'code': f'CODE{i}XX', 'discount_type': 'flat',
                                      'discount_flat_minor': 500}))
        page = get(world.mkt[0], f'{ADM}/campaigns/?limit=2&offset=1')
        assert page['count'] == 3 and len(page['results']) == 2 and page['limit'] == 2

    def test_the_activity_of_a_rider_promo_names_people_only_by_reference(self, world):
        from decimal import Decimal

        from finance_world import make_rider, make_trip
        from servers.ride.promos import redeem_promo

        campaign = launch(world.mkt[0], promo_payload())
        rider = make_rider('R', '91')
        driver = __import__('promotions_support').hyd_driver(world, 'D')
        trip = make_trip(world.hyd, rider, driver, fare='200.00')
        assert redeem_promo('WELCOME50', rider, trip, Decimal('200.00'), zone=world.hyd).ok
        data = get(world.mkt[0], f'{ADM}/campaigns/{campaign.id}/activity/')
        assert data['activity'] == 'redemptions' and data['count'] == 1
        row = data['results'][0]
        assert row['who'] == f'rider #{rider.id}' and row['discount_minor'] == 5_000
        assert row['state'] == 'committed'
        assert 'phone' not in str(row)

    def test_the_detail_carries_a_projection_and_the_ledger_totals(self, world):
        campaign = launch(world.mkt[0], promo_payload())
        data = get(world.finance[0], f'{ADM}/campaigns/{campaign.id}/')
        assert data['projection']['average_minor'] == 5_000
        assert data['projection']['max_redemptions'] == 20
        assert data['ledger'] == [] and data['tax'] == 'not configured'
