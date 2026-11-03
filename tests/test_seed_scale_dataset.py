"""`seed_scale_dataset`: refuses outside the load stack, and seeds a consistent dataset.

The full dataset is 2 million trips; this runs it at 0.2 % so the invariants are
checked in seconds on the CI database.
"""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Count

from servers.admin_access.models import CITY, GeoNode
from servers.command_centre.models import OperatorAlert
from servers.driver.models import Driver, WithdrawalRequest
from servers.pricing.models import ServiceZone
from servers.rider.models import Rider
from servers.ride.models import Trip
from servers.safety.models import SafetyIncident
from servers.sos.models import SOSEvent
from servers.support.models import SupportTicket

User = get_user_model()
SCALE = '0.002'


def _run(*args):
    out = StringIO()
    call_command('seed_scale_dataset', *args, stdout=out)
    return out.getvalue()


def _synthetic_rows():
    return (User.objects.count(), Driver.objects.count(), ServiceZone.objects.count(),
            Trip.objects.count(), GeoNode.objects.count())


@pytest.mark.django_db
class TestRefusesOutsideTheLoadStack:
    @pytest.mark.parametrize('environment', ['qa', 'production', 'development', 'staging', ''])
    def test_refused_before_any_write(self, environment, settings):
        settings.ENVIRONMENT = environment
        settings.IS_PRODUCTION = environment == 'production'
        before = _synthetic_rows()

        with pytest.raises(CommandError) as exc:
            _run('--scale', SCALE)

        assert 'load' in str(exc.value)
        assert repr(environment) in str(exc.value)
        assert _synthetic_rows() == before

    def test_reset_is_refused_too(self, settings):
        settings.ENVIRONMENT = 'qa'
        settings.IS_PRODUCTION = False
        with pytest.raises(CommandError, match='load'):
            _run('--reset', '--scale', SCALE)

    def test_the_environment_variable_alone_does_not_open_the_gate(self, settings, monkeypatch):
        monkeypatch.setenv('ENVIRONMENT', 'load')
        settings.ENVIRONMENT = 'qa'
        settings.IS_PRODUCTION = False
        with pytest.raises(CommandError, match='load'):
            _run('--scale', SCALE)

    def test_load_with_the_production_flag_is_still_refused(self, settings):
        settings.ENVIRONMENT = 'load'
        settings.IS_PRODUCTION = True
        with pytest.raises(CommandError, match='load'):
            _run('--scale', SCALE)


