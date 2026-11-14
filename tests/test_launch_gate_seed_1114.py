"""14 Nov (owner): the production launch gate opens only Hyderabad. Production runs migrations,
then a person runs `seed_admin_access` (deploy.yml migrates only); on a fresh database that seed
leaves Vijayawada, Visakhapatnam and Warangal in draft (not open), and a rerun never reopens a city."""
import pytest

from servers.admin_access.models import ACTIVE, CITY, DRAFT, GeoNode
from tests.conftest import seed_admin_access


@pytest.mark.django_db
def test_fresh_seed_opens_only_hyderabad():
    GeoNode.objects.all().delete()
    seed_admin_access()
    live = set(GeoNode.objects.filter(kind=CITY, status=ACTIVE).values_list('code', flat=True))
    assert live == {'IN-TG-HYD'}
    for code in ('IN-AP-VJA', 'IN-AP-VTZ', 'IN-TG-WGL'):
        assert GeoNode.objects.get(code=code).status == DRAFT


@pytest.mark.django_db
def test_a_rerun_never_changes_a_city_status():
    GeoNode.objects.all().delete()
    seed_admin_access()
    GeoNode.objects.filter(code='IN-TG-HYD').update(status='paused')
    seed_admin_access()
    assert GeoNode.objects.get(code='IN-TG-HYD').status == 'paused'
    assert GeoNode.objects.get(code='IN-AP-VJA').status == DRAFT
