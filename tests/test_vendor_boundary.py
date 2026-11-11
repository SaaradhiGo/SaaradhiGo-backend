"""THE VENDOR BOUNDARY: no vendor SDK, and no vendor hostname, outside `integrations/`.

Owner ruling (AUDIT 27.62): every third-party provider must be swappable by configuration only,
with no business-code changes. That holds only while business code (`base/` and `servers/`)
never speaks a vendor directly. This test walks every .py file under both and fails on

  * an import of a vendor SDK (AST: `import x`, `from x import y`, relative imports excluded),
    including `importlib.import_module("x")` and `__import__("x")` with a literal name;
  * a vendor API hostname in any string literal (a raw `requests` call to a vendor), docstrings
    excepted.

It runs in the normal suite, so it runs in CI (`pytest`, see .github/workflows/deploy.yml).

THE ALLOWLIST is explicit and commented. Anything added to it needs a reason that is not "it was
easier".
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED = ('base', 'servers')

#: Top-level module names that are vendor SDKs.
VENDOR_MODULES = frozenset({
    'boto3', 'botocore', 'firebase_admin', 'twilio', 'razorpay', 'stripe', 'msg91', 'sendgrid',
    'sentry_sdk',
})
#: Prefixes (module name starts with) that are vendor SDKs.
VENDOR_PREFIXES = ('cashfree', 'google.cloud')

VENDOR_HOSTS = re.compile(
    r'(?:msg91\.com|twilio\.com|cashfree\.com|api\.razorpay\.com|api\.stripe\.com|'
    r'fcm\.googleapis\.com|sns\.[\w-]+\.amazonaws\.com)', re.IGNORECASE)

#: file (posix, relative to the repo root) -> SDK modules it may import, with the reason.
ALLOWED_IMPORTS = {
    # Error-tracking bootstrap. Sentry is initialised once at settings import, before Django
    # is configured, so it cannot live behind a runtime registry. It is observability, not a
    # provider a business flow depends on, and swapping it is a one-file change.
    'base/settings.py': {'sentry_sdk'},
}
#: Nothing is allowed to name a vendor hostname outside integrations/.
ALLOWED_HOSTS = {}


def _is_vendor(module):
    return module.split('.')[0] in VENDOR_MODULES or module.startswith(VENDOR_PREFIXES)


def _docstring_nodes(tree):
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, 'body', [])
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                nodes.add(id(body[0].value))
    return nodes


def violations_in(source):
    """`[(kind, detail)]` for one file's source text."""
    tree = ast.parse(source)
    found = []
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_vendor(alias.name):
                    found.append(('import', alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _is_vendor(node.module):
                found.append(('import', node.module))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
            if name in ('import_module', '__import__') and node.args:
                arg = node.args[0]
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and _is_vendor(arg.value)):
                    found.append(('import', arg.value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and VENDOR_HOSTS.search(node.value):
                found.append(('host', node.value[:80]))
    return found


def _python_files():
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            yield path


def _scan():
    offenders, used_allowance = [], set()
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding='utf-8')
        for kind, detail in violations_in(text):
            if kind == 'import' and detail.split('.')[0] in ALLOWED_IMPORTS.get(rel, ()):
                used_allowance.add((rel, detail.split('.')[0]))
                continue
            if kind == 'host' and detail in ALLOWED_HOSTS.get(rel, ()):
                continue
            offenders.append(f'{rel}: {kind} {detail}')
    return offenders, used_allowance


def test_no_vendor_sdk_or_hostname_outside_integrations():
    offenders, _ = _scan()
    assert offenders == [], (
        'Vendor code outside integrations/ breaks "swappable by configuration only". Move it '
        'behind integrations/<capability>/ and call the interface:\n  ' + '\n  '.join(offenders))


def test_the_allowlist_has_no_stale_entries():
    _, used = _scan()
    declared = {(rel, mod) for rel, mods in ALLOWED_IMPORTS.items() for mod in mods}
    assert declared == used, f'allowlisted but no longer needed: {sorted(declared - used)}'


def test_the_scanner_catches_each_kind_of_violation():
    cases = {
        'import boto3': 'import',
        'from botocore.exceptions import ClientError': 'import',
        'import firebase_admin.messaging': 'import',
        'from google.cloud import storage': 'import',
        'import cashfree_pg': 'import',
        'from twilio.rest import Client': 'import',
        'import razorpay': 'import',
        'import stripe': 'import',
        'import msg91': 'import',
        'import sendgrid': 'import',
        'import importlib\nimportlib.import_module("boto3")': 'import',
        '__import__("stripe")': 'import',
        'import requests\nrequests.post("https://api.msg91.com/x")': 'host',
        'URL = "https://api.twilio.com/2010-04-01/x"': 'host',
        'URL = f"https://{e}.cashfree.com/pg"': 'host',
        'URL = "https://sandbox.cashfree.com/pg"': 'host',
        'URL = "https://api.razorpay.com/v1"': 'host',
        'URL = "https://api.stripe.com/v1"': 'host',
        'URL = "https://fcm.googleapis.com/v1/x"': 'host',
        'URL = "https://sns.us-east-1.amazonaws.com/"': 'host',
    }
    for source, kind in cases.items():
        assert any(k == kind for k, _ in violations_in(source)), source


def test_the_scanner_leaves_clean_code_alone():
    clean = (
        'import requests\n'
        'from integrations.sms import msg91\n'
        'from django.conf import settings\n'
        'def f():\n'
        '    """Talks to sns.<region>.amazonaws.com and api.msg91.com (docstring only)."""\n'
        '    return requests.get("https://maps.googleapis.com/maps/api/geocode/json")\n')
    assert violations_in(clean) == []


def test_the_adapters_exist_and_are_where_the_boundary_says():
    """The inverse check: the vendor code is really in integrations/, not deleted."""
    expected = [
        'integrations/email/ses.py', 'integrations/sms/msg91.py', 'integrations/sms/twilio.py',
        'integrations/sms/sns.py', 'integrations/sms/sns_otp.py', 'integrations/push/fcm.py',
        'integrations/push/sns.py', 'integrations/push/fcm_direct.py',
        'integrations/payments/cashfree.py', 'integrations/payments/razorpay.py',
        'integrations/payments/stripe.py', 'integrations/payouts/cashfree.py',
        'integrations/payouts/razorpayx.py', 'integrations/razorpay_http.py', 'integrations/payouts/stripe_connect.py',
        'integrations/storage/s3.py',
    ]
    missing = [p for p in expected if not (ROOT / p).is_file()]
    assert missing == [], missing
