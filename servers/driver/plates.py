"""One spelling of a registration plate, in Python and in SQL.

`Vehicle.vehicle_number` is stored as the driver typed it: `TS09AB1234`,
`TS 09 AB 1234` and `ts-09-ab-1234` are the same plate and three different
strings. Search has to find all three from any one of them, and it has to
do so through an index rather than by normalising every row at query time.

So the normalisation is written ONCE as a database expression -- used by
the `vehicle_plate_norm_idx` expression index on the model and by the
search query, which must match it exactly for PostgreSQL to use the index
-- and once more in Python for the term the operator typed.
"""

import re

from django.db.models import F, Value
from django.db.models.functions import Replace, Upper

_PLATE_NOISE = re.compile(r'[\s\-]+')


def normalise_plate(text):
    """`ts-09 ab 1234` -> `TS09AB1234`. Empty input stays empty."""
    if not text:
        return ''
    return _PLATE_NOISE.sub('', str(text)).upper()


def normalised_plate_expression(field='vehicle_number'):
    """`UPPER(REPLACE(REPLACE(vehicle_number, ' ', ''), '-', ''))`.

    Built from `F()` rather than a bare field name so the SAME object
    compiles identically whether it is the index definition or an
    annotation on a query.
    """
    return Upper(
        Replace(
            Replace(F(field), Value(' '), Value('')),
            Value('-'), Value(''),
        )
    )
