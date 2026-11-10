"""The web's `X-SG-Surface` header passes the CORS preflight (9 Nov regression on QA)."""
import pytest
from django.test import override_settings


@pytest.mark.django_db
@override_settings(CORS_ALLOWED_ORIGINS=['https://www.qa.saaradhigo.com'], CORS_ALLOW_ALL_ORIGINS=False)
def test_preflight_allows_the_surface_header(client):
    r = client.options('/api/v1/ride/app-config/', HTTP_ORIGIN='https://www.qa.saaradhigo.com',
                       HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
                       HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,x-sg-surface')
    assert r.status_code == 200
    allowed = r.headers.get('access-control-allow-headers', '')
    assert 'x-sg-surface' in allowed and 'authorization' in allowed
