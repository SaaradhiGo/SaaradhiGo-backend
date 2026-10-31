"""Module 11: the dispatcher, the safety flag, preferences, quiet hours, retries.

WHAT THESE PROVE
----------------
  * THE STUB IS THE DEFAULT. With NOTIFY_LIVE_SEND off every external channel is recorded by the
    stub and no live adapter is ever constructed, however many providers are "configured".
  * THE LIVE FLAG IS NOT ENOUGH OUTSIDE PRODUCTION. With it on, only recipients on
    NOTIFY_TEST_RECIPIENTS are sent to; everyone else is `suppressed: not on test allowlist`.
  * PREFERENCES: transactional defaults on (push, in_app, sms, email), promotional off,
    WhatsApp off until an explicit opt-in, in-app transactional always on; a STOP opts the number
    out of ALL SMS, transactional included.
  * QUIET HOURS hold PROMOTIONAL messages only, in the recipient's own time.
  * IDEMPOTENCY: the same key writes one row per channel, once.
  * RETRIES back off, stop at the limit and dead-letter as `failed`; a permanent error fails at once.
"""

from datetime import datetime, timedelta, timezone as dt_utc
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from notifications_support import (  # noqa: F401  (fixtures)
    block_the_network, fake_credentials, geo, make_driver_user, make_rider_user, make_template,
    sms_world,
)
from servers.notifications import config, dispatcher, preferences, quiet, templating
from servers.notifications.models import (
    DELIVERED, FAILED, QUEUED, SENT, SUPPRESSED, NotificationMessage, NotificationStatusEvent,
)
from servers.notifications.providers import build
from servers.notifications.providers.base import (
    LiveSendDisabled, PermanentProviderError, SendResult, TransientProviderError,
)

pytestmark = [pytest.mark.django_db]


@pytest.fixture(autouse=True)
def no_celery(monkeypatch):
    """Scheduling is exercised separately; here a send is `deliver(id)` called by the test."""
    import servers.notifications.tasks as tasks

    monkeypatch.setattr(tasks.send_message, 'delay', lambda *a, **k: None)
    monkeypatch.setattr(tasks.send_message, 'apply_async', lambda *a, **k: None)


def notify(world, **kw):
    kw.setdefault('idempotency_key', 'k1')
    return dispatcher.notify('test.event', world.user, {'thing': 'world'}, **kw)


# ===========================================================================
# THE STUB IS THE DEFAULT
# ===========================================================================

class TestTheStubIsTheDefault:
    def test_the_flag_defaults_to_off(self, settings):
        from base import settings as base_settings
        assert base_settings.NOTIFY_LIVE_SEND is False
        assert config.live_send_enabled() is False

    def test_every_external_channel_resolves_to_the_stub_when_the_flag_is_off(self, settings):
        settings.NOTIFY_LIVE_SEND = False
        for channel, table in settings.NOTIFY_PROVIDERS.items():
            for country in table:
                provider, live = config.effective_provider(channel, country)
                if channel == 'in_app':
                    assert (provider, live) == ('in_app', False)
                else:
                    assert (provider, live) == ('stub', False), (channel, country)

    def test_a_send_with_the_flag_off_is_recorded_by_the_stub_and_sends_nothing(
            self, geo, settings, monkeypatch):
        settings.NOTIFY_LIVE_SEND = False
        world = sms_world(geo)
        # Make every live adapter explode if constructed or used.
        from servers.notifications.providers import _LIVE, live_class
        for (name, channel) in _LIVE:
            monkeypatch.setattr(live_class(name, channel), '__init__',
                                mock.Mock(side_effect=AssertionError('live adapter constructed')))

        rows = notify(world)
        sms = next(m for m in rows if m.channel == 'sms')
        assert dispatcher.deliver(sms.pk) == SENT
        sms.refresh_from_db()
        assert sms.provider == 'stub' and sms.provider_message_id.startswith('stub-')
        assert sms.status == SENT

    def test_a_live_adapter_is_never_constructed_when_the_flag_is_off(self, settings):
        settings.NOTIFY_LIVE_SEND = False
        for name, channel in (('msg91', 'sms'), ('msg91', 'whatsapp'), ('twilio', 'sms'),
                              ('ses', 'email'), ('fcm', 'push')):
            with pytest.raises(LiveSendDisabled):
                build(name, channel)
        assert build('stub', 'sms').name == 'stub'

    def test_the_message_row_carries_a_masked_recipient_and_no_raw_contact(self, geo):
        world = sms_world(geo)
        rows = notify(world)
        sms = next(m for m in rows if m.channel == 'sms')
        assert sms.recipient_masked == '+91••••••0001'
        assert '9700000001' not in sms.recipient_masked
        assert sms.city.code == 'IN-TG-HYD' and sms.country == 'IN'


