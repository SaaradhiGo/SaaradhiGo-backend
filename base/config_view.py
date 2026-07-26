"""Public client-config endpoint.

GET /api/v1/config/ returns a small, public JSON blob the mobile + web
clients read once at startup to discover server-side feature flags.
Keeping this on the server (instead of compile-time constants in the
app) means we can flip a flag without a new app release.

Currently exposes:
  - wallet.topups_enabled            -- master switch for the rider
                                        wallet top-up flow. False in
                                        Phase-0 (closed-loop credits
                                        posture, ADR-0003).
  - wallet.credits_only              -- explicit signal to the UI that
                                        only refund/promo/support
                                        credits are available.
  - wallet.balance_cap               -- max rupee credit balance.
  - wallet.refund_modes              -- modes the refund endpoint
                                        understands ('original',
                                        'credit').
  - service_area.public_zones_url    -- where to fetch the list of
                                        active ServiceZones.

Add a new key here when you ship a new feature flag. NEVER put a
secret in this payload -- the endpoint is unauthenticated by design.
"""

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from base.utils import success_response


@api_view(['GET'])
@permission_classes([AllowAny])
def public_config(request):
    return success_response(
        {
            'wallet': {
                'topups_enabled': bool(getattr(settings, 'WALLET_TOPUPS_ENABLED', False)),
                'credits_only': not bool(getattr(settings, 'WALLET_TOPUPS_ENABLED', False)),
                'balance_cap': str(getattr(settings, 'RIDER_CREDIT_BALANCE_CAP', '2000.00')),
                'credit_expiry_days': int(getattr(settings, 'RIDER_CREDIT_EXPIRY_DAYS', 365)),
                'refund_modes': ['original', 'credit'],
                'display_name': 'VahanGo Credits',
            },
            'service_area': {
                'public_zones_url': '/api/v1/pricing/zones/',
            },
        },
        status.HTTP_200_OK,
    )
