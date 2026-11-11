"""An expression index that also serves `LIKE 'prefix%'` on PostgreSQL.

WHY A SUBCLASS AND NOT `django.contrib.postgres.indexes.OpClass`
----------------------------------------------------------------
Console search matches a normalised plate or the national part of a phone
number by EXACT value and by PREFIX. Equality on a B-tree over the
expression is free on every backend. The prefix is not: on PostgreSQL a
plain B-tree only answers `LIKE 'TS09%'` when the database collation is
`C`, and Railway's is not, so the same index that serves `=` would leave
the prefix search as a sequential scan over every vehicle and every
account. The fix is the `text_pattern_ops` operator class, which indexes
the bytes rather than the collation order and answers both `=` and a
left-anchored `LIKE`.

`OpClass(...)` is how Django spells that, but it is PostgreSQL-only: on
SQLite it emits `expr text_pattern_ops`, which is a syntax error, and the
test suite runs on SQLite. So the opclass is added only when the schema
editor is talking to PostgreSQL; everywhere else this is an ordinary
expression index.

THE EXPRESSION MUST BE THE ONE THE QUERY USES
---------------------------------------------
PostgreSQL matches an expression index by expression tree, not by name.
The model that declares the index and the view that queries it must
therefore build the expression from the SAME helper (`servers.driver.plates`
for plates, `phone_tail()` below for numbers), or the index is correct,
present, and never used.
"""

import copy

from django.db import models
from django.db.models.functions import Right

#: The national part of an E.164 number. India and the United States, the
#: two markets, both have ten-digit national numbers, so the last ten
#: digits are what a human reads off a call screen or types from memory.
PHONE_TAIL_DIGITS = 10


def phone_tail(field='phone_number'):
    """`RIGHT(phone_number, 10)` -- the expression indexed AND queried."""
    return Right(field, PHONE_TAIL_DIGITS)


class PatternPrefixIndex(models.Index):
    """A B-tree over an expression that also answers `startswith`.

    On PostgreSQL the expression is wrapped in `text_pattern_ops`; on every
    other backend it is a plain expression index. `deconstruct()` is
    inherited, so a migration serialises this as the plain expression and
    the opclass decision is made at `CREATE INDEX` time, where the backend
    is known.
    """

    def create_sql(self, model, schema_editor, using='', **kwargs):
        if schema_editor.connection.vendor != 'postgresql' or not self.expressions:
            return super().create_sql(model, schema_editor, using=using, **kwargs)

        from django.contrib.postgres.indexes import OpClass
        from django.db.models import OrderBy
        from django.db.models.functions import Collate
        from django.db.models.indexes import IndexExpression

        # `OpClass` is only recognised as an index WRAPPER (rather than
        # parenthesised as the expression itself, which is a syntax error)
        # once it is registered. `django.contrib.postgres` does that in its
        # `AppConfig.ready()`; this project does not install that app, so
        # the same registration is made here. Idempotent.
        if OpClass not in IndexExpression.wrapper_classes:
            IndexExpression.register_wrappers(OrderBy, OpClass, Collate)

        # A shallow copy so the model's own Index (which migrations compare
        # against the on-disk state) is never mutated.
        wrapped = copy.copy(self)
        wrapped.expressions = tuple(
            OpClass(expression, name='text_pattern_ops')
            for expression in self.expressions
        )
        return models.Index.create_sql(wrapped, model, schema_editor, using=using, **kwargs)
