"""Every driver has a driver wallet in their market's currency from the moment the row exists.

22 Nov (owner: "fix the underlying causes"): the daily integrity scan kept finding drivers
without a wallet and wallets in the wrong currency. Wallets were opened lazily, at the first
money event, while several readers (`Wallet.objects.get(...)`) assume one exists; and a wallet
opened before a driver's home city was set took the platform default (INR) even for a US
driver. Now the wallet is opened with the Driver row, and while it holds no entries its
currency follows the driver's market when the home city changes. A wallet with entries is never
relabelled here: that is a Finance decision (`integrity._wrong_currency_wallets`).
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='driver.Driver', dispatch_uid='driver_wallet_on_create')
def ensure_driver_wallet(sender, instance, created, **kwargs):
    if kwargs.get('raw'):
        return  # fixtures
    try:
        from base.countries import currency_for
        from servers.payments.payout_config import driver_payout_country
        from servers.rider.models import Wallet, WalletTransaction, get_wallet

        wallet = get_wallet(instance.user_id, Wallet.SCOPE_DRIVER)
        country = driver_payout_country(instance)
        want = currency_for(country) if country else None
        if (want and (wallet.currency or '').upper() != want
                and not WalletTransaction.objects.filter(user_id=instance.user_id).exists()):
            Wallet.objects.filter(pk=wallet.pk).update(currency=want)
    except Exception:  # noqa: BLE001 - never stop a driver being saved; the daily scan still checks
        logger.exception('driver wallet on create failed for driver %s', getattr(instance, 'pk', None))
