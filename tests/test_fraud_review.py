"""Module 13: the review queue. Capability gates, scope, lifecycle, reason codes, the two-reviewer
race, linked accounts, hand-offs, the Riders-page flag, and that the subject is never told.

THE STOP LINE: confirming a flag records a decision and may keep `RiderFlag('promo_abuse')` in
step. It moves no money and notifies nobody; the hand-offs are links that the other modules gate.
"""

from unittest import mock

import pytest

from fraud_support import (  # noqa: F401
    ADM, block_the_network, body_of, client_for, error_code, flag_url, geo, get, hyd_driver,
    make_flag, make_rider, operator, post, world,
)
from servers.admin_audit.models import AdminAuditLog
from servers.fraud.models import FlagEvent, RiskFlag
from servers.rider.models import Rider, RiderFlag

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def rider_flag(world):
    rider = make_rider('F1', '91')
    return make_flag(rider, world.geo['IN-TG-HYD'])


def confirm(client, flag, reason='multi_accounting', note='Same handset, four accounts'):
    return post(client, flag_url(flag, 'confirm/'), {'reason_code': reason, 'note': note})


def dismiss(client, flag, reason='false_positive', note='A family sharing a phone'):
    return post(client, flag_url(flag, 'dismiss/'), {'reason_code': reason, 'note': note})


class TestCapabilitiesAndScope:
    def test_read_roles_see_the_queue_and_the_rest_are_refused(self, world, rider_flag):
        for who, expected in ((world.ops, 200), (world.mkt, 200), (world.mkt_hyd, 200),
                              (world.super, 200), (world.finance, 403), (world.analyst, 403),
                              (world.support, 403)):
            assert who[0].get(f'{ADM}/flags/').status_code == expected, who[1].full_name
            assert who[0].get(flag_url(rider_flag)).status_code == expected

    def test_marketing_is_read_only(self, world, rider_flag):
        for tail, body in (('confirm/', {'reason_code': 'other', 'note': 'long enough note'}),
                           ('dismiss/', {'reason_code': 'other', 'note': 'long enough note'}),
                           ('assign/', {}), ('unassign/', {}), ('note/', {'note': 'hello there'}),
                           ('reopen/', {'reason_code': 'other', 'note': 'long enough note'}),
                           ('handoff/', {'kind': 'rider_flag'})):
            assert post(world.mkt[0], flag_url(rider_flag, tail), body).status_code == 403
        assert RiskFlag.objects.get(pk=rider_flag.pk).status == 'open'

    def test_finance_and_analyst_cannot_review(self, world, rider_flag):
        for who in (world.finance, world.analyst, world.support):
            assert post(who[0], flag_url(rider_flag, 'assign/')).status_code == 403

    def test_out_of_scope_is_a_404_not_a_403(self, world, rider_flag):
        # Austin City Ops holds fraud.review, but not over Hyderabad.
        assert world.ops_us[0].get(flag_url(rider_flag)).status_code == 404
        assert post(world.ops_us[0], flag_url(rider_flag, 'confirm/'),
                    {'reason_code': 'other', 'note': 'long enough note'}).status_code == 404
        assert world.ops_us[0].get(f'{ADM}/flags/').json()['data']['count'] == 0
        assert world.ops[0].get(f'{ADM}/flags/').json()['data']['count'] == 1

    def test_a_missing_flag_looks_the_same_as_an_out_of_scope_one(self, world, rider_flag):
        a = world.ops_us[0].get(flag_url(rider_flag))
        b = world.ops_us[0].get(f'{ADM}/flags/999999/')
        assert (a.status_code, a.json()['error']['message']) == (
            b.status_code, b.json()['error']['message'])

    def test_a_country_scoped_marketer_sees_a_city_flag(self, world, rider_flag):
        assert world.mkt[0].get(flag_url(rider_flag)).status_code == 200
        assert world.mkt_us[0].get(flag_url(rider_flag)).status_code == 404

    def test_the_detail_tells_the_console_whether_to_show_actions(self, world, rider_flag):
        assert get(world.ops[0], flag_url(rider_flag))['may_review'] is True
        assert get(world.mkt[0], flag_url(rider_flag))['may_review'] is False


