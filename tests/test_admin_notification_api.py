"""Regression: the console's notification bell polled a route that did not exist.

THE DEFECT
----------
`templates/admin_pages/base.html` called `/api/notifications/` on every console
page load and `/api/notifications/<id>/read/` on dismiss. Neither was routed.
Django returned its HTML 404 page, the bell ran `res.json()` over
`<!DOCTYPE html>`, and every admin page logged:

    Notification fetch error: SyntaxError: Unexpected token '<'

The bell therefore never displayed a single notification, and the console
carried a JavaScript exception on every page view. It was found by driving the
deployed console in a browser and reading the console log -- no Python test
could have caught it, because no Python code referenced the broken URL.

WHAT THESE TESTS PIN
--------------------
1. The two URLs resolve at the exact paths the template calls.
2. They answer JSON -- never HTML -- in every case, including refusal. An HTML
   body is what produced the original parse error, so "it refused correctly"
   is not enough; it has to refuse in a shape the caller can read.
3. Authorisation: operators only, and only their own rows.
4. The payload carries the five keys the template renders.
5. Mark-read is idempotent and cannot touch another account's notification.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from servers.rider.models import Notification

User = get_user_model()

pytestmark = pytest.mark.django_db


# The literal paths the template fetches. Written out rather than reversed,
# because the point of the test is that THESE strings resolve -- reversing
# would pass even if the path moved and the template broke again.
FEED_PATH = '/api/notifications/'


def _read_path(notif_id):
    return f'/api/notifications/{notif_id}/read/'


@pytest.fixture
def operator():
    return User.objects.create_user(
        phone_number='+919700000501', username='ops_notif', role='admin',
        is_staff=True, is_superuser=True,
    )


@pytest.fixture
def other_operator():
    return User.objects.create_user(
        phone_number='+919700000502', username='ops_notif2', role='admin',
        is_staff=True, is_superuser=True,
    )


@pytest.fixture
def rider():
    return User.objects.create_user(
        phone_number='+919700000503', username='rider_notif', role='rider',
    )


@pytest.fixture
def console(operator):
    client = Client()
    client.force_login(operator)
    return client


def _notify(user, **kw):
    kw.setdefault('title', 'Driver document expired')
    kw.setdefault('message', 'Vehicle RC for DL-01-AA-1111 expired today.')
    kw.setdefault('notif_type', 'kyc')
    return Notification.objects.create(user_id=user, **kw)


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

def test_feed_path_is_routed(console):
    """The exact path base.html polls must not 404."""
    response = console.get(FEED_PATH)
    assert response.status_code == 200, (
        'base.html polls this path on every page load; a 404 here is the '
        'original defect returning.'
    )


def test_mark_read_path_is_routed(console, operator):
    notif = _notify(operator)
    response = console.post(_read_path(notif.id))
    assert response.status_code == 200


def test_routes_are_reversible_by_name(operator):
    """The template builds these with {% url %}, so the names must exist."""
    assert reverse('admin_notifications_feed') == FEED_PATH
    assert reverse('admin_notification_mark_read', args=[7]) == _read_path(7)


# --------------------------------------------------------------------------
# the response is JSON in every case -- this is what removes the parse error
# --------------------------------------------------------------------------

def test_feed_answers_json_not_html(console):
    response = console.get(FEED_PATH)
    assert response['Content-Type'].startswith('application/json')
    json.loads(response.content)  # would raise on an HTML error page


def test_refusal_is_json_not_an_html_redirect(rider):
    """A refused caller must still get JSON.

    `@admin_required` redirects to the HTML login page. Had these endpoints
    used it, an unauthorised poll would have followed the redirect, received
    HTML and raised the very SyntaxError this change removes.
    """
    client = Client()
    client.force_login(rider)
    response = client.get(FEED_PATH)

    assert response.status_code == 403
    assert response['Content-Type'].startswith('application/json')
    body = json.loads(response.content)
    assert body['status'] == 'forbidden'
    assert body['notifications'] == []


def test_anonymous_caller_is_refused_in_json(client):
    response = client.get(FEED_PATH)
    assert response.status_code == 403
    assert response['Content-Type'].startswith('application/json')
    assert json.loads(response.content)['notifications'] == []


def test_missing_notification_is_json_404(console):
    response = console.post(_read_path(999999))
    assert response.status_code == 404
    assert response['Content-Type'].startswith('application/json')
    assert json.loads(response.content)['status'] == 'not_found'


# --------------------------------------------------------------------------
# payload contract
# --------------------------------------------------------------------------

def test_payload_has_the_keys_the_template_renders(console, operator):
    _notify(operator)
    body = json.loads(console.get(FEED_PATH).content)

    assert body['status'] == 'ok'
    assert len(body['notifications']) == 1
    row = body['notifications'][0]
    # base.html reads exactly these.
    assert set(row) == {
        'id', 'title', 'message', 'created_at', 'notification_type',
    }
    assert row['title'] == 'Driver document expired'


@pytest.mark.parametrize('notif_type,expected', [
    ('sos', 'ALERT'),
    ('kyc', 'WARNING'),
    ('payment', 'WARNING'),
    ('payout', 'WARNING'),
    ('wallet', 'WARNING'),
    ('ride_event', 'INFO'),
    ('system', 'INFO'),
    ('marketing', 'INFO'),
    ('', 'INFO'),
    ('something_added_later', 'INFO'),
])
def test_severity_mapping(console, operator, notif_type, expected):
    """Unknown categories fall back to INFO rather than rendering no icon."""
    _notify(operator, notif_type=notif_type)
    row = json.loads(console.get(FEED_PATH).content)['notifications'][0]
    assert row['notification_type'] == expected


def test_only_unread_rows_are_returned(console, operator):
    """The badge count is `notifications.length`, so read rows must not appear."""
    _notify(operator, title='unread one')
    _notify(operator, title='already seen', is_read=True)

    rows = json.loads(console.get(FEED_PATH).content)['notifications']
    assert [r['title'] for r in rows] == ['unread one']


def test_newest_first(console, operator):
    _notify(operator, title='older')
    _notify(operator, title='newer')
    rows = json.loads(console.get(FEED_PATH).content)['notifications']
    assert [r['title'] for r in rows] == ['newer', 'older']


def test_feed_is_capped(console, operator):
    from servers.admin_dashboard.notification_api import FEED_LIMIT

    for i in range(FEED_LIMIT + 5):
        _notify(operator, title=f'n{i}')

    rows = json.loads(console.get(FEED_PATH).content)['notifications']
    assert len(rows) == FEED_LIMIT


# --------------------------------------------------------------------------
# scope: one operator never sees or mutates another's rows
# --------------------------------------------------------------------------

def test_feed_shows_only_the_callers_own_notifications(
    console, operator, other_operator,
):
    _notify(operator, title='mine')
    _notify(other_operator, title='not mine')

    rows = json.loads(console.get(FEED_PATH).content)['notifications']
    assert [r['title'] for r in rows] == ['mine']


def test_cannot_mark_another_operators_notification_read(
    console, other_operator,
):
    theirs = _notify(other_operator)
    response = console.post(_read_path(theirs.id))

    assert response.status_code == 404, (
        "another operator's id must look like it does not exist, not like "
        'a permission error that confirms the row is there'
    )
    theirs.refresh_from_db()
    assert theirs.is_read is False


def test_mark_read_actually_marks_read(console, operator):
    notif = _notify(operator)
    assert notif.is_read is False

    console.post(_read_path(notif.id))

    notif.refresh_from_db()
    assert notif.is_read is True


def test_mark_read_is_idempotent(console, operator):
    """The bell may retry or be double-clicked; neither is an error."""
    notif = _notify(operator)

    first = console.post(_read_path(notif.id))
    second = console.post(_read_path(notif.id))

    assert first.status_code == 200
    assert second.status_code == 200
    notif.refresh_from_db()
    assert notif.is_read is True


def test_marking_read_removes_it_from_the_feed(console, operator):
    notif = _notify(operator)
    assert len(json.loads(console.get(FEED_PATH).content)['notifications']) == 1

    console.post(_read_path(notif.id))

    assert json.loads(console.get(FEED_PATH).content)['notifications'] == []


# --------------------------------------------------------------------------
# method safety
# --------------------------------------------------------------------------

def test_feed_rejects_post(console):
    assert console.post(FEED_PATH).status_code == 405


def test_mark_read_rejects_get(console, operator):
    notif = _notify(operator)
    assert console.get(_read_path(notif.id)).status_code == 405


@pytest.mark.parametrize('path_factory,method', [
    (lambda: FEED_PATH, 'post'),
    (lambda: _read_path(1), 'get'),
])
def test_authorisation_is_decided_before_method(client, path_factory, method):
    """An unauthenticated caller gets 403, not 405.

    `@require_GET` / `@require_POST` run ahead of the view body, so decorating
    these views made an anonymous caller using the wrong verb receive 405 --
    which both leaks that the route exists and broke
    `test_admin_dashboard_authz`, whose contract is that every admin route
    refuses anonymous callers with a refusing status. Authorisation is checked
    first for exactly that reason.
    """
    response = getattr(client, method)(path_factory())
    assert response.status_code == 403
    assert response['Content-Type'].startswith('application/json')
