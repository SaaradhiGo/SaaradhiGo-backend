"""Structural guard: no code path changes a wallet balance without writing a ledger line (AUDIT 27.76).

Owner decision on review B (MEDIUM-4, treated as HIGH): every driver balance change writes an
append-only ledger entry. The rider self-refund used to take the driver's earnings back with a bare
`balance_minor -=` and only a report row, so the driver's statement could not explain the balance.

WHAT IT CHECKS
--------------
It parses every module under `servers/` and `base/` (not migrations, not tests) and finds each
function that changes a wallet balance: an assignment or augmented assignment to `.balance_minor` or
`.balance`, or a queryset `.update(balance_minor=...)`. That same function must also write the
ledger line: a `WalletTransaction.objects.create(...)` / `get_or_create(...)` call. Setting a
missing balance to zero (`= Decimal('0.00')`, `= 0`) is an initialisation, not a change.

A function added tomorrow that moves money without a ledger line fails this test the moment it is
written. `ALLOWLIST` is the only escape hatch: each entry names the function and why, is asserted to
still exist and still need the exemption, and its size is pinned. The planted-violation test proves
the scan bites.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ('servers', 'base')
BALANCE_ATTRS = {'balance_minor', 'balance'}

#: (module path, function name) -> why it may change a balance without a ledger write of its own.
ALLOWLIST = {
    ('servers/payments/tasks.py', 'reconcile_stuck_payments'):
        'Completes a RIDER wallet top-up: its WalletTransaction was written as pending when the '
        'order was created, and this flips that same row to completed with the balance.',
    ('servers/payments/views.py', '_handle_cashfree_webhook'):
        'Same, from the gateway webhook: the pending top-up WalletTransaction becomes completed in '
        'the same transaction as the rider balance.',
}


def _is_zero(node):
    if isinstance(node, ast.Constant) and node.value in (0, 0.0):
        return True
    return (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'Decimal'
            and node.args and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value) in ('0', '0.00'))


def _changes_balance(node):
    if isinstance(node, ast.AugAssign):
        return isinstance(node.target, ast.Attribute) and node.target.attr in BALANCE_ATTRS
    if isinstance(node, ast.Assign):
        return (any(isinstance(t, ast.Attribute) and t.attr in BALANCE_ATTRS for t in node.targets)
                and not _is_zero(node.value))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'update':
        return any(k.arg in BALANCE_ATTRS for k in node.keywords)
    return False


def _writes_ledger(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ('create', 'get_or_create')):
        return False
    manager = node.func.value
    return (isinstance(manager, ast.Attribute) and manager.attr == 'objects'
            and isinstance(manager.value, ast.Name) and manager.value.id == 'WalletTransaction')


def offenders(source, path):
    """`[(path, function, line)]` of functions that change a balance and write no ledger line."""
    out = []
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # The function's own body, not functions nested inside it (they are checked on their own).
        own = []
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            own.append(node)
            stack.extend(ast.iter_child_nodes(node))
        changes = [n for n in own if _changes_balance(n)]
        if changes and not any(_writes_ledger(n) for n in own):
            out.append((path, fn.name, changes[0].lineno))
    return out


def scan():
    found = []
    for top in SCANNED:
        for path in (ROOT / top).rglob('*.py'):
            if 'migrations' in path.parts or 'tests' in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            found.extend(offenders(path.read_text(encoding='utf-8'), rel))
    return found


def test_every_balance_change_writes_a_ledger_line():
    unexplained = [(p, f, line) for p, f, line in scan() if (p, f) not in ALLOWLIST]
    assert unexplained == [], unexplained


def test_the_allowlist_is_exactly_what_it_says():
    still_needed = {(p, f) for p, f, _ in scan()}
    assert set(ALLOWLIST) <= still_needed, set(ALLOWLIST) - still_needed
    assert len(ALLOWLIST) == 2


def test_the_scan_finds_the_balance_changes():
    """It sees the real ones (which pass because they write their line)."""
    source = (ROOT / 'servers/driver/utils.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    names = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
             and any(_changes_balance(n) for n in ast.walk(fn))}
    assert {'credit_driver_wallet', 'credit_fare_collected_by_platform',
            'reverse_trip_earnings'} <= names


def test_a_planted_bare_balance_edit_is_reported():
    planted = '''
def take_it_back(wallet, amount):
    wallet.balance_minor = wallet.balance_minor - amount
    wallet.save(update_fields=['balance_minor'])


def with_a_line(wallet, amount, user):
    wallet.balance_minor -= amount
    wallet.save()
    WalletTransaction.objects.create(user_id=user, amount_minor=amount, txn_type='debit')


def bulk(qs):
    qs.update(balance_minor=0 + 5)


def initialise(wallet):
    wallet.balance = Decimal('0.00')
'''
    assert [(f, line) for _, f, line in offenders(planted, 'planted.py')] == [
        ('take_it_back', 3), ('bulk', 14)]