class TestLifecycle:
    def test_assign_to_self_then_unassign(self, world, rider_flag):
        data = body_of(post(world.ops[0], flag_url(rider_flag, 'assign/'), expect=200))
        assert data['status'] == 'in_review' and data['assigned_to']['id'] == world.ops[1].pk
        data = body_of(post(world.ops[0], flag_url(rider_flag, 'unassign/'), expect=200))
        assert data['status'] == 'open' and data['assigned_to'] is None
        assert [e['action'] for e in data['timeline']] == ['raised', 'assigned', 'unassigned']

    def test_assign_to_another_reviewer_in_scope(self, world, geo, rider_flag):
        other_client, other = operator('city_ops_manager', geo, 'IN-TG-HYD')
        listed = get(world.ops[0], flag_url(rider_flag, 'reviewers/'))['results']
        assert {r['id'] for r in listed} >= {world.ops[1].pk, other.pk}
        data = body_of(post(world.ops[0], flag_url(rider_flag, 'assign/'),
                            {'assignee_id': other.pk}, expect=200))
        assert data['assigned_to']['id'] == other.pk

    def test_cannot_assign_to_someone_who_cannot_review_or_see_it(self, world, rider_flag):
        for who in (world.mkt, world.finance, world.ops_us):
            response = post(world.ops[0], flag_url(rider_flag, 'assign/'),
                            {'assignee_id': who[1].pk})
            assert response.status_code == 400 and error_code(response) == 'ASSIGNEE_NOT_ELIGIBLE'
        assert post(world.ops[0], flag_url(rider_flag, 'assign/'),
                    {'assignee_id': 999999}).status_code == 400

    def test_a_note_is_on_the_timeline(self, world, rider_flag):
        data = body_of(post(world.ops[0], flag_url(rider_flag, 'note/'),
                            {'note': 'Called the city lead'}, expect=200))
        assert data['timeline'][-1]['action'] == 'note'
        assert data['timeline'][-1]['note'] == 'Called the city lead'

    def test_confirm_records_the_decision_and_audits_it(self, world, rider_flag):
        data = body_of(confirm(world.ops[0], rider_flag))
        assert data['status'] == 'confirmed' and data['decision_reason'] == 'multi_accounting'
        assert data['decided_by']['id'] == world.ops[1].pk
        last = data['timeline'][-1]
        assert (last['action'], last['from_status'], last['to_status'], last['reason']) == (
            'confirmed', 'open', 'confirmed', 'multi_accounting')
        assert last['actor']['id'] == world.ops[1].pk and last['note']
        assert AdminAuditLog.objects.filter(action='fraud_flag_confirmed',
                                            target_id=str(rider_flag.pk)).count() == 1

    def test_dismiss_from_in_review(self, world, rider_flag):
        post(world.ops[0], flag_url(rider_flag, 'assign/'), expect=200)
        data = body_of(dismiss(world.ops[0], rider_flag, 'legitimate_shared_device'))
        assert data['status'] == 'dismissed'
        assert [e['to_status'] for e in data['timeline']][-2:] == ['in_review', 'dismissed']

    def test_every_write_is_audited(self, world, rider_flag):
        post(world.ops[0], flag_url(rider_flag, 'assign/'), expect=200)
        post(world.ops[0], flag_url(rider_flag, 'note/'), {'note': 'checking the device'},
             expect=200)
        confirm(world.ops[0], rider_flag)
        post(world.ops[0], flag_url(rider_flag, 'reopen/'),
             {'reason_code': 'new_evidence', 'note': 'More accounts appeared'}, expect=200)
        post(world.ops[0], flag_url(rider_flag, 'unassign/'), expect=200)
        actions = set(AdminAuditLog.objects.filter(target_type='risk_flag').values_list(
            'action', flat=True))
        assert {'fraud_flag_assigned', 'fraud_flag_noted', 'fraud_flag_confirmed',
                'fraud_flag_reopened'} <= actions

    def test_reason_codes_are_required_and_must_fit_the_decision(self, world, rider_flag):
        ops = world.ops[0]
        assert error_code(post(ops, flag_url(rider_flag, 'confirm/'),
                               {'note': 'long enough note'})) == 'INVALID_REASON'
        # a dismiss code on a confirm, and the other way round
        assert error_code(confirm(ops, rider_flag, 'false_positive')) == 'INVALID_REASON'
        assert error_code(dismiss(ops, rider_flag, 'collusion')) == 'INVALID_REASON'
        assert error_code(confirm(ops, rider_flag, 'invented')) == 'INVALID_REASON'
        assert RiskFlag.objects.get(pk=rider_flag.pk).status == 'open'

    def test_a_note_of_five_to_five_hundred_characters_is_required(self, world, rider_flag):
        ops = world.ops[0]
        for note in ('', '    ', 'abcd', 'x' * 501):
            response = confirm(ops, rider_flag, note=note)
            assert response.status_code == 400 and error_code(response) == 'INVALID_NOTE'
        assert confirm(ops, rider_flag, note='abcde').status_code == 200

    def test_reopen_needs_a_reason_and_a_decided_flag(self, world, rider_flag):
        ops = world.ops[0]
        reopen = lambda **b: post(ops, flag_url(rider_flag, 'reopen/'), b)  # noqa: E731
        assert error_code(reopen(reason_code='new_evidence', note='not decided yet')) == \
            'NOT_DECIDED'
        dismiss(ops, rider_flag)
        assert error_code(reopen(note='no reason code given')) == 'INVALID_REASON'
        assert error_code(reopen(reason_code='new_evidence', note='')) == 'INVALID_NOTE'
        data = body_of(reopen(reason_code='new_evidence', note='Two more accounts appeared'))
        assert data['status'] == 'open' and data['decision_reason'] == ''
        assert data['timeline'][-1]['action'] == 'reopened'
        assert data['timeline'][-1]['reason'] == 'new_evidence'
        assert confirm(ops, rider_flag).status_code == 200

    def test_a_decided_flag_cannot_be_assigned_or_noted_into_a_new_state(self, world,
                                                                          rider_flag):
        confirm(world.ops[0], rider_flag)
        assert post(world.ops[0], flag_url(rider_flag, 'assign/')).status_code == 409


