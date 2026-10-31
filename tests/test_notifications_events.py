"""Module 11: the two system events wired now, the seed, and the one-off command.

  * commission.increase_notice drains pricing.DriverNotice: in_app + push + sms + email,
    transactional, English; the body states the scope, old % -> new % and the effective date in
    the DRIVER'S time zone.
  * payout.missing_details drains payments.DriverPayoutNotice the same way.
  * seed_notification_templates is create-if-missing and never modifies a template; the India SMS
    versions are DRAFT because an India SMS cannot be active without DLT ids (the rule working).
  * send_queued_notices is idempotent, logs counts and respects the stub and the allowlist.
"""

from datetime import datetime, timezone as dt_utc
from decimal import Decimal
from itertools import count
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from conftest import seed_actor
from notifications_support import (  # noqa: F401
    block_the_network, geo, make_driver_user, make_user,
)
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.notifications import events
from servers.notifications.models import (
    ACTIVE, DRAFT, NotificationMessage, NotificationTemplate, TemplateRegistration,
)
from servers.payments.models import DriverPayoutNotice, PayoutRun
from servers.pricing.commission_models import CommissionRule, DriverNotice

pytestmark = [pytest.mark.django_db]


@pytest.fixture(autouse=True)
def no_celery(monkeypatch):
    import servers.notifications.tasks as tasks

    monkeypatch.setattr(tasks.send_message, 'delay', lambda *a, **k: None)
    monkeypatch.setattr(tasks.send_message, 'apply_async', lambda *a, **k: None)
    monkeypatch.setattr(tasks.dispatch_queued, 'delay', lambda *a, **k: None)


_emails = count(1)


def seed(actor=None):
    out = StringIO()
    call_command('seed_notification_templates', actor_email=(actor or seed_actor()).email,
                 stdout=out)
    return out.getvalue()


def notice_for(geo, *, country='IN', node='IN', vehicle_type=None, old='15.00', new='18.00',
               effective=None):
    rule = CommissionRule.objects.create(
        country=country, geo_node=geo[node], vehicle_type=vehicle_type, percent=Decimal(new),
        status='active', version=1, effective_from=datetime(2026, 1, 1, tzinfo=dt_utc.utc),
        reason='test')
    return DriverNotice.objects.create(
        rule=rule, country=country, geo_node=geo[node], vehicle_type=vehicle_type,
        old_percent=Decimal(old) if old is not None else None, new_percent=Decimal(new),
        effective_from=effective or datetime(2026, 11, 12, 20, 30, tzinfo=dt_utc.utc))


def driver(geo, node='IN-TG-HYD', phone=None, email=None, vehicle='sedan', approved=True):
    user = make_driver_user(geo, node, phone, email or f'drv{next(_emails)}@example.test',
                            approved=approved)
    kind, _ = VehicleType.objects.get_or_create(type=vehicle)
    car = Vehicle.objects.create(driver_id=user.driver, vehicle_type_id=kind,
                                 vehicle_number=f'TS09{user.pk:05d}')
    user.driver.active_vehicle = car
    user.driver.save(update_fields=['active_vehicle'])
    return user


# ===========================================================================
# THE SEED
# ===========================================================================

