"""The daily report email (owner ruling 25 Nov): staff opt in; at 08:00 local each city's numbers for
yesterday go out as one email per person and country, through the outbox (sent once SES is live)."""
from datetime import date, datetime, timezone as dt_tz
from io import StringIO

import pytest
from django.core.management import call_command

from conftest import seed_actor
from servers.notifications.models import NotificationMessage
from servers.reports.daily_email import send_due
from servers.reports.models import DailyCityMetrics
from test_finance_actions import body_of, post
from test_pricing_module import geo, operator  # noqa: F401

pytestmark = pytest.mark.django_db
API = '/api/v1/reports/admin/daily-email/'
# 08:20 in Hyderabad on 25 Nov 2026 = 02:50 UTC.
HYD_0820 = datetime(2026, 11, 25, 2, 50, tzinfo=dt_tz.utc)


def test_opt_in_then_one_email_with_yesterdays_numbers(geo):
    call_command('seed_notification_templates', actor_email=seed_actor().email, stdout=StringIO())
    client, user = operator('super_admin', geo)
    user.phone_number = '+919800000555'
    user.save(update_fields=['phone_number'])
    r = post(client, API, {'subscribed': True}, expect=200)
    assert body_of(r)['subscribed'] is True and body_of(r)['send_hour'] == 8
    DailyCityMetrics.objects.create(city=geo['IN-TG-HYD'], date=date(2026, 11, 24), currency='INR',
                                    trips_completed=12, cancelled_by_rider=2, gmv_minor=240000)
    assert send_due(HYD_0820)['queued'] == 1
    assert send_due(HYD_0820)['queued'] == 1          # the same key: no second email
    msgs = NotificationMessage.objects.filter(template_key='reports.daily_summary', recipient_user_id=user.pk)
    assert msgs.count() == 1
    post(client, API, {'subscribed': False}, expect=200)
    assert send_due(HYD_0820)['queued'] == 0


def test_not_subscribed_no_email(geo):
    call_command('seed_notification_templates', actor_email=seed_actor().email, stdout=StringIO())
    operator('super_admin', geo)
    assert send_due(HYD_0820)['queued'] == 0