class TestTheRace:
    def test_the_second_reviewer_to_decide_gets_a_409(self, world, geo, rider_flag):
        first, second = world.ops[0], operator('city_ops_manager', geo, 'IN-TG-HYD')[0]
        assert confirm(first, rider_flag).status_code == 200
        late = dismiss(second, rider_flag)
        assert late.status_code == 409 and error_code(late) == 'ALREADY_DECIDED'
        flag = RiskFlag.objects.get(pk=rider_flag.pk)
        assert flag.status == 'confirmed' and flag.decision_reason == 'multi_accounting'
        assert FlagEvent.objects.filter(flag=flag, action__in=['confirmed', 'dismissed']).count() == 1

    def test_the_decision_takes_the_flag_row_lock(self, world, rider_flag):
        """The decision runs inside a transaction that locks the row first, so on PostgreSQL two
        reviewers serialise and the second sees the first one's status."""
        from servers.fraud import review

        with mock.patch.object(review.RiskFlag.objects, 'select_for_update',
                               wraps=review.RiskFlag.objects.select_for_update) as locked:
            confirm(world.ops[0], rider_flag)
        assert locked.called


class TestTheRidersPageStaysInStep:
    def _referral_flag(self, world, tag='R1'):
        rider = make_rider(tag, '91')
        rider_row = Rider.objects.get(user_id=rider)
        RiderFlag.objects.create(rider=rider_row, reason='promo_abuse', note='Referral 9 blocked')
        flag = make_flag(rider, world.geo['IN-TG-HYD'], kind='referral_abuse')
        return rider_row, flag

    def test_dismissing_clears_the_promo_abuse_flag_with_the_same_note(self, world):
        rider_row, flag = self._referral_flag(world)
        dismiss(world.ops[0], flag, 'false_positive', 'Siblings, one phone')
        cleared = RiderFlag.objects.get(rider=rider_row, reason='promo_abuse')
        assert cleared.cleared_at is not None and cleared.clear_note == 'Siblings, one phone'

    def test_confirming_keeps_it_set(self, world):
        rider_row, flag = self._referral_flag(world, 'R2')
        confirm(world.ops[0], flag, 'promo_abuse', 'Confirmed referral ring')
        assert RiderFlag.objects.filter(rider=rider_row, reason='promo_abuse',
                                        cleared_at__isnull=True).count() == 1

    def test_confirming_sets_it_with_the_reviewers_note_if_missing(self, world):
        rider = make_rider('R3', '91')
        flag = make_flag(rider, world.geo['IN-TG-HYD'], kind='multi_accounting')
        confirm(world.ops[0], flag, 'promo_abuse', 'Four accounts, one handset')
        active = RiderFlag.objects.get(rider__user_id=rider, reason='promo_abuse',
                                       cleared_at__isnull=True)
        assert active.note == 'Four accounts, one handset'

    def test_dismissing_keeps_it_while_another_promotion_flag_is_confirmed(self, world):
        rider_row, flag = self._referral_flag(world, 'R4')
        make_flag(rider_row.user_id, world.geo['IN-TG-HYD'], kind='multi_accounting',
                  status='confirmed')
        dismiss(world.ops[0], flag)
        assert RiderFlag.objects.filter(rider=rider_row, reason='promo_abuse',
                                        cleared_at__isnull=True).exists()

    def test_other_kinds_and_drivers_do_not_touch_the_riders_page(self, world):
        rider = make_rider('R5', '91')
        flag = make_flag(rider, world.geo['IN-TG-HYD'], kind='payment_abuse')
        confirm(world.ops[0], flag, 'payment_abuse', 'Five failed payments')
        assert not RiderFlag.objects.filter(rider__user_id=rider).exists()
        driver = hyd_driver(world, 'D1')
        flag = make_flag(driver.user_id, world.geo['IN-TG-HYD'], kind='collusion')
        assert confirm(world.ops[0], flag, 'collusion', 'Same rider, 14 trips').status_code == 200


