"""Stamp a money row with its currency at write time, so no call site can forget.

WHY A MIXIN RATHER THAN SETTING IT AT EACH CALL SITE
----------------------------------------------------
There are roughly a dozen places that create a money-bearing row -- trip creation in
the consumer, the quote snapshot, receipt issuance, wallet credit, settlement, payout.
Setting `currency=` at each of them works exactly until somebody adds the thirteenth,
and the failure is silent: the row defaults to INR and a US trip's settlement joins the
Indian totals.

So the currency is derived in `save()` from the row's own trip or zone. A new call site
inherits it by existing. The only way to get it wrong is to write SQL that bypasses the
ORM, which is the same exemption `zone_code` and `rate_card_version` already live with.

WHY NOT A DATABASE TRIGGER OR A GENERATED COLUMN
------------------------------------------------
Both would be stronger. Both also require the currency to be derivable by SQL at write
time, and it is not: a trip's currency comes from its zone, and a zone can be re-parented
or have its currency corrected afterwards. The value has to be captured as a fact when
the row is written, which is application-layer work.

PRECEDENCE
----------
An explicitly-set currency always wins. A caller who knows better than the derivation --
a correction, a data migration, a test -- must be able to say so. The derivation only
fills a blank or the bare default.
"""

import logging

from base.money import (
    DEFAULT_CURRENCY,
    assert_currency_change_keeps_scale,
    normalize_currency,
)

logger = logging.getLogger(__name__)


class CurrencyStampedModel:
    """Mixin: fill `self.currency` from this row's trip or zone on save.

    Mixed in BEFORE `models.Model` so its `save` runs first.

    Subclasses declare where to look, in order of preference:

        currency_source_fields = ('trip_id', 'trip', 'zone')

    Each name is resolved as an attribute; the first one that yields an object
    with a usable currency wins.
    """

    # Overridden per model. Checked in order.
    currency_source_fields = ('trip_id', 'trip', 'zone')

    def _derive_currency(self):
        """Walk the declared sources and return the first currency found.

        Never raises. A row that cannot resolve its currency is still worth
        storing -- losing a settlement because a zone lookup failed would be far
        worse than storing it with the default -- but the fallback is logged at
        ERROR because it means an aggregate is about to be subtly wrong.
        """
        for name in self.currency_source_fields:
            try:
                related = getattr(self, name, None)
            except Exception:
                # A FK whose target was deleted, or an unsaved relation.
                continue
            if related is None:
                continue
            found = getattr(related, 'currency', None)
            if found:
                return normalize_currency(found)
            # A trip knows its zone even when it has no stamped currency of its
            # own -- rows written before the column existed.
            zone = getattr(related, 'zone', None)
            zone_currency = getattr(zone, 'currency', None)
            if zone_currency:
                return normalize_currency(zone_currency)
        return None

    def save(self, *args, **kwargs):
        # Only fill a blank or the bare default. An explicit value always wins:
        # a correction, a migration or a test must be able to say so.
        before = getattr(self, 'currency', None)
        after = before
        if not before or before == DEFAULT_CURRENCY:
            derived = self._derive_currency()
            if derived:
                after = derived
            elif not before:
                logger.error(
                    '%s could not resolve a currency from %s; storing %s. Any '
                    'aggregate including this row is now suspect.',
                    type(self).__name__, self.currency_source_fields,
                    DEFAULT_CURRENCY,
                )
                after = DEFAULT_CURRENCY
        # Amounts were converted to minor units with `before`; a stamp to a
        # currency of a different scale would reinterpret them.
        assert_currency_change_keeps_scale(self, before, after)
        self.currency = after

        # Widen `update_fields` ONLY if the stamp actually changed.
        #
        # The tempting version adds 'currency' to every narrow save. That would
        # reintroduce exactly the class of bug the narrow-write discipline exists
        # to prevent: `auto_cancel_trip` passes `update_fields` precisely so a
        # stale in-memory instance cannot overwrite columns it did not intend to
        # touch, and currency would become the one column it always overwrites --
        # from whatever the stale instance happened to hold.
        #
        # So a no-op derivation leaves the caller's field list exactly as given.
        if after != before:
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                fields = set(update_fields)
                fields.add('currency')
                kwargs['update_fields'] = tuple(fields)

        return super().save(*args, **kwargs)