# ===========================================================================
# THE LIVE FLAG + THE ALLOWLIST
# ===========================================================================

class TestTheAllowlist:
    def live(self, settings, recipients=()):
        settings.NOTIFY_LIVE_SEND = True
        settings.IS_PRODUCTION = False
        settings.ENVIRONMENT = 'qa'
        settings.NOTIFY_TEST_RECIPIENTS = list(recipients)

    def test_a_non_allowlisted_recipient_is_suppressed_when_the_flag_is_on_outside_production(
            self, geo, settings):
        self.live(settings, ['+919999999999'])
        world = sms_world(geo)
        rows = notify(world)
        sms = next(m for m in rows if m.channel == 'sms')
        assert sms.status == SUPPRESSED
        assert sms.suppression_reason == 'not on test allowlist'
        assert not NotificationMessage.objects.filter(
            pk=sms.pk).exclude(provider_message_id='').exists()
        # In-app is internal: it is not an external send, so it is not gated.
        assert next(m for m in rows if m.channel == 'in_app').status == QUEUED

    def test_an_allowlisted_phone_is_sent_through_the_configured_provider(
            self, geo, settings, fake_credentials):
        self.live(settings, ['+919700000001'])
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        assert sms.status == QUEUED and sms.provider == 'msg91'
        with mock.patch('servers.notifications.providers.msg91.Msg91Sms.send',
                        return_value=SendResult('req-1', 'sent')) as send:
            assert dispatcher.deliver(sms.pk) == SENT
        send.assert_called_once()
        sms.refresh_from_db()
        assert sms.provider_message_id == 'req-1'

    def test_an_allowlisted_email_matches_case_insensitively(self, geo, settings):
        self.live(settings, ['Driver@Example.Test'])
        user = make_driver_user(geo, phone='+919700000002', email='driver@example.test')
        make_template(geo, channel='email', subject='Hi', body='Email {thing}')
        row = dispatcher.notify('test.event', user, {'thing': 'x'}, idempotency_key='e1')[0]
        assert row.status == QUEUED

    def test_the_allowlist_does_not_apply_in_production(self, geo, settings):
        settings.NOTIFY_LIVE_SEND = True
        settings.IS_PRODUCTION = True
        settings.NOTIFY_TEST_RECIPIENTS = []
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        assert sms.status == QUEUED and sms.provider == 'msg91'

    def test_the_flag_is_rechecked_at_send_time(self, geo, settings):
        self.live(settings, ['+919700000001'])
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        settings.NOTIFY_LIVE_SEND = False          # switched off between queue and send
        assert dispatcher.deliver(sms.pk) == SENT
        sms.refresh_from_db()
        assert sms.provider == 'stub'

    def test_the_allowlist_is_read_from_the_environment_setting_never_the_repo(self):
        from base import settings as base_settings
        assert base_settings.NOTIFY_TEST_RECIPIENTS == []


# ===========================================================================
# PREFERENCES
# ===========================================================================