class TestLinkedAccounts:
    def test_linked_by_device_and_phone_with_masked_labels_only(self, world):
        subject = make_rider('L1', '91')
        twin = make_rider('L2', '91')
        sibling = make_rider('L3', '91')
        for user in (subject, twin):
            user.fcm_token = 'push-token-secret-value'
            user.save(update_fields=['fcm_token'])
        sibling.emergency_contact = subject.phone_number
        sibling.save(update_fields=['emergency_contact'])
        flag = make_flag(subject, world.geo['IN-TG-HYD'])
        response = world.ops[0].get(flag_url(flag))
        data = response.json()['data']['linked']
        by_ref = {a['ref']: a for a in data['accounts']}
        assert by_ref[f'rider #{twin.pk}']['signals'] == ['same_device']
        assert by_ref[f'rider #{sibling.pk}']['signals'] == ['same_emergency_contact']
        assert by_ref[f'rider #{twin.pk}']['status'] == 'active'
        text = response.content.decode()
        for secret in ('push-token-secret-value', subject.phone_number, twin.phone_number,
                       subject.phone_number[-10:]):
            assert secret not in text

    def test_status_shows_a_blocked_rider_as_suspended(self, world):
        from servers.rider.models import RiderBlock

        subject, twin = make_rider('L4', '91'), make_rider('L5', '91')
        for user in (subject, twin):
            user.fcm_token = 'device-aaaa'
            user.save(update_fields=['fcm_token'])
        RiderBlock.objects.create(rider=Rider.objects.get(user_id=twin), reason_code='fraud')
        flag = make_flag(subject, world.geo['IN-TG-HYD'])
        accounts = get(world.ops[0], flag_url(flag))['linked']['accounts']
        assert accounts[0]['status'] == 'suspended'

    def test_an_account_outside_the_callers_scope_is_counted_not_named(self, world):
        from finance_world import make_driver  # noqa: F401

        subject = make_rider('L6', '91')
        austin_twin = make_rider('L7', '1')
        from servers.admin_access.models import GeoNode

        Rider.objects.filter(user_id=austin_twin).update(
            home_city=GeoNode.objects.get(code='US-TX-AUS'))
        for user in (subject, austin_twin):
            user.fcm_token = 'device-bbbb'
            user.save(update_fields=['fcm_token'])
        flag = make_flag(subject, world.geo['IN-TG-HYD'])
        seen = get(world.ops[0], flag_url(flag))['linked']
        assert seen['accounts'] == [] and seen['outside_scope'] == 1
        assert seen['outside_scope_label'] == '1 account outside your scope'
        assert f'#{austin_twin.pk}' not in str(seen)
        everything = get(world.super[0], flag_url(flag))['linked']
        assert everything['outside_scope'] == 0
        assert [a['ref'] for a in everything['accounts']] == [f'rider #{austin_twin.pk}']

    def test_drivers_sharing_a_payout_instrument_are_linked(self, world):
        a, b = hyd_driver(world, 'LA'), hyd_driver(world, 'LB')
        b.upi_id = a.upi_id
        b.save(update_fields=['upi_id'])
        flag = make_flag(a.user_id, world.geo['IN-TG-HYD'], kind='collusion')
        response = world.ops[0].get(flag_url(flag))
        accounts = response.json()['data']['linked']['accounts']
        assert [(x['ref'], x['signals']) for x in accounts] == [
            (f'driver #{b.user_id_id}', ['same_instrument'])]
        assert a.upi_id not in response.content.decode()

    def test_counterparts_named_in_the_evidence_are_listed(self, world):
        driver = hyd_driver(world, 'LC')
        rider = make_rider('L8', '91')
        flag = make_flag(driver.user_id, world.geo['IN-TG-HYD'], kind='collusion', evidence={
            'signals': ['repeat_pair'], 'refs': {'counterpart_user_ids': [rider.pk],
                                                 'trip_ids': [11, 12]}, 'measures': {}})
        data = get(world.ops[0], flag_url(flag))
        assert data['linked']['accounts'][0]['signals'] == ['named_in_evidence']
        assert [t['id'] for t in data['related']['trips']] == [11, 12]


