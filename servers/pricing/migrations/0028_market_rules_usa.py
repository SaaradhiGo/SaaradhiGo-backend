"""Per-state operating rules on MarketRegulation (14 Nov, USA from day 1), and the Idaho row.

ADDITIVE: nullable columns only; NULL means "not set here" (the country value, then the code
default, applies), so India behaves exactly as before. The data step creates the US-ID row
from the Idaho TNC Act (Idaho Code Title 49, Chapter 37; insurance 41-2519). Values a person
must give (the platform policy, the agent for service of process) stay empty.
"""
from django.db import migrations, models

IDAHO = {
    'cash_allowed': False,
    'tips_enabled': True,
    'tip_presets': [15, 18, 20],
    'sales_tax_enabled': False,
    'vehicle_classes': ['standard', 'xl'],
    'min_driver_age': 19,
    'min_vehicle_year': 2011,
    'vehicle_min_doors': 4,
    'trip_record_retention_days': 730,
    'driver_record_retention_days': 365,
    'complaint_record_retention_days': 730,
    'background_recheck_months': 12,
    'zero_tolerance_suspend': True,
    'platform_insurance_required': True,
    'fee_plan_required': True,
    'languages': ['en', 'es'],
    # Idaho Code 49-3710: the application, a local and national criminal check including the
    # national sex offender registry, a driving record; licence, registration and insurance.
    'driver_requirements': {
        k: {'need': 'required', 'blocks_online': True} for k in (
            'profile_photo', 'legal_name', 'date_of_birth', 'address', 'emergency_contact',
            'accessibility_ack', 'driving_licence', 'background_consent', 'background_check',
            'vehicle', 'vehicle_photos', 'registration', 'insurance', 'payout_method')
    } | {k: {'need': 'off', 'blocks_online': False} for k in (
        'aadhaar', 'pan', 'police_verification', 'address_proof', 'permanent_address', 'rc', 'permit',
        'fitness', 'puc', 'inspection')},
    'citations': {
        'cash_allowed': 'Idaho Code 49-3712',
        'min_driver_age': 'Idaho Code 49-3710(2)(g)',
        'background_check': 'Idaho Code 49-3710(1)(b)-(c)',
        'trip_record_retention_days': 'Idaho Code 49-3714(1)',
        'driver_record_retention_days': 'Idaho Code 49-3714(2)',
        'complaint_record_retention_days': 'Idaho Code 49-3709(3)',
        'zero_tolerance_suspend': 'Idaho Code 49-3709(2)',
        'platform_insurance': 'Idaho Code 41-2519',
        'service_agent': 'Idaho Code 49-3705',
        'driver_photo_and_plate': 'Idaho Code 49-3707',
        'fare_disclosure': 'Idaho Code 49-3706',
    },
    'note': 'Idaho TNC Act; legal review pending',
}


def seed(apps, schema_editor):
    MarketRegulation = apps.get_model('pricing', 'MarketRegulation')
    row, created = MarketRegulation.objects.get_or_create(
        country='US', state_code='ID', defaults={'currency': 'USD', 'version': 1, **IDAHO})
    if not created:
        changed = False
        for k, v in IDAHO.items():
            if getattr(row, k, None) in (None, ''):
                setattr(row, k, v)
                changed = True
        if changed:
            row.save()


class Migration(migrations.Migration):
    dependencies = [('pricing', '0027_zone_low_rating')]

    operations = [
        migrations.AddField(model_name='marketregulation', name='cash_allowed', field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='tips_enabled', field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='tip_presets', field=models.JSONField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='sales_tax_enabled', field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='vehicle_classes', field=models.JSONField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='min_vehicle_year', field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='vehicle_min_doors', field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='trip_record_retention_days', field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='driver_record_retention_days', field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='complaint_record_retention_days', field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='background_recheck_months', field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='zero_tolerance_suspend', field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='platform_insurance_required', field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='platform_insurance', field=models.JSONField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='fee_plan_required', field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='service_agent', field=models.JSONField(blank=True, null=True)),
        migrations.AddField(model_name='marketregulation', name='citations', field=models.JSONField(blank=True, null=True)),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
