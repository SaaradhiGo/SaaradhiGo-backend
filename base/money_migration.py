"""Data migration from `Decimal(10,2)` columns to integer minor units.

Used by one migration per app (ride, payments, driver, rider, pricing). Each
moves `<field>` into `<field>_minor` while both columns exist, and then removes
the Decimal column.

EXACT, OR NOT AT ALL
--------------------
A stored amount is converted by scaling it -- `Decimal('135.68').scaleb(2)`
is exactly `13568` -- never by rounding it. A value that does not land on a
whole number of minor units (a 3-decimal amount, or any fraction in a 0-digit
currency) would need a rounding decision, and a data migration is the wrong
place to make one silently, so it raises instead.

Then a SEPARATE verification pass re-reads every row from the database and
asserts `minor == original * 10**digits` for every converted value. It fails
the migration -- and on PostgreSQL rolls the whole migration back -- if a
single value differs. It is a second read rather than a check of the values
just computed, so it also catches a write that did not land.

The digits come from `base.money.CURRENCIES`, the same table the application
converts with.
"""

from decimal import Decimal

from base.money import exponent, normalize_currency

BATCH = 500


class MoneyMigrationError(RuntimeError):
    pass


def _exact_minor(value, digits, where):
    if value is None:
        return None
    scaled = Decimal(value).scaleb(digits)
    if scaled != scaled.to_integral_value():
        raise MoneyMigrationError(
            f'{where}: {value} is not a whole number of minor units at '
            f'{digits} digits; refusing to round in a migration')
    return int(scaled)


def _digits(row, currency_field):
    return exponent(normalize_currency(getattr(row, currency_field, None)))


def _verify(Model, fields, currency_field):
    bad = []
    for row in Model.objects.all().order_by('pk').iterator(chunk_size=BATCH):
        digits = _digits(row, currency_field)
        for field in fields:
            original = getattr(row, field)
            minor = getattr(row, f'{field}_minor')
            if original is None and minor is None:
                continue
            if (original is None or minor is None
                    or Decimal(minor) != Decimal(original) * (Decimal(10) ** digits)):
                bad.append(f'{Model.__name__}#{row.pk}.{field}: {original} -> {minor}')
    if bad:
        raise MoneyMigrationError(
            f'{len(bad)} converted value(s) differ from original x 10^digits; '
            f'first: {bad[:10]}')


def forward(app_label, model_name, fields, currency_field='currency'):
    """RunPython code: copy each Decimal `field` into `field_minor`, then verify."""
    def run(apps, schema_editor):
        Model = apps.get_model(app_label, model_name)
        pending = []
        for row in Model.objects.all().order_by('pk').iterator(chunk_size=BATCH):
            digits = _digits(row, currency_field)
            for field in fields:
                setattr(row, f'{field}_minor', _exact_minor(
                    getattr(row, field), digits, f'{model_name}#{row.pk}.{field}'))
            pending.append(row)
            if len(pending) >= BATCH:
                Model.objects.bulk_update(pending, [f'{f}_minor' for f in fields])
                pending = []
        if pending:
            Model.objects.bulk_update(pending, [f'{f}_minor' for f in fields])
        _verify(Model, fields, currency_field)
    return run


def backward(app_label, model_name, fields, currency_field='currency'):
    """RunPython reverse: rebuild each Decimal `field` from `field_minor`, then verify."""
    def run(apps, schema_editor):
        Model = apps.get_model(app_label, model_name)
        pending = []
        for row in Model.objects.all().order_by('pk').iterator(chunk_size=BATCH):
            digits = _digits(row, currency_field)
            for field in fields:
                minor = getattr(row, f'{field}_minor')
                setattr(row, field,
                        None if minor is None else Decimal(minor).scaleb(-digits))
            pending.append(row)
            if len(pending) >= BATCH:
                Model.objects.bulk_update(pending, list(fields))
                pending = []
        if pending:
            Model.objects.bulk_update(pending, list(fields))
        _verify(Model, fields, currency_field)
    return run