class TestHandoffs:
    def test_a_handoff_is_offered_only_on_a_confirmed_flag_and_by_capability(self, world):
        driver = hyd_driver(world, 'H1')
        flag = make_flag(driver.user_id, world.geo['IN-TG-HYD'], kind='collusion')
        before = get(world.ops[0], flag_url(flag))['handoffs']
        assert all(not h['available'] for h in before)
        confirm(world.ops[0], flag, 'collusion', 'Same rider, 14 trips')
        by_kind = {h['kind']: h for h in get(world.ops[0], flag_url(flag))['handoffs']}
        assert by_kind['driver_suspend']['available'] is True
        assert by_kind['driver_suspend']['href'] == f'/drivers/{driver.pk}'
        assert by_kind['driver_deactivate']['available'] is False      # City Ops lacks it
        assert by_kind['finance_adjustment']['available'] is False
        assert 'rider_block' not in by_kind                            # a driver subject
        supers = {h['kind']: h for h in get(world.super[0], flag_url(flag))['handoffs']}
        assert supers['driver_deactivate']['available'] and supers['finance_adjustment']['available']

    def test_taking_one_is_recorded_and_does_nothing_else(self, world):
        from servers.driver.models import DriverSuspension

        driver = hyd_driver(world, 'H2')
        flag = make_flag(driver.user_id, world.geo['IN-TG-HYD'], kind='collusion')
        early = post(world.ops[0], flag_url(flag, 'handoff/'), {'kind': 'driver_suspend'})
        assert early.status_code == 409 and error_code(early) == 'NOT_CONFIRMED'
        confirm(world.ops[0], flag, 'collusion', 'Same rider, 14 trips')
        data = body_of(post(world.ops[0], flag_url(flag, 'handoff/'),
                            {'kind': 'driver_suspend'}, expect=200))
        assert data['handoffs_taken'][0]['kind'] == 'driver_suspend'
        assert data['timeline'][-1]['action'] == 'handoff'
        assert not DriverSuspension.objects.filter(driver=driver).exists()
        driver.user_id.refresh_from_db()
        assert driver.user_id.is_active
        assert AdminAuditLog.objects.filter(action='fraud_flag_handoff').count() == 1

    def test_a_handoff_the_caller_cannot_take_or_that_does_not_fit_is_refused(self, world):
        driver = hyd_driver(world, 'H3')
        flag = make_flag(driver.user_id, world.geo['IN-TG-HYD'], kind='collusion')
        confirm(world.ops[0], flag, 'collusion', 'Same rider, 14 trips')
        assert post(world.ops[0], flag_url(flag, 'handoff/'),
                    {'kind': 'driver_deactivate'}).status_code == 403
        assert post(world.ops[0], flag_url(flag, 'handoff/'),
                    {'kind': 'rider_block'}).status_code == 400
        assert post(world.ops[0], flag_url(flag, 'handoff/'),
                    {'kind': 'nonsense'}).status_code == 400


