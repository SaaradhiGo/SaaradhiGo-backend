"""Server-side sorting on the Payouts and Approvals lists.

The console's column headers sort these lists. The database does the sorting
over every page; the browser never re-orders one page. An unknown sort field is
a 400, never silently ignored, so a column can never look sorted when it is not.
"""

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from conftest import seed_actor, seed_admin_access
from finance_world import build_world, operator
from servers.admin_access.models import GeoNode
from servers.approvals import service
from servers.approvals.actions import SUPPORT_REFUND
from servers.approvals.models import ApprovalRequest
from servers.driver.models import WithdrawalRequest

pytestmark = pytest.mark.django_db

PAYOUTS = '/api/v1/payments/admin/payouts/'
APPROVALS = '/api/v1/approvals/admin/requests/'


@pytest.fixture
def geo(db):
    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


def results(response):
    assert response.status_code == 200, response.content[:400]
    return response.json()['data']['results']


class TestPayouts:
    @pytest.fixture
    def world(self, geo):
        world = build_world(geo)
        # A third Hyderabad payout so amount and request-time orders differ.
        WithdrawalRequest.objects.create(
            driver=world.in_pending.driver, amount=Decimal('5.00'),
            payout_method='upi', status='pending')
        return world

    def test_default_is_newest_request_first(self, world):
        client, _ = world.finance
        rows = results(client.get(PAYOUTS))
        stamps = [r['requested_at'] for r in rows]
        assert stamps == sorted(stamps, reverse=True)

    @pytest.mark.parametrize('ordering, field, reverse', [
        ('amount', 'amount_minor', False), ('-amount', 'amount_minor', True),
        ('requested', 'requested_at', False), ('-id', 'id', True),
    ])
    def test_sorts_on_the_server(self, world, ordering, field, reverse):
        client, _ = world.finance
        rows = results(client.get(f'{PAYOUTS}?ordering={ordering}'))
        assert len(rows) >= 3
        values = [r[field] for r in rows]
        assert values == sorted(values, reverse=reverse)

    def test_sorting_does_not_widen_scope(self, world):
        client, _ = world.finance
        plain = {r['id'] for r in results(client.get(PAYOUTS))}
        assert {r['id'] for r in results(client.get(f'{PAYOUTS}?ordering=-amount'))} == plain
        assert world.us_withdrawal.id not in plain

    def test_an_unknown_sort_is_refused(self, world):
        client, _ = world.finance
        resp = client.get(f'{PAYOUTS}?ordering=driver__user_id__phone_number')
        assert resp.status_code == 400
        assert 'ordering' in str(resp.json()['error'])


class TestApprovals:
    @pytest.fixture
    def rows(self, geo):
        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
        client, maker = operator('super_admin', geo, scope_code='GLOBAL')
        made = []
        for i, amount in enumerate((5_000_000, 9_000_000, 7_000_000)):
            made.append(service.require_approval_or_execute(
                SUPPORT_REFUND, actor=maker, country='IN', amount=amount, currency='INR',
                target_type='trip', target_id=f'T{i}', payload={'trip': f'T{i}'},
                idempotency_key=f'sort:{i}').request)
        return client, made

    def test_default_is_newest_first(self, rows):
        client, made = rows
        assert [r['id'] for r in results(client.get(APPROVALS))] == [m.id for m in reversed(made)]

    def test_sorts_by_amount_both_ways(self, rows):
        client, _ = rows
        up = [r['amount'] for r in results(client.get(f'{APPROVALS}?ordering=amount'))]
        down = [r['amount'] for r in results(client.get(f'{APPROVALS}?ordering=-amount'))]
        assert up == [5_000_000, 7_000_000, 9_000_000]
        assert down == list(reversed(up))

    def test_requests_with_no_amount_go_last_both_ways(self, rows, geo):
        client, made = rows
        ApprovalRequest.objects.filter(pk=made[1].pk).update(amount=None)
        down = [r['amount'] for r in results(client.get(f'{APPROVALS}?ordering=-amount'))]
        up = [r['amount'] for r in results(client.get(f'{APPROVALS}?ordering=amount'))]
        assert down == [7_000_000, 5_000_000, None]
        assert up == [5_000_000, 7_000_000, None]

    def test_an_unknown_sort_is_refused(self, rows):
        client, _ = rows
        resp = client.get(f'{APPROVALS}?ordering=payload')
        assert resp.status_code == 400
        assert resp.json()['error']['code'] == 'INVALID_ORDERING'