class TestPreferences:
    def test_the_defaults(self):
        assert preferences.default_for('sms', 'transactional')
        assert preferences.default_for('email', 'transactional')
        assert preferences.default_for('push', 'transactional')
        assert preferences.default_for('in_app', 'transactional')
        assert not preferences.default_for('whatsapp', 'transactional')
        for channel in ('sms', 'email', 'push', 'whatsapp', 'in_app'):
            assert not preferences.default_for(channel, 'promotional')

    def test_promotional_is_off_until_opted_in(self, geo):
        user = make_driver_user(geo, phone='+919700000003')
        make_template(geo, key='promo.x', channel='push', category='promotional',
                      title='T', body='Offer {thing}')
        row = dispatcher.notify('promo.x', user, {'thing': 'a'}, idempotency_key='p1')[0]
        assert row.status == SUPPRESSED and 'not opted in' in row.suppression_reason

    def test_whatsapp_needs_an_explicit_opt_in(self, geo):
        user = make_driver_user(geo, phone='+919700000004')
        make_template(geo, channel='whatsapp', body='WA {thing}')
        first = dispatcher.notify('test.event', user, {'thing': 'a'}, idempotency_key='w1')[0]
        assert first.status == SUPPRESSED and first.suppression_reason == 'no explicit WhatsApp opt-in'
        preferences.set_preference(user, 'whatsapp', 'transactional', True, 'app')
        second = dispatcher.notify('test.event', user, {'thing': 'a'}, idempotency_key='w2')[0]
        assert second.status == QUEUED

    def test_in_app_transactional_cannot_be_switched_off(self, geo):
        user = make_driver_user(geo, phone='+919700000005')
        with pytest.raises(ValueError):
            preferences.set_preference(user, 'in_app', 'transactional', False, 'app')
        assert preferences.is_opted_in(user.pk, 'in_app', 'transactional') == (True, '')

    def test_a_user_who_opted_out_of_a_channel_gets_a_suppressed_row_with_the_reason(self, geo):
        world = sms_world(geo)
        preferences.set_preference(world.user, 'sms', 'transactional', False, 'app')
        sms = next(m for m in notify(world) if m.channel == 'sms')
        assert sms.status == SUPPRESSED and 'opted out of sms transactional' in sms.suppression_reason

    def test_stop_opts_the_number_out_of_all_sms_transactional_included(self, geo):
        world = sms_world(geo)
        changed = preferences.opt_out_channel(
            world.user, 'sms', 'webhook:twilio', 'notification_sms_stop_received', 'STOP')
        assert changed
        for category in ('transactional', 'promotional'):
            assert preferences.is_opted_in(world.user.pk, 'sms', category)[0] is False
        sms = next(m for m in notify(world) if m.channel == 'sms')
        assert sms.status == SUPPRESSED
        # ...and nothing else is switched off.
        assert preferences.is_opted_in(world.user.pk, 'push', 'transactional')[0] is True

    def test_a_missing_contact_is_a_suppression_not_a_crash(self, geo):
        user = make_driver_user(geo, phone='+919700000006')       # no email
        make_template(geo, channel='email', subject='Hi', body='Email {thing}')
        row = dispatcher.notify('test.event', user, {'thing': 'x'}, idempotency_key='m1')[0]
        assert row.status == SUPPRESSED and row.suppression_reason == 'no email on file'


# ===========================================================================
# QUIET HOURS (promotional only)
# ===========================================================================

class TestQuietHours:
    IST = ZoneInfo('Asia/Kolkata')

    def at(self, hour, minute=0):
        return datetime(2026, 11, 3, hour, minute, tzinfo=self.IST).astimezone(dt_utc.utc)

    def test_the_window_wraps_midnight(self):
        window = (21, 10)
        for hour in (21, 23, 0, 5, 9):
            assert quiet.in_quiet_hours(hour, window)
        for hour in (10, 12, 20):
            assert not quiet.in_quiet_hours(hour, window)

    def test_release_time_is_the_end_of_the_window_in_the_recipients_zone(self):
        release = quiet.release_time(self.at(22), self.IST, 'IN')
        assert release.astimezone(self.IST).hour == 10
        assert release.astimezone(self.IST).day == 4          # tomorrow morning
        early = quiet.release_time(self.at(3), self.IST, 'IN')
        assert early.astimezone(self.IST).day == 3            # this morning
        assert quiet.release_time(self.at(14), self.IST, 'IN') is None

    def promo_world(self, geo):
        user = make_driver_user(geo, phone='+919700000007')
        make_template(geo, key='promo.y', channel='push', category='promotional',
                      title='T', body='Offer {thing}')
        preferences.set_preference(user, 'push', 'promotional', True, 'app')
        user.fcm_token = 'tok'
        user.save(update_fields=['fcm_token'])
        return user

    def test_a_promotional_message_in_quiet_hours_is_held_not_dropped(self, geo):
        user = self.promo_world(geo)
        row = dispatcher.notify('promo.y', user, {'thing': 'a'}, idempotency_key='q1',
                                now=self.at(22))[0]
        assert row.status == QUEUED and row.not_before is not None
        assert row.not_before.astimezone(self.IST).hour == 10
        # Held: a send before the release time does nothing.
        assert dispatcher.deliver(row.pk, now=self.at(23)) == 'skipped'
        assert dispatcher.deliver(row.pk, now=row.not_before) == SENT

    def test_a_promotional_message_in_the_day_goes_at_once(self, geo):
        user = self.promo_world(geo)
        row = dispatcher.notify('promo.y', user, {'thing': 'a'}, idempotency_key='q2',
                                now=self.at(14))[0]
        assert row.status == QUEUED and row.not_before is None

    def test_a_transactional_message_ignores_quiet_hours(self, geo):
        world = sms_world(geo)
        row = next(m for m in dispatcher.notify('test.event', world.user, {'thing': 'a'},
                                                idempotency_key='q3', now=self.at(23))
                   if m.channel == 'sms')
        assert row.status == QUEUED and row.not_before is None

    def test_the_hours_are_per_country_config(self, settings):
        settings.NOTIFY_QUIET_HOURS = {'IN': (22, 8), 'US': (21, 10)}
        assert config.quiet_hours('IN') == (22, 8)
        assert quiet.release_time(self.at(21), self.IST, 'IN') is None
        assert quiet.release_time(self.at(22, 30), self.IST, 'IN') is not None