class TestSeed:
    def test_creates_the_english_templates_active_except_the_india_sms_drafts(self, geo):
        seed()
        rows = NotificationTemplate.objects.all()
        assert rows.count() == 28       # 2 keys x 2 countries x 4 channels + 3 keys x 2 countries x 2
        assert rows.filter(status=ACTIVE).count() == 26
        drafts = rows.filter(status=DRAFT)
        assert {(t.channel, t.country) for t in drafts} == {('sms', 'IN')}
        assert drafts.count() == 2
        assert all(t.category == 'transactional' and t.locale == 'en' for t in rows)
        assert {t.key for t in rows} == {
            'commission.increase_notice', 'payout.missing_details', 'trips.operator_cancelled',
            'trips.operator_ended_rider', 'trips.operator_ended_driver'}

    def test_the_india_sms_drafts_carry_the_text_to_register_and_no_ids(self, geo):
        seed()
        draft = NotificationTemplate.objects.get(
            key='commission.increase_notice', channel='sms', country='IN')
        reg = draft.registrations.get(provider='msg91')
        assert reg.registered_text.count('{#var#}') == 4
        assert reg.dlt_template_id == '' and reg.sender_id == '' and reg.provider_template_id == ''
        from servers.notifications.template_workflow import activation_problems
        problems = activation_problems(draft)
        assert [p['code'] for p in problems] == ['DLT_REGISTRATION_REQUIRED']
        assert 'DLT template ID and sender ID' in problems[0]['message']

    def test_every_seeded_template_is_valid_and_the_us_sms_is_active(self, geo):
        seed()
        from servers.notifications.template_workflow import activation_problems
        for template in NotificationTemplate.objects.exclude(channel='sms', country='IN'):
            assert activation_problems(template) == [], template
        assert NotificationTemplate.objects.get(
            key='payout.missing_details', channel='sms', country='US').status == ACTIVE

    def test_it_never_modifies_an_existing_template_and_a_second_run_creates_nothing(self, geo):
        seed()
        template = NotificationTemplate.objects.get(
            key='commission.increase_notice', channel='push', country='IN')
        NotificationTemplate.objects.filter(pk=template.pk).update(body='Edited by a person')
        assert 'already present' in seed()
        template.refresh_from_db()
        assert template.body == 'Edited by a person' and NotificationTemplate.objects.count() == 28

    def test_a_template_in_any_status_blocks_the_seed_for_that_slot(self, geo):
        seed()
        NotificationTemplate.objects.filter(
            key='commission.increase_notice', channel='email', country='US').update(status='archived')
        seed()
        assert NotificationTemplate.objects.filter(
            key='commission.increase_notice', channel='email', country='US').count() == 1

    def test_it_is_attributed_and_audited(self, geo):
        seed()
        row = AdminAuditLog.objects.get(action='notification_templates_seeded')
        assert row.actor_id == seed_actor().pk
        assert row.actor_label.endswith('via seed_notification_templates')
        assert len(row.after['created']) == 28

    def test_it_needs_an_actor(self, geo):
        with pytest.raises(CommandError, match='--actor-email'):
            call_command('seed_notification_templates', stdout=StringIO())
        assert not NotificationTemplate.objects.exists()

    def test_without_the_country_nodes_it_says_so_and_creates_nothing(self, db):
        seed_actor()
        out = seed()
        assert 'run seed_admin_access first' in out and not NotificationTemplate.objects.exists()


# ===========================================================================
# DRAINING DriverNotice
# ===========================================================================

