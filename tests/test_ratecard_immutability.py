"""A RateCard that priced a ride must not be editable into a different one.

`rate_card_version` on a fare snapshot is only meaningful if the row it points at
cannot change. Before this, it could: `admin_dashboard`'s fare-update view loaded
the active card and called `.save()` on it, so changing a city's fares rewrote the
schedule that historical trips had been priced under. "Which rates priced this
trip?" was answerable only as "whatever the row says today".

The guard lives on the model rather than in the views, because there are four write
paths (Django admin, the DRF ModelViewSet, the ops-console fare form, and data
migrations) and a per-view rule would have to be remembered four times.

Operational correction stays possible, deliberately: retiring a card, closing its
window and annotating it are all still allowed, and a genuine mistyped-before-use
correction has an explicit audited escape hatch.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from servers.driver.models import VehicleType
from servers.pricing.models import RateCard, ServiceZone
from servers.pricing.services import get_active_rate_card


@pytest.fixture
def zone(db):
    z, _ = ServiceZone.objects.get_or_create(
        code='IMMUT-TEST',
        defaults={
            'name': 'Immutability test', 'zone_type': 'city',
            'state_code': 'TS', 'city': 'Hyderabad',
            'polygon_geojson': {
                'type': 'Polygon',
                'coordinates': [[[78.0, 17.0], [79.0, 17.0], [79.0, 18.0],
                                 [78.0, 18.0], [78.0, 17.0]]],
            },
        },
    )
    return z


@pytest.fixture
def vehicle_type(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def card(zone, vehicle_type):
    return RateCard.objects.create(
        zone=zone, vehicle_type=vehicle_type,
        base_fare=Decimal('30.00'), per_km_fare=Decimal('12.00'),
        per_min_fare=Decimal('2.00'), min_fare=Decimal('50.00'),
        commission_percent=Decimal('18.00'), gst_percent=Decimal('5.00'),
        surge_cap_multiplier=Decimal('1.50'),
        version=1, is_active=True,
    )


# ---------------------------------------------------------------------------
# The core refusal
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_editing_a_price_on_an_existing_card_is_refused(card):
    """The defect, as a test. This is what the ops console was doing."""
    card.per_km_fare = Decimal('14.00')

    with pytest.raises(ValidationError) as exc:
        card.save()

    assert 'per_km_fare' in str(exc.value)
    card.refresh_from_db()
    assert card.per_km_fare == Decimal('12.00'), 'the stored rate must not move'


@pytest.mark.django_db
@pytest.mark.parametrize('field,value', [
    ('base_fare', Decimal('35.00')),
    ('per_km_fare', Decimal('13.50')),
    ('per_min_fare', Decimal('2.50')),
    ('min_fare', Decimal('60.00')),
    ('commission_percent', Decimal('25.00')),
    ('gst_percent', Decimal('12.00')),
    ('surge_cap_multiplier', Decimal('2.00')),
    ('night_surge_multiplier', Decimal('1.40')),
])
def test_every_pricing_field_is_protected(card, field, value):
    """Commission and GST included: both determine what money moved."""
    setattr(card, field, value)
    with pytest.raises(ValidationError):
        card.save()


@pytest.mark.django_db
def test_update_fields_cannot_smuggle_a_price_change_through(card):
    """A narrow save is still a save. `update_fields` must not be an escape."""
    card.per_km_fare = Decimal('99.00')
    with pytest.raises(ValidationError):
        card.save(update_fields=['per_km_fare'])


@pytest.mark.django_db
def test_a_brand_new_card_saves_freely(zone, vehicle_type):
    """Creation is not an edit. Seeding a city must keep working."""
    fresh = RateCard.objects.create(
        zone=zone, vehicle_type=vehicle_type,
        base_fare=Decimal('40.00'), per_km_fare=Decimal('15.00'),
        per_min_fare=Decimal('3.00'), min_fare=Decimal('70.00'),
        version=1,
    )
    assert fresh.pk is not None


# ---------------------------------------------------------------------------
# Operational correction must not become impossible
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_retiring_a_card_is_still_allowed(card):
    """Deactivation is lifecycle, not price. Ops must keep this."""
    card.is_active = False
    card.save()
    card.refresh_from_db()
    assert card.is_active is False


@pytest.mark.django_db
def test_closing_the_effective_window_is_still_allowed(card):
    card.effective_to = timezone.now()
    card.save()
    card.refresh_from_db()
    assert card.effective_to is not None


@pytest.mark.django_db
def test_notes_can_still_be_edited(card):
    card.notes = 'Superseded after the October review.'
    card.save()
    card.refresh_from_db()
    assert 'October' in card.notes


# ---------------------------------------------------------------------------
# The supported way to change a price
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_price_change_creates_a_new_version(card):
    """change pricing -> new RateCard version, not an in-place edit."""
    successor = card.new_version(per_km_fare=Decimal('14.00'))

    assert successor.pk != card.pk
    assert successor.version == 2
    assert successor.per_km_fare == Decimal('14.00')
    # Everything not overridden is inherited, so a version bump is not a rewrite.
    assert successor.base_fare == card.base_fare
    assert successor.commission_percent == card.commission_percent
    assert successor.zone_id == card.zone_id

    card.refresh_from_db()
    assert card.per_km_fare == Decimal('12.00'), 'history is untouched'
    assert card.effective_to is not None, 'the old window is closed'


@pytest.mark.django_db
def test_the_resolver_picks_the_new_version_and_only_one_card(card, zone, vehicle_type):
    """Two live cards for one (zone, vehicle_type) would make pricing ambiguous."""
    card.new_version(per_km_fare=Decimal('14.00'))

    resolved = get_active_rate_card(zone, vehicle_type)
    assert resolved is not None
    assert resolved.version == 2
    assert resolved.per_km_fare == Decimal('14.00')

    live = RateCard.objects.filter(
        zone=zone, vehicle_type=vehicle_type, is_active=True,
        effective_to__isnull=True,
    )
    assert live.count() == 1, 'exactly one open-ended card per zone+vehicle type'


@pytest.mark.django_db
def test_a_historical_quote_is_still_explainable_after_a_price_change(card, zone,
                                                                     vehicle_type):
    """The property this whole change exists for.

    A trip priced under version 1 must still be explainable by reading version 1,
    after version 2 has taken over.
    """
    priced_under = get_active_rate_card(zone, vehicle_type)
    recorded = {
        'version': priced_under.version,
        'per_km_fare': priced_under.per_km_fare,
        'commission_percent': priced_under.commission_percent,
    }

    card.new_version(per_km_fare=Decimal('25.00'),
                     commission_percent=Decimal('30.00'))

    historical = RateCard.objects.get(zone=zone, vehicle_type=vehicle_type,
                                      version=recorded['version'])
    assert historical.per_km_fare == recorded['per_km_fare']
    assert historical.commission_percent == recorded['commission_percent']


# ---------------------------------------------------------------------------
# The audited escape hatch
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_audited_correction_can_edit_a_price_and_says_so(card, caplog):
    """For a card mistyped before it priced anything. Loud on purpose."""
    import logging

    with caplog.at_level(logging.WARNING, logger='servers.pricing.models'):
        card.apply_audited_correction(
            reason='Typo: per_km entered as 12.00, tariff order says 13.00',
            actor_label='qa-pilot-admin',
            per_km_fare=Decimal('13.00'),
        )

    card.refresh_from_db()
    assert card.per_km_fare == Decimal('13.00')

    records = [r for r in caplog.records if r.msg == 'ratecard_pricing_corrected']
    assert records, 'a correction must be logged'
    logged = records[0].__dict__
    assert logged['rate_card_id'] == card.pk
    assert 'per_km_fare' in logged['changed']
    assert 'Typo' in logged['reason']
    assert logged['actor'] == 'qa-pilot-admin'


@pytest.mark.django_db
def test_a_correction_without_a_reason_is_refused(card):
    """An unexplained edit to money is not a correction."""
    with pytest.raises(ValueError):
        card.apply_audited_correction(reason='', per_km_fare=Decimal('13.00'))


@pytest.mark.django_db
def test_a_correction_cannot_be_used_for_non_pricing_fields(card):
    """Keeps the audited path narrow: it exists for prices, nothing else."""
    with pytest.raises(ValueError):
        card.apply_audited_correction(reason='x', is_active=False)


@pytest.mark.django_db
def test_the_guard_re_arms_after_a_correction(card):
    """One correction must not leave the row permanently editable."""
    card.apply_audited_correction(reason='typo', per_km_fare=Decimal('13.00'))

    card.per_km_fare = Decimal('20.00')
    with pytest.raises(ValidationError):
        card.save()


# ---------------------------------------------------------------------------
# The write paths that used to allow an in-place edit
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_admin_api_refuses_a_pricing_edit_with_a_useful_error(card, db):
    """RateCard.save() would raise anyway; the ViewSet turns that into a 409 that
    says what to do instead."""
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    User = get_user_model()
    admin = User.objects.create_user(phone_number='+919700000902', role='admin')
    admin.is_staff = True
    admin.save()

    api = APIClient()
    api.force_authenticate(user=admin)
    resp = api.patch(f'/api/v1/pricing/admin/rate-cards/{card.pk}/',
                     {'per_km_fare': '14.00'}, format='json')

    assert resp.status_code == 409, resp.content
    body = resp.json()
    assert 'per_km_fare' in body['immutable_fields']
    card.refresh_from_db()
    assert card.per_km_fare == Decimal('12.00')


@pytest.mark.django_db
def test_the_admin_api_still_allows_retiring_a_card(card):
    """Lifecycle edits must pass straight through."""
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    User = get_user_model()
    admin = User.objects.create_user(phone_number='+919700000903', role='admin')
    admin.is_staff = True
    admin.save()

    api = APIClient()
    api.force_authenticate(user=admin)
    resp = api.patch(f'/api/v1/pricing/admin/rate-cards/{card.pk}/',
                     {'is_active': False}, format='json')

    assert resp.status_code == 200, resp.content
    card.refresh_from_db()
    assert card.is_active is False


@pytest.mark.django_db
def test_a_bulk_queryset_update_cannot_change_pricing(card, zone, vehicle_type):
    """`.update()` never calls save(), so the model guard alone was one queryset
    away from useless."""
    with pytest.raises(ValidationError):
        RateCard.objects.filter(pk=card.pk).update(per_km_fare=Decimal('99.00'))

    card.refresh_from_db()
    assert card.per_km_fare == Decimal('12.00')


@pytest.mark.django_db
def test_a_bulk_retirement_is_still_allowed(card, zone, vehicle_type):
    """Deactivating a batch of cards is a legitimate operation."""
    RateCard.objects.filter(zone=zone).update(is_active=False)
    card.refresh_from_db()
    assert card.is_active is False