# ===========================================================================
# IDEMPOTENCY
# ===========================================================================

class TestIdempotency:
    def test_the_same_key_writes_one_row_per_channel_once(self, geo):
        world = sms_world(geo)
        first = notify(world, idempotency_key='same')
        second = notify(world, idempotency_key='same')
        assert sorted(m.pk for m in first) == sorted(m.pk for m in second)
        assert NotificationMessage.objects.count() == 2

    def test_a_different_key_is_a_different_message(self, geo):
        world = sms_world(geo)
        notify(world, idempotency_key='a')
        notify(world, idempotency_key='b')
        assert NotificationMessage.objects.count() == 4

    def test_a_key_is_required(self, geo):
        world = sms_world(geo)
        with pytest.raises(ValueError):
            dispatcher.notify('test.event', world.user, {'thing': 'x'}, idempotency_key='')

    def test_a_delivered_task_run_twice_sends_once(self, geo):
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        assert dispatcher.deliver(sms.pk) == SENT
        assert dispatcher.deliver(sms.pk) == 'skipped'
        assert NotificationStatusEvent.objects.filter(message=sms, status='sent').count() == 1


# ===========================================================================
# RETRIES AND DEAD LETTERS
# ===========================================================================

class TestRetries:
    def live_message(self, geo, settings, fake_credentials):
        settings.NOTIFY_LIVE_SEND = True
        settings.IS_PRODUCTION = True
        world = sms_world(geo)
        return next(m for m in notify(world) if m.channel == 'sms')

    def test_a_transient_failure_retries_with_backoff_then_dead_letters_at_five(
            self, geo, settings, fake_credentials):
        sms = self.live_message(geo, settings, fake_credentials)
        t = timezone.now()
        outcomes = []
        with mock.patch('servers.notifications.providers.msg91.Msg91Sms.send',
                        side_effect=TransientProviderError('HTTP 503')):
            for attempt in range(1, 6):
                sms.refresh_from_db()
                outcomes.append(dispatcher.deliver(sms.pk, now=(sms.not_before or t)))
        assert outcomes == ['retry', 'retry', 'retry', 'retry', 'dead_letter']
        sms.refresh_from_db()
        assert sms.status == FAILED and sms.attempts == 5
        assert sms.last_error.startswith('dead-lettered after 5 attempts')

    def test_backoff_grows(self, settings):
        assert [config.backoff_seconds(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
        assert config.backoff_seconds(30) == 3600

    def test_a_retry_that_succeeds_is_sent(self, geo, settings, fake_credentials):
        sms = self.live_message(geo, settings, fake_credentials)
        with mock.patch('servers.notifications.providers.msg91.Msg91Sms.send',
                        side_effect=[TransientProviderError('HTTP 429'), SendResult('r9', 'sent')]):
            assert dispatcher.deliver(sms.pk) == 'retry'
            sms.refresh_from_db()
            assert sms.status == QUEUED and sms.not_before > timezone.now()
            assert dispatcher.deliver(sms.pk, now=sms.not_before) == SENT
        sms.refresh_from_db()
        assert sms.attempts == 2 and sms.provider_message_id == 'r9'

    def test_a_permanent_failure_fails_at_once_and_masks_the_error(
            self, geo, settings, fake_credentials):
        sms = self.live_message(geo, settings, fake_credentials)
        with mock.patch('servers.notifications.providers.msg91.Msg91Sms.send',
                        side_effect=PermanentProviderError('bad number +919700000001 for a@b.co')):
            assert dispatcher.deliver(sms.pk) == FAILED
        sms.refresh_from_db()
        assert sms.status == FAILED and sms.attempts == 1
        assert '9700000001' not in sms.last_error and 'a@b.co' not in sms.last_error

    def test_an_unexpected_exception_is_retried_not_lost(self, geo, settings, fake_credentials):
        sms = self.live_message(geo, settings, fake_credentials)
        with mock.patch('servers.notifications.providers.msg91.Msg91Sms.send',
                        side_effect=RuntimeError('boom')):
            assert dispatcher.deliver(sms.pk) == 'retry'

    def test_the_rate_limit_defers_without_spending_an_attempt(self, geo, settings, monkeypatch):
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        monkeypatch.setattr(dispatcher, '_rate_ok', lambda provider: False)
        assert dispatcher.deliver(sms.pk) == 'rate_limited'
        sms.refresh_from_db()
        assert sms.status == QUEUED and sms.attempts == 0 and sms.not_before is not None

    def test_a_lost_worker_row_is_requeued_by_the_sweep(self, geo):
        from servers.notifications.tasks import requeue_stuck
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        NotificationMessage.objects.filter(pk=sms.pk).update(
            status='sending', attempts=1, updated_at=timezone.now() - timedelta(hours=1))
        assert requeue_stuck()['requeued'] == 1
        sms.refresh_from_db()
        assert sms.status == QUEUED


# ===========================================================================
# BODY HANDLING (PII)
# ===========================================================================

class TestBodies:
    def test_a_template_without_personal_variables_keeps_its_body(self, geo):
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        dispatcher.deliver(sms.pk)
        sms.refresh_from_db()
        assert sms.body == 'Hello world' and sms.body_hash

    def test_a_template_with_personal_variables_drops_its_body_once_final(self, geo):
        user = make_driver_user(geo, phone='+919700000008')
        make_template(geo, key='personal.x', channel='push', title='T', body='Hi {name}',
                      variables=('name',), personal=('name',))
        user.fcm_token = 'tok'
        user.save(update_fields=['fcm_token'])
        row = dispatcher.notify('personal.x', user, {'name': 'Asha'}, idempotency_key='b1')[0]
        assert row.body == 'Hi Asha' and row.variable_values == ['Asha']    # needed for the send
        dispatcher.deliver(row.pk)
        row.refresh_from_db()
        assert row.status == SENT and row.body == '' and row.variable_values == []
        assert len(row.body_hash) == 64

    def test_a_missing_variable_fails_the_row_visibly(self, geo):
        world = sms_world(geo)
        rows = dispatcher.notify('test.event', world.user, {}, idempotency_key='mv')
        assert {m.status for m in rows} == {FAILED}
        assert 'thing' in rows[0].last_error


# ===========================================================================
# PLACEHOLDER VALIDATION
# ===========================================================================

class TestPlaceholderValidation:
    def test_undeclared_unused_and_malformed_placeholders(self):
        assert templating.validate(body='Hi {name}', variables=['name']) == []
        assert any('undeclared' in p for p in templating.validate(body='Hi {name}', variables=[]))
        assert any('never used' in p for p in templating.validate(body='Hi', variables=['name']))
        assert any('stray brace' in p for p in templating.validate(body='Hi {name', variables=['name']))
        assert any('stray brace' in p for p in templating.validate(body='Hi {Name}', variables=[]))
        assert any('{#var#}' in p for p in templating.validate(body='Hi {#var#}', variables=[]))
        assert any('empty' in p for p in templating.validate(body='  ', variables=[]))
        assert any('Personal variables' in p for p in templating.validate(
            body='Hi', variables=[], personal_variables=['name']))

    def test_render_substitutes_and_refuses_a_missing_value(self):
        assert templating.render('A {x} B {y}', {'x': 1, 'y': 'two'}) == 'A 1 B two'
        with pytest.raises(templating.MissingVariable) as exc:
            templating.render('A {x} {z}', {'x': 1})
        assert exc.value.names == ['z']

    def test_values_are_length_capped(self):
        assert len(templating.render('{x}', {'x': 'a' * 1000})) == templating.MAX_VALUE_LENGTH

    def test_the_dlt_mapping_is_one_to_one_in_count_order_and_fixed_text(self):
        text = 'Pay {#var#} by {#var#}.'
        assert templating.dlt_mapping_problems('Pay {amount} by {date}.', text) == []
        count = templating.dlt_mapping_problems('Pay {amount}.', text)
        assert any('1 placeholder(s) but the DLT text has 2' in p for p in count)
        words = templating.dlt_mapping_problems('Please pay {amount} by {date}.', text)
        assert any('does not match the DLT-approved text' in p for p in words)
        assert templating.dlt_mapping_problems('x', '') == ['No DLT-approved text is recorded for this template.']
        # same slots, different fixed words
        assert templating.dlt_mapping_problems('Owe {amount} by {date}.', text)


# ===========================================================================
# THE ATTEMPT TO SEND IS AN EVENT
# ===========================================================================

class TestEvents:
    def test_the_log_has_a_status_timeline(self, geo):
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        dispatcher.deliver(sms.pk)
        statuses = list(sms.events.values_list('status', flat=True))
        assert statuses == ['queued', 'sending', 'sent']

    def test_events_are_append_only(self, geo):
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        event = sms.events.first()
        event.detail = 'x'
        with pytest.raises(ValueError):
            event.save()
        with pytest.raises(ValueError):
            event.delete()

    def test_delivered_can_follow_sent_and_failed_cannot_follow_delivered(self, geo):
        world = sms_world(geo)
        sms = next(m for m in notify(world) if m.channel == 'sms')
        dispatcher.deliver(sms.pk)
        sms.refresh_from_db()
        assert dispatcher.apply_status(sms, DELIVERED, provider='stub', provider_message_id='p1')
        sms.refresh_from_db()
        assert sms.status == DELIVERED
        assert dispatcher.apply_status(sms, FAILED, provider='stub', provider_message_id='p1')
        sms.refresh_from_db()
        assert sms.status == DELIVERED
        # idempotent per (provider id, status)
        assert dispatcher.apply_status(sms, DELIVERED, provider='stub',
                                       provider_message_id='p1') is False


# ===========================================================================
# THE SWEEP, AND STRUCTURAL GUARDS
# ===========================================================================

class TestSweepAndGuards:
    def test_dispatch_queued_is_the_backstop_for_a_lost_on_commit_task(self, geo, monkeypatch):
        import servers.notifications.tasks as tasks
        sent = []
        monkeypatch.setattr(tasks.send_message, 'delay', lambda mid: sent.append(mid))
        world = sms_world(geo)
        rows = notify(world)
        # Brand-new rows are left to their own task; once settled the sweep picks them up.
        assert tasks.dispatch_queued()['dispatched'] == 0
        NotificationMessage.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        summary = tasks.dispatch_queued()
        assert summary['dispatched'] == 2 and sorted(sent) == sorted(m.pk for m in rows)

    def test_a_held_row_is_not_dispatched_before_its_time(self, geo, monkeypatch):
        import servers.notifications.tasks as tasks
        sent = []
        monkeypatch.setattr(tasks.send_message, 'delay', lambda mid: sent.append(mid))
        world = sms_world(geo)
        row = next(m for m in notify(world) if m.channel == 'sms')
        NotificationMessage.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(minutes=5),
            not_before=timezone.now() + timedelta(hours=2))
        tasks.dispatch_queued()
        assert row.pk not in sent

    def test_the_sweep_and_the_health_task_are_registered_in_the_beat_schedule(self, settings):
        tasks = {entry['task'] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        assert {'notifications.dispatch_queued', 'notifications.check_provider_health'} <= tasks

    def test_only_the_provider_factory_names_the_live_adapters(self):
        """The dispatcher, the workflows and the views reach a provider through `build()`, which
        refuses a live adapter while NOTIFY_LIVE_SEND is off. Importing an adapter class
        directly anywhere else would be a way around that."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / 'servers' / 'notifications'
        live_modules = ('providers.msg91', 'providers.twilio', 'providers.ses', 'providers.fcm',
                        'Msg91Sms', 'Msg91WhatsApp', 'TwilioSms', 'SesEmail', 'FcmPush')
        offenders = []
        for path in root.rglob('*.py'):
            if path.parent.name == 'providers':
                continue
            text = path.read_text(encoding='utf-8')
            for name in live_modules:
                if name in text:
                    offenders.append((path.name, name))
        # `providers/__init__.py` is exempt (it is the factory); everything else must be clean.
        assert offenders == [], offenders

    def test_the_otp_login_path_is_untouched_and_does_not_use_the_adapters(self):
        import inspect

        from base import utils
        source = inspect.getsource(utils.send_otp_via_sns)
        assert 'notifications' not in source and 'get_sns_client' in source