class TestCommissionNotice:
    def test_every_approved_driver_in_scope_gets_four_channels(self, geo):
        seed()
        a, b = driver(geo, phone='+919700000101'), driver(geo, phone='+919700000102')
        notice = notice_for(geo)
        counts = events.queue_driver_notice(notice)
        assert counts == {'drivers': 2, 'messages': 6, 'no_template': 0}  # IN sms is a draft
        rows = NotificationMessage.objects.filter(source='commission.increase_notice')
        assert {(m.recipient_user_id, m.channel) for m in rows} == {
            (u.pk, c) for u in (a, b) for c in ('in_app', 'push', 'email')}
        notice.refresh_from_db()
        assert notice.status == 'sent' and notice.sent_at is not None

    def test_the_us_driver_gets_sms_too_because_us_sms_is_active(self, geo):
        seed()
        us = driver(geo, 'US-TX-AUS', phone='+15125550101')
        notice = notice_for(geo, country='US', node='US', old='15.00', new='16.00')
        events.queue_driver_notice(notice)
        channels = set(NotificationMessage.objects.filter(recipient_user_id=us.pk)
                       .values_list('channel', flat=True))
        assert channels == {'in_app', 'push', 'sms', 'email'}

    def test_the_body_states_scope_rates_and_the_date_in_the_drivers_timezone(self, geo):
        seed()
        hyd = driver(geo, phone='+919700000103')
        notice = notice_for(geo, node='IN-TG-HYD',
                            effective=datetime(2026, 11, 12, 20, 30, tzinfo=dt_utc.utc))
        events.queue_driver_notice(notice)
        body = NotificationMessage.objects.get(recipient_user_id=hyd.pk, channel='in_app').body
        # 20:30 UTC on the 12th is 02:00 on the 13th in Hyderabad.
        assert 'From 13 November 2026' in body
        assert 'all vehicle classes in Hyderabad' in body
        assert 'from 15.00% to 18.00%' in body

    def test_the_same_instant_reads_as_the_previous_day_in_a_us_zone(self, geo):
        seed()
        us = driver(geo, 'US-TX-AUS', phone='+15125550102')
        from servers.pricing.models import ServiceZone
        ServiceZone.objects.create(
            code='US-TX-AUS', name='Austin', zone_type='city', city='Austin', country='US',
            polygon_geojson={'type': 'Polygon', 'coordinates': [[[-98, 30], [-97, 30], [-97, 31],
                                                                  [-98, 31], [-98, 30]]]},
            priority=10, is_active=True, geo_node=geo['US-TX-AUS'], currency='USD',
            timezone_name='America/Chicago')
        notice = notice_for(geo, country='US', node='US-TX-AUS', old=None, new='16.00',
                            effective=datetime(2026, 11, 13, 3, 30, tzinfo=dt_utc.utc))
        events.queue_driver_notice(notice)
        body = NotificationMessage.objects.get(recipient_user_id=us.pk, channel='in_app').body
        assert 'From 12 November 2026' in body            # 21:30 on the 12th in Austin
        assert 'changes from the current rate to 16.00%' in body

    def test_a_vehicle_class_scopes_the_audience_and_the_words(self, geo):
        seed()
        sedan = driver(geo, phone='+919700000104', vehicle='sedan')
        driver(geo, phone='+919700000105', vehicle='auto')
        kind = VehicleType.objects.get(type='sedan')
        events.queue_driver_notice(notice_for(geo, vehicle_type=kind))
        assert set(NotificationMessage.objects.values_list('recipient_user_id', flat=True)) == {sedan.pk}
        assert 'sedan in India' in NotificationMessage.objects.filter(channel='in_app').first().body

    def test_drivers_outside_the_scope_or_unapproved_are_not_told(self, geo):
        seed()
        driver(geo, 'US-TX-AUS', phone='+15125550103')
        driver(geo, phone='+919700000106', approved=False)
        counts = events.queue_driver_notice(notice_for(geo))
        assert counts['drivers'] == 0 and not NotificationMessage.objects.exists()

    def test_running_it_twice_sends_nothing_twice(self, geo):
        seed()
        driver(geo, phone='+919700000107')
        notice = notice_for(geo)
        events.queue_driver_notice(notice)
        before = NotificationMessage.objects.count()
        events.queue_driver_notice(notice)
        assert NotificationMessage.objects.count() == before

    def test_a_notice_stays_queued_when_there_is_no_active_template(self, geo):
        driver(geo, phone='+919700000108')          # templates NOT seeded
        notice = notice_for(geo)
        counts = events.queue_driver_notice(notice)
        assert counts['messages'] == 0 and counts['no_template'] == 1
        notice.refresh_from_db()
        assert notice.status == 'queued'

    def test_deliver_hands_the_notice_to_the_outbox_and_sends_nothing_inline(self, geo):
        from servers.pricing.driver_notices import deliver
        seed()
        driver(geo, phone='+919700000109')
        notice = notice_for(geo)
        assert deliver(notice) is False
        assert not NotificationMessage.objects.exists()
        notice.refresh_from_db()
        assert notice.status == 'queued'

    def test_the_messages_go_to_the_stub_by_default(self, geo):
        from servers.notifications import dispatcher
        seed()
        user = driver(geo, phone='+919700000110')
        events.queue_driver_notice(notice_for(geo))
        for message in NotificationMessage.objects.filter(recipient_user_id=user.pk):
            if message.channel != 'push':          # no device token on the fixture driver
                dispatcher.deliver(message.pk)
        email = NotificationMessage.objects.get(recipient_user_id=user.pk, channel='email')
        assert email.provider == 'stub' and email.status == 'sent'
        inapp = NotificationMessage.objects.get(recipient_user_id=user.pk, channel='in_app')
        assert inapp.provider == 'in_app' and inapp.status == 'delivered'
        from servers.rider.models import Notification
        assert Notification.objects.filter(user_id=user).count() == 1


# ===========================================================================
# DRAINING DriverPayoutNotice
# ===========================================================================

