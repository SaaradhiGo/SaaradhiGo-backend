"""Wrap every existing PromoCode and IncentiveQuest in a campaign. ADDITIVE: nothing is changed or
deleted on the old rows.

A wrapper has `is_legacy=True`, no budget and no approval (it was live before this module
existed), and it behaves exactly as the old row did: the booking path still serves the
`PromoCode`, the driver app still reads the `IncentiveQuest`. Its state mirrors the old row:
`live` while active and inside its window, `scheduled` before it, `ended` after it, `paused`
when switched off. Existing `PromoRedemption` rows are linked to their wrapper (state stays
`legacy`, so the new settlement engine never touches them).

Reversible: the wrappers are deleted and the redemption links cleared.
"""

from django.db import migrations
from django.utils import timezone

CURRENCY_COUNTRY = {'INR': 'IN', 'USD': 'US'}


def _state(active, start, end, now):
    if end <= now:
        return 'ended'
    if not active:
        return 'paused'
    return 'live' if start <= now else 'scheduled'


def forward(apps, schema_editor):
    PromoCode = apps.get_model('ride', 'PromoCode')
    PromoRedemption = apps.get_model('ride', 'PromoRedemption')
    IncentiveQuest = apps.get_model('driver', 'IncentiveQuest')
    GeoNode = apps.get_model('admin_access', 'GeoNode')
    Campaign = apps.get_model('promotions', 'Campaign')
    now = timezone.now()

    def country_node(currency):
        code = CURRENCY_COUNTRY.get(currency, '')
        return GeoNode.objects.filter(kind='country', code=code).first(), code

    for promo in PromoCode.objects.select_related('zone').order_by('id'):
        if Campaign.objects.filter(promo_code=promo).exists():
            continue
        node = promo.zone.geo_node if promo.zone_id and promo.zone.geo_node_id else None
        node, code = (node, '') if node else country_node(promo.currency)
        country = code or CURRENCY_COUNTRY.get(promo.currency, '')
        campaign = Campaign.objects.create(
            kind='rider_promo', name=promo.code[:120], description=(promo.description or '')[:256],
            state=_state(promo.is_active, promo.valid_from, promo.valid_to, now),
            country=country, geo_node=node, currency=promo.currency, budget_minor=None,
            starts_at=promo.valid_from, ends_at=promo.valid_to, is_legacy=True,
            promo_code=promo, reason='Wrapped from an existing promo code (module 12).',
            config={
                'code': promo.code, 'auto_apply': False, 'discount_type': promo.discount_type,
                'discount_percent': (str(promo.discount_percent)
                                     if promo.discount_percent is not None else None),
                'discount_flat_minor': promo.discount_flat_minor,
                'max_discount_minor': promo.max_discount_amount_minor,
                'min_fare_minor': promo.min_fare_minor,
                'max_per_user': promo.max_per_user_redemptions,
                'max_total_redemptions': promo.max_total_redemptions,
                'zone_code': promo.zone.code if promo.zone_id else '',
                'new_riders_only': False})
        PromoRedemption.objects.filter(promo=promo, campaign__isnull=True).update(
            campaign=campaign)

    for quest in IncentiveQuest.objects.order_by('id'):
        if Campaign.objects.filter(quest=quest).exists():
            continue
        node, code = country_node(quest.currency)
        Campaign.objects.create(
            kind='driver_incentive', name=quest.title[:120],
            description=(quest.description or '')[:256],
            state=_state(quest.is_active, quest.start_date, quest.end_date, now),
            country=code, geo_node=node, currency=quest.currency, budget_minor=None,
            starts_at=quest.start_date, ends_at=quest.end_date, is_legacy=True, quest=quest,
            reason='Wrapped from an existing incentive quest (module 12).',
            config={'target_trips': quest.target_trips, 'reward_minor': quest.reward_amount_minor,
                    'zone_code': '', 'hour_from': None, 'hour_to': None, 'vehicle_type': ''})


def backward(apps, schema_editor):
    PromoRedemption = apps.get_model('ride', 'PromoRedemption')
    Campaign = apps.get_model('promotions', 'Campaign')
    PromoRedemption.objects.filter(campaign__is_legacy=True).update(campaign=None)
    Campaign.objects.filter(is_legacy=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('promotions', '0001_initial'),
        # The link column on PromoRedemption (which itself depends on promotions 0001).
        ('ride', '0027_promotion_redemption_link'),
        ('admin_access', '0001_initial'),
        ('driver', '0035_finance_actions'),
    ]

    operations = [migrations.RunPython(forward, backward)]
