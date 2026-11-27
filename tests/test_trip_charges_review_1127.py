"""Trip charges, step 1 review (27 Nov): waiting is the driver's earnings (not a pass-through), who pays
the authority for an airport or venue fee, and production holds the flag off until reimbursement."""
import pytest

from servers.admin_access import readiness
from servers.approvals.service import InvalidRequest
from servers.platform_ops import flags
from servers.platform_ops.errors import PlatformError
from servers.platform_ops.models import FeatureFlag
from servers.pricing import trip_charges
from servers.pricing.trip_charge_models import AT_GATE, PLATFORM_REMITS, TripChargeItem
from test_pricing_module import geo, world  # noqa: F401
from test_trip_charges_1126 import AIRPORT_BOX, ROAD, _clean, airport, item, lines  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

AIRPORT = {'country': 'IN', 'kind': 'airport', 'name': 'Airport pickup fee', 'scope_code': 'IN-TG-HYD',
           'applies_on': 'pickup', 'who_pays': 'rider', 'amounts': {'sedan': 10000},
           'geometry': {'type': 'Polygon', 'coordinates': AIRPORT_BOX}}


class TestWho:
    def test_waiting_goes_to_the_driver_and_only_the_rest_is_passed_through(self, world):
        wait = item('wait', 'waiting', {'sedan': 200}, free_minutes=3)
        assert trip_charges.item_dict(wait)['who_words'] == 'Rider pays · goes to the driver'
        assert trip_charges.item_dict(airport(applies_on='pickup'))['who_words'] == 'Rider pays · passed through'
        note = trip_charges.preview('IN', {'kind': 'waiting', 'name': 'Waiting', 'scope_code': 'IN-TG-HYD',
                                           'amounts': {'sedan': 200}, 'free_minutes': 3})['impact']['note']
        assert 'driver fee or commission applies' in note

    def test_waiting_is_always_paid_by_the_rider(self, world):
        with pytest.raises(InvalidRequest) as e:
            trip_charges.preview('IN', {'kind': 'waiting', 'name': 'Waiting', 'who_pays': 'platform',
                                        'amounts': {'sedan': 200}})
        assert e.value.code == 'INVALID_WHO_PAYS'


class TestWhoPaysTheAuthority:
    def test_an_airport_is_paid_at_the_gate_unless_run_as_a_permit(self, world):
        out = trip_charges.preview('IN', AIRPORT)
        assert out['item']['collection'] == AT_GATE and 'reimbursed in full' in out['impact']['note']
        with pytest.raises(InvalidRequest) as e:
            trip_charges.preview('IN', {**AIRPORT, 'collection': PLATFORM_REMITS})
        assert e.value.code == 'REMIT_TO_REQUIRED'
        out = trip_charges.preview('IN', {**AIRPORT, 'collection': PLATFORM_REMITS, 'remit_to': 'GMR Hyderabad'})
        assert out['item']['remit_to'] == 'GMR Hyderabad' and 'pays it to GMR Hyderabad' in out['impact']['note']

    def test_the_change_shows_in_words_and_reaches_the_line(self, world):
        airport(applies_on='pickup')
        out = trip_charges.preview('IN', {**AIRPORT, 'code': 'rgia', 'collection': PLATFORM_REMITS,
                                          'remit_to': 'Airport authority'})
        assert {'label': 'Airport pickup fee: who pays the authority',
                'from': 'Driver pays at the gate (reimbursed)',
                'to': 'SaaradhiGo pays the authority (permit)'} in out['changes']
        TripChargeItem.objects.update(collection=PLATFORM_REMITS, remit_to='Airport authority')
        (line,) = lines(pickup=(17.24, 78.43), drop=(17.43, 78.40))
        assert line['collection'] == PLATFORM_REMITS and line['remit_to'] == 'Airport authority'

    def test_tolls_are_paid_at_the_plaza_and_permits_by_saaradhigo(self, world):
        road = trip_charges.preview('IN', {'kind': 'tolled_road', 'name': 'ORR', 'scope_code': 'IN-TG-HYD',
                                           'amounts': {'sedan': 250}, 'corridor_m': 60,
                                           'geometry': {'type': 'LineString', 'coordinates': ROAD}})
        assert road['item']['collection'] == AT_GATE
        permit = trip_charges.preview('IN', {'kind': 'permit', 'name': 'AP permit', 'amounts': {'sedan': 30000},
                                             'remit_to': 'AP Transport', 'geometry': {
                                                 'type': 'Polygon', 'coordinates': AIRPORT_BOX}})
        assert permit['item']['collection'] == PLATFORM_REMITS


class TestProductionHold:
    def _on(self):
        FeatureFlag.objects.filter(key=trip_charges.FLAG).update(default_enabled=True)

    def test_production_never_charges_extras_until_reimbursement_is_live(self, world, settings):
        self._on()
        assert trip_charges._enabled(world.hyd) is True
        settings.IS_PRODUCTION = True
        assert trip_charges._enabled(world.hyd) is False

    def test_production_refuses_turning_the_flag_on(self, world, settings):
        settings.IS_PRODUCTION = True
        _, actor = world.super
        with pytest.raises(PlatformError) as e:
            flags.propose_change(actor, trip_charges.FLAG, default_enabled=True, reason='turn it on')
        assert e.value.code == 'TRIP_EXTRAS_HELD'

    def test_the_launch_checklist_blocks_a_city_with_extras_on(self, world, settings):
        node = world.geo['IN-TG-HYD']
        row = {i['key']: i for i in readiness.city_readiness(node)}['trip_extras']
        assert row['ok'] and 'off here' in row['detail']
        self._on()
        row = {i['key']: i for i in readiness.city_readiness(node)}['trip_extras']
        assert row['ok'] and row['status'] == 'warning'                  # QA: a warning
        settings.PLATFORM_REQUIRE_LIVE_PROVIDERS = True
        row = {i['key']: i for i in readiness.city_readiness(node)}['trip_extras']
        assert row['blocking'] and row['label'] == 'Trip extras (driver reimbursement)'