class TestTheListAndSorting:
    def test_sorted_by_severity_then_age_and_filterable(self, world):
        node = world.geo['IN-TG-HYD']
        old_low = make_flag(make_rider('S1', '91'), node, severity='low', age_hours=100)
        new_high = make_flag(make_rider('S2', '91'), node, severity='high', age_hours=1)
        old_high = make_flag(make_rider('S3', '91'), node, severity='high', age_hours=60)
        mid = make_flag(make_rider('S4', '91'), node, severity='medium', kind='payment_abuse')
        done = make_flag(make_rider('S5', '91'), node, severity='high', status='dismissed')
        data = get(world.ops[0], f'{ADM}/flags/')
        assert [f['id'] for f in data['results']] == [old_high.pk, new_high.pk, mid.pk,
                                                       old_low.pk, done.pk]
        assert data['facets']['severity'] == {'high': 2, 'medium': 1, 'low': 1}
        only = get(world.ops[0], f'{ADM}/flags/?kind=payment_abuse')['results']
        assert [f['id'] for f in only] == [mid.pk]
        high = get(world.ops[0], f'{ADM}/flags/?severity=high&status=active')['results']
        assert [f['id'] for f in high] == [old_high.pk, new_high.pk]
        aged = get(world.ops[0], f'{ADM}/flags/?older_than_hours=48')['results']
        assert {f['id'] for f in aged} == {old_low.pk, old_high.pk}
        post(world.ops[0], flag_url(mid, 'assign/'), expect=200)
        mine = get(world.ops[0], f'{ADM}/flags/?assigned=me')['results']
        assert [f['id'] for f in mine] == [mid.pk]
        assert [f['id'] for f in get(world.ops[0], f'{ADM}/flags/?assigned=unassigned')[
            'results']][:1] == [old_high.pk]
        assert world.ops[0].get(f'{ADM}/flags/?severity=urgent').status_code == 400
        assert world.ops[0].get(f'{ADM}/flags/?kind=nope').status_code == 400

    def test_module_12_referral_flags_are_still_listed(self, world):
        rider = make_rider('M12', '91')
        old = RiskFlag.objects.create(subject=rider, kind='referral_abuse',
                                      anchor_node=world.geo['IN-TG-HYD'], source_ref='referral:7',
                                      evidence={'referral_id': 7, 'signals': ['same_phone']})
        rows = get(world.mkt[0], f'{ADM}/flags/?kind=referral_abuse')['results']
        assert [r['id'] for r in rows] == [old.pk]
        assert rows[0]['evidence']['signals'] == ['same_phone']
        assert rows[0]['severity'] == 'medium' and rows[0]['status'] == 'open'
        assert get(world.mkt[0], flag_url(old))['linked']['accounts'] == []


class TestTheSubjectIsNeverTold:
    def test_no_notification_of_any_kind_is_sent_by_the_review_actions(self, world):
        from servers.notifications.models import NotificationMessage
        from servers.rider.models import Notification

        rider = make_rider('N1', '91')
        flag = make_flag(rider, world.geo['IN-TG-HYD'], kind='multi_accounting')
        with mock.patch('servers.notifications.dispatcher.notify') as notify:
            post(world.ops[0], flag_url(flag, 'assign/'), expect=200)
            post(world.ops[0], flag_url(flag, 'note/'), {'note': 'looks like one person'},
                 expect=200)
            confirm(world.ops[0], flag, 'multi_accounting', 'Four accounts, one handset')
            post(world.ops[0], flag_url(flag, 'reopen/'),
                 {'reason_code': 'appeal', 'note': 'The rider complained'}, expect=200)
            dismiss(world.ops[0], flag)
        assert not notify.called
        assert NotificationMessage.objects.count() == 0
        assert Notification.objects.filter(user_id=rider).count() == 0

    def test_review_actions_do_not_touch_any_money_table(self, world):
        from servers.promotions.models import LedgerEntry
        from servers.rider.models import WalletTransaction
        from servers.ride.models import TripMoneyAction

        rider = make_rider('N2', '91')
        flag = make_flag(rider, world.geo['IN-TG-HYD'], kind='referral_abuse')
        counts = lambda: (WalletTransaction.objects.count(), LedgerEntry.objects.count(),  # noqa: E731
                          TripMoneyAction.objects.count())
        before = counts()
        confirm(world.ops[0], flag, 'promo_abuse', 'Confirmed referral ring')
        post(world.ops[0], flag_url(flag, 'handoff/'), {'kind': 'rider_flag'}, expect=200)
        assert counts() == before
