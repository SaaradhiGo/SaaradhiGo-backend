"""Audit log in plain words (owner, 24 Nov): no field names, money as money, a real place."""
import pytest

from servers.admin_audit import viewer
from servers.admin_audit.models import AdminAuditLog
from servers.admin_audit.words import summary


def _row(**kw):
    base = dict(actor_label='ops@saaradhigo.com', action='x', target_type='trip', target_id='1',
                before={}, after={}, reason='r')
    base.update(kw)
    return AdminAuditLog.objects.create(**base)


@pytest.mark.django_db
def test_trip_rounding_reads_as_rupees_in_india():
    e = _row(action='inr_trip_money_rounded', target_id='999999',
             before={'fare': {'final_fare_minor': 32150}, 'settlements': {}, 'net_shown': 32150},
             after={'fare': {'final_fare_minor': 32200}, 'settlements': {}, 'net_whole': 32200,
                    'commission_whole': 0, 'correction_minor': 50})
    names = viewer.names_for([e])
    text = summary(e, names)
    assert text.startswith('Rounded the fare to whole rupees')
    assert '₹321.50 to ₹322' in text and '32200' not in text and 'net whole' not in text
    row = viewer.entry_row(e, names)
    assert row['place'] == 'IN'


@pytest.mark.django_db
def test_run_summary_names_counts_not_fields():
    e = _row(action='inr_money_rounded_to_rupees', target_id='all',
             before={'trips': 85, 'fee_days': 0}, after={'wallets_moved': 1})
    names = viewer.names_for([e])
    assert summary(e, names) == 'Rounded India money to whole rupees: 85 trips, 1 driver balance corrected'
    assert viewer.entry_row(e, names)['target_label'] == 'All trips'


@pytest.mark.django_db
def test_a_new_field_reads_as_set_not_from_nothing():
    e = _row(action='thing_changed', target_type='geonode', target_id='IN',
             before={'a': 1}, after={'a': 1, 'minimum_withdrawal_minor': 20000, 'country': 'IN'})
    assert 'Set' in summary(e, viewer.names_for([e])) and '₹200' in summary(e, viewer.names_for([e]))


@pytest.mark.django_db
def test_theme_reset_reads_as_a_sentence():
    e = _row(action='themes_reset_to_light', target_type='user_preference', target_id='all',
             before={'not_light': 3}, after={'reset': 3})
    assert summary(e, viewer.names_for([e])) == 'Reset 3 accounts to the light theme'