class TestPayoutNotice:
    def make(self, geo, phone):
        user = driver(geo, phone=phone)
        run = PayoutRun.objects.create(country='IN', currency='INR',
                                       week_start=datetime(2026, 11, 2).date())
        return user, DriverPayoutNotice.objects.create(run=run, driver=user.driver)

    def test_a_skipped_driver_is_told_on_every_channel_and_the_notice_is_sent(self, geo):
        seed()
        user, notice = self.make(geo, '+919700000111')
        counts = events.queue_payout_notice(notice)
        assert counts['messages'] == 3                       # IN sms is a draft
        notice.refresh_from_db()
        assert notice.status == 'sent' and notice.sent_at is not None
        body = NotificationMessage.objects.get(recipient_user_id=user.pk, channel='in_app').body
        assert 'payout details' in body

    def test_the_payout_run_hook_does_the_same(self, geo):
        from servers.payments.weekly_payout import deliver_payout_notice
        seed()
        user, notice = self.make(geo, '+919700000112')
        assert deliver_payout_notice(notice) is True
        assert NotificationMessage.objects.filter(recipient_user_id=user.pk).count() == 3

    def test_it_is_idempotent_and_failure_in_the_hook_never_raises(self, geo, monkeypatch):
        from servers.payments.weekly_payout import deliver_payout_notice
        seed()
        _, notice = self.make(geo, '+919700000113')
        events.queue_payout_notice(notice)
        events.queue_payout_notice(notice)
        assert NotificationMessage.objects.count() == 3
        monkeypatch.setattr(events, 'queue_payout_notice', lambda n: 1 / 0)
        assert deliver_payout_notice(notice) is False


# ===========================================================================
# send_queued_notices
# ===========================================================================

class TestSendQueuedNotices:
    def run(self, *args, actor=True):
        out = StringIO()
        kwargs = {'actor_email': seed_actor().email} if actor else {}
        call_command('send_queued_notices', *args, stdout=out, **kwargs)
        return out.getvalue()

    def world(self, geo):
        seed()
        user = driver(geo, phone='+919700000114', email='d114@example.test')
        notice = notice_for(geo)
        return user, notice

    def test_a_dry_run_counts_and_writes_nothing_and_needs_no_actor(self, geo):
        self.world(geo)
        out = self.run('--dry-run', actor=False)
        assert '1 commission notice(s)' in out
        assert not NotificationMessage.objects.exists()
        assert not AdminAuditLog.objects.filter(action='notification_queued_notices_sent').exists()

    def test_it_refuses_without_an_actor_and_writes_nothing(self, geo):
        self.world(geo)
        with pytest.raises(CommandError, match='--actor-email'):
            self.run(actor=False)
        assert not NotificationMessage.objects.exists()

    def test_it_queues_the_backlog_logs_counts_and_audits(self, geo):
        user, notice = self.world(geo)
        out = self.run()
        assert 'commission notices: 1 handled, 3 message(s) queued' in out
        notice.refresh_from_db()
        assert notice.status == 'sent'
        row = AdminAuditLog.objects.get(action='notification_queued_notices_sent')
        assert row.after['commission_messages'] == 3
        assert row.actor_label.endswith('via send_queued_notices')

    def test_it_is_idempotent(self, geo):
        self.world(geo)
        self.run()
        count = NotificationMessage.objects.count()
        out = self.run()
        assert NotificationMessage.objects.count() == count
        assert 'commission notices: 0 handled' in out

    def test_it_respects_the_stub_and_the_allowlist(self, geo, settings):
        from servers.notifications import dispatcher
        settings.NOTIFY_LIVE_SEND = True
        settings.IS_PRODUCTION = False
        settings.NOTIFY_TEST_RECIPIENTS = ['someone.else@example.test']
        user, _ = self.world(geo)
        self.run()
        email = NotificationMessage.objects.get(recipient_user_id=user.pk, channel='email')
        assert email.status == 'suppressed' and email.suppression_reason == 'not on test allowlist'
        assert dispatcher.deliver(email.pk) == 'skipped'

    def test_the_seed_plus_the_command_leave_the_india_sms_unsent_because_it_is_a_draft(self, geo):
        user, _ = self.world(geo)
        self.run()
        assert not NotificationMessage.objects.filter(
            recipient_user_id=user.pk, channel='sms').exists()


def test_the_registration_row_is_unique_per_provider(geo):
    seed()
    template = NotificationTemplate.objects.get(
        key='commission.increase_notice', channel='sms', country='IN')
    from django.db import IntegrityError, transaction
    with pytest.raises(IntegrityError), transaction.atomic():
        TemplateRegistration.objects.create(template=template, provider='msg91')