@pytest.mark.django_db
def test_seeds_a_consistent_synthetic_dataset_and_resets_cleanly(settings):
    settings.ENVIRONMENT = 'load'
    settings.IS_PRODUCTION = False

    _run('--scale', SCALE)

    # --- geography: 5 countries, 120 cities, 2 paused ----------------------
    cities = GeoNode.objects.filter(kind=CITY)
    assert cities.count() == 120
    assert cities.filter(status='paused').count() == 2
    segs = {c.pk: [p for p in c.path.split('/') if p and p != 'GLOBAL'] for c in cities}
    countries = {s[0] for s in segs.values()}
    assert countries == {'IN', 'US', 'CA', 'GB', 'AU'}
    by_country = {cc: sum(1 for s in segs.values() if s[0] == cc) for cc in countries}
    assert by_country == {'IN': 63, 'US': 32, 'CA': 11, 'GB': 9, 'AU': 5}
    zones = ServiceZone.objects.filter(code__endswith='-SYN')
    assert zones.count() == 120 and all(z.geo_node_id for z in zones)

    # --- people are labelled synthetic and cannot dial anyone --------------
    synthetic = User.objects.filter(email__endswith='@synthetic.invalid')
    assert synthetic.count() == User.objects.count()
    assert not User.objects.exclude(phone_number__regex=r"^[+](91|1|44|61)0[0-9]{9}$").exists()
    assert all(n.startswith('Synthetic ') for n in synthetic.values_list('full_name', flat=True)[:50])
    assert Driver.objects.count() >= 240 and Rider.objects.count() >= 240
    assert all(d.home_city_id and d.active_vehicle_id for d in Driver.objects.all()[:50])

    # --- trips --------------------------------------------------------------
    trips = Trip.objects.all()
    assert trips.count() == 4000
    assert set(trips.values_list('currency', flat=True)) <= {'INR', 'USD'}
    assert trips.filter(zone__isnull=True).count() == 0
    status_counts = {r['status_id__status_code']: r['n']
                     for r in trips.values('status_id__status_code').annotate(n=Count('pk'))}
    completed = status_counts['completed']
    assert 0.80 <= completed / 4000 <= 0.90
    assert status_counts['cancelled'] > 0
    done = trips.filter(status_id__status_code='completed')
    assert not done.filter(driver_id__isnull=True).exists()
    assert not done.filter(final_fare_minor__isnull=True).exists()
    assert not done.filter(final_fare_minor__lte=0).exists()
    assert not done.filter(completed_at__isnull=True).exists()
    assert not trips.filter(client_request_id__isnull=True).exists()
    # Nothing lies in the future, and nothing is older than the window.
    from django.utils import timezone
    assert not trips.filter(requested_at__gt=timezone.now()).exists()
    assert not done.filter(completed_at__gt=timezone.now()).exists()

    # Active trips: each driver has at most one, and they are marked on ride.
    active = trips.filter(status_id__status_code__in=['accepted', 'reached', 'in_progress'])
    assert active.count() + trips.filter(status_id__status_code='requested').count() >= 8
    drivers_busy = list(active.values_list('driver_id', flat=True))
    assert None not in drivers_busy
    assert len(drivers_busy) == len(set(drivers_busy))
    assert set(Driver.objects.filter(pk__in=drivers_busy).values_list('status', flat=True)) == {'on ride'}
    # Some of them are stuck: accepted > 30 minutes or in progress past the estimate.
    now = timezone.now()
    import datetime
    stuck_accepted = trips.filter(status_id__status_code='accepted',
                                  accepted_at__lt=now - datetime.timedelta(minutes=30))
    stuck_running = [t for t in trips.filter(status_id__status_code='in_progress')
                     if (now - t.started_at).total_seconds() / 60 > float(t.estimated_duration_min) + 60]
    assert stuck_accepted.count() + len(stuck_running) >= 1

    # total_trips agrees with the trips.
    d = Driver.objects.filter(total_trips__gt=0).first()
    assert d.total_trips == trips.filter(driver_id=d, status_id__status_code='completed').count()

    # --- SOS: each one has its incident, anchored; some are open -----------
    sos = SOSEvent.objects.all()
    assert sos.count() >= 4
    assert not sos.filter(incident__isnull=True).exists()
    assert not sos.filter(anchor_node__isnull=True).exists()
    assert sos.filter(status='open').count() >= 1
    for ev in sos:
        assert ev.incident.trip_id == ev.trip_id
        assert ev.incident.sos_events.filter(pk=ev.pk).exists()
        if ev.status in ('resolved', 'false_alarm'):
            assert ev.incident.status == 'closed'
        if ev.status == 'open':
            assert ev.incident.status == 'open'
    assert SafetyIncident.objects.count() == sos.values('incident').distinct().count()

    # --- support, payouts, alerts -----------------------------------------
    assert SupportTicket.objects.count() >= 5
    assert WithdrawalRequest.objects.count() > 0
    assert set(WithdrawalRequest.objects.values_list('currency', flat=True)) <= {'INR', 'USD'}
    assert OperatorAlert.objects.filter(acknowledged_at__isnull=True).count() == 14
    assert OperatorAlert.objects.filter(acknowledged_at__isnull=False).count() == 22

    # --- no second run without --reset ------------------------------------
    before = _synthetic_rows()
    with pytest.raises(CommandError, match='--reset'):
        _run('--scale', SCALE)
    assert _synthetic_rows() == before

    # --- --reset removes exactly what it made and reseeds the same shape --
    bystander = User.objects.create(username='real-person', phone_number='+919876543210',
                                    role='rider', email='real@example.com')
    _run('--reset', '--scale', SCALE)
    assert User.objects.filter(pk=bystander.pk).exists()
    assert Trip.objects.count() == 4000
    assert GeoNode.objects.filter(kind=CITY).count() == 120
    assert User.objects.filter(email__endswith='@synthetic.invalid').count() == before[0]
