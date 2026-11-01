"""Every row lock that joins another table must be valid on PostgreSQL.

`Model.objects.select_for_update().select_related('nullable_fk')` makes a LEFT OUTER JOIN, and
PostgreSQL refuses `FOR UPDATE` on the nullable side of an outer join ("FOR UPDATE cannot be applied
to the nullable side of an outer join"). SQLite ignores `FOR UPDATE`, so the default test run never
sees it. On QA this was every promotions write after the draft (E2E journey, AUDIT 27.72): submit,
edit, cancel and launch all returned 500.

The fix is `select_for_update(of=('self',))`: lock the row being changed, and read the joined rows.
This test finds every `select_for_update(...)` chained with `select_related(...)` in `servers/` from
the source and runs the same query shape against PostgreSQL, so a new one cannot regress.
"""

import ast
from pathlib import Path

import pytest
from django.apps import apps
from django.db import transaction

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]

SERVERS = Path(__file__).resolve().parents[1] / 'servers'


def _chain(node):
    """The method calls of `A.objects.m1(...).m2(...)...` outermost last, and the base name."""
    calls = []
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append((node.func.attr, node))
        node = node.func.value
    calls.reverse()
    if (isinstance(node, ast.Attribute) and node.attr == 'objects'
            and isinstance(node.value, ast.Name)):
        return node.value.id, calls
    return None, calls


def _locks():
    found = {}
    for path in SERVERS.rglob('*.py'):
        if 'migrations' in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            model, calls = _chain(node)
            names = [n for n, _ in calls]
            if not model or 'select_for_update' not in names or 'select_related' not in names:
                continue
            sfu = dict(calls)['select_for_update']
            of = next((ast.literal_eval(k.value) for k in sfu.keywords if k.arg == 'of'), None)
            related = tuple(ast.literal_eval(a) for a in dict(calls)['select_related'].args)
            app = path.relative_to(SERVERS).parts[0]
            found[(model, related, of, app)] = f'{path.relative_to(SERVERS.parent)}:{node.lineno}'
    return found


def _model(name, app):
    same_app = [m for m in apps.get_models() if m.__name__ == name and m._meta.app_label == app]
    if same_app:
        return same_app[0]
    matches = [m for m in apps.get_models() if m.__name__ == name]
    assert len(matches) == 1, (name, app, matches)
    return matches[0]


LOCKS = _locks()


def test_the_sweep_finds_the_locks():
    assert len(LOCKS) >= 10


@pytest.mark.parametrize('key', sorted(LOCKS, key=str), ids=lambda k: LOCKS[k])
def test_the_lock_is_valid_on_postgres(key):
    name, related, of, app = key
    model = _model(name, app)
    qs = model.objects.select_for_update(**({'of': of} if of else {})).select_related(*related)
    with transaction.atomic():
        list(qs.filter(pk=-1))
