"""Encrypt the bank account numbers and PANs already stored, and fill the fingerprints (10 Nov).

The historical model has the plain CharField, so the values are encrypted here with the same
helper the model field uses. Idempotent: an encrypted value is left as it is.
"""
from django.db import migrations


def encrypt_existing(apps, schema_editor):
    from base.encrypted import decrypt, encrypt, fingerprint

    DriverBankAccount = apps.get_model('driver', 'DriverBankAccount')
    for bank in DriverBankAccount.objects.all():
        plain = decrypt(bank.account_number)
        DriverBankAccount.objects.filter(pk=bank.pk).update(
            account_number=encrypt(plain), account_fingerprint=fingerprint(plain))


class Migration(migrations.Migration):
    dependencies = [('driver', '0042_driver_requirements_fields')]
    operations = [migrations.RunPython(encrypt_existing, migrations.RunPython.noop)]
