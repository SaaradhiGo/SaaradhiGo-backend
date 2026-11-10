"""Seed the driver requirements for India and the USA (owner brief, 10 Nov; AUDIT §37).

Seeded rather than raised as a console request because only the owner holds approval.check on
QA (AUDIT §35 decision 1). Every later change goes through pricing.change maker-checker.
The maximum vehicle age (15 years in both) is a placeholder for Srini to confirm.
"""
from django.db import migrations

R, O = 'required', 'optional'


def item(need, blocks=False):
    return {'need': need, 'blocks_online': blocks}


COMMON = {
    'profile_photo': item(R, True), 'legal_name': item(R, True), 'date_of_birth': item(R, True),
    'address': item(R, True), 'permanent_address': item(O), 'emergency_contact': item(R, True),
    'driving_licence': item(R, True), 'vehicle': item(R, True), 'vehicle_photos': item(R, True),
    'insurance': item(R, True), 'payout_method': item(R),
}
SEED = {
    'IN': ({**COMMON, 'address_proof': item(R, True), 'aadhaar': item(R, True), 'pan': item(R),
            'police_verification': item(R, True), 'rc': item(R, True), 'permit': item(R, True),
            'fitness': item(R, True), 'puc': item(R, True)}, 18, 15, ['en', 'te', 'hi']),
    'US': ({**COMMON, 'background_check': item(R, True), 'registration': item(R, True),
            'inspection': item(R, True)}, 21, 15, ['en', 'es']),
}
LABEL = 'Owner brief 2026-11-10 (seeded; later changes through maker-checker)'


def seed(apps, schema_editor):
    MarketRegulation = apps.get_model('pricing', 'MarketRegulation')
    for country, (items, age, vehicle_age, languages) in SEED.items():
        row, created = MarketRegulation.objects.get_or_create(
            country=country, state_code='', defaults={'version': 0})
        row.driver_requirements = items
        row.min_driver_age = age
        row.max_vehicle_age_years = vehicle_age
        row.languages = languages
        row.version = (row.version or 0) + 1
        row.updated_by_label = LABEL
        row.reason = 'Driver requirements per market (10 Nov).'
        row.save()


class Migration(migrations.Migration):
    dependencies = [('pricing', '0023_market_driver_requirements')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
