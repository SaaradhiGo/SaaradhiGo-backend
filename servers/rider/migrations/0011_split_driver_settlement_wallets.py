"""Backfill Wallet.scope so driver settlement money is not rider credit.

Before this, one Wallet row per user held both rider credits and driver
settlement earnings. Every existing row was created by whichever path ran
first. `0010` added `scope` with a default of 'rider', which would have
labelled every driver's settlement balance as spendable rider credit —
exactly the mixing this split exists to prevent.

Rule applied here: if the owning user has a Driver profile, the balance
was produced by `credit_driver_wallet` / withdrawals, so the row is
re-scoped to 'driver'. Riders keep 'rider'. A user who is both gets their
existing row moved to 'driver' (settlement money is the real money) and a
fresh empty 'rider' row is created lazily on first credit.
"""

from django.db import migrations


def split_wallets(apps, schema_editor):
    Wallet = apps.get_model('rider', 'Wallet')
    Driver = apps.get_model('driver', 'Driver')

    driver_user_ids = set(
        Driver.objects.values_list('user_id_id', flat=True)
    )
    if not driver_user_ids:
        return

    updated = (
        Wallet.objects
        .filter(user_id_id__in=driver_user_ids, scope='rider')
        .update(scope='driver')
    )
    print(f'  re-scoped {updated} wallet(s) to driver settlement')


def unsplit_wallets(apps, schema_editor):
    """Reverse: collapse driver-scoped rows back to rider scope.

    Only safe because the unique constraint is (user, scope); if a user
    ended up with both rows this reverse would collide, so we drop the
    empty rider row first.
    """
    Wallet = apps.get_model('rider', 'Wallet')
    for wallet in Wallet.objects.filter(scope='driver'):
        Wallet.objects.filter(
            user_id_id=wallet.user_id_id, scope='rider', balance=0,
        ).delete()
        wallet.scope = 'rider'
        wallet.save(update_fields=['scope'])


class Migration(migrations.Migration):

    dependencies = [
        ('rider', '0010_wallet_scope_alter_wallet_user_id_and_more'),
        ('driver', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(split_wallets, unsplit_wallets),
    ]
