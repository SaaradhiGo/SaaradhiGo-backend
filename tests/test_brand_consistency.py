"""The platform must present exactly one name to customers.

It was presenting two. A rider signing up received an SMS saying "Your OTP for
VahanGo is …", opened an app whose Android label said SaaradhiGo, found a wallet
labelled "VahanGo Credits" (because the *backend* told it to), and — on completing a
ride — got a GST receipt headed "SaaradhiGo / VahanGo" thanking them for riding with
VahanGo. Push notifications about refunds and promo credits said VahanGo too, and the
grievance addresses given to drivers pointed at a domain this platform does not use.

None of that is cosmetic. The OTP SMS is the first thing anyone sees, the receipt is a
tax document, and a grievance address that nobody reads is a compliance problem rather
than a typo.

These tests exist so the retired brand cannot come back, and so customer-facing copy
keeps reading the setting instead of hardcoding a name.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings

RETIRED_BRAND = 'vahango'

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _python_and_template_sources():
    for base in ('servers', 'base', 'templates'):
        root = BACKEND_ROOT / base
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if path.suffix not in {'.py', '.html', '.txt'}:
                continue
            if '__pycache__' in path.parts or 'migrations' in path.parts:
                continue
            yield path


def _code_lines(path):
    """Lines that are not pure comments, so explanatory comments are allowed."""
    for n, raw in enumerate(path.read_text(encoding='utf-8', errors='ignore')
                            .splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith('#'):
            continue
        yield n, raw


def _emittable_strings(path):
    """Every string literal the module could emit, excluding docstrings.

    AST rather than a line scan, because comments AND docstrings legitimately
    discuss the migration -- the earlier line-based version flagged its own
    explanation. What matters is a literal the code can actually send to a customer.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='ignore'))
    except SyntaxError:
        return

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, 'body', None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            yield getattr(node, 'lineno', 0), node.value
        elif isinstance(node, ast.Name):
            yield getattr(node, 'lineno', 0), node.id
        elif isinstance(node, ast.Attribute):
            yield getattr(node, 'lineno', 0), node.attr


def test_the_brand_setting_exists_and_is_not_the_retired_one():
    assert getattr(settings, 'PLATFORM_BRAND_NAME', None), (
        'PLATFORM_BRAND_NAME must exist so customer-facing copy has one source'
    )
    assert RETIRED_BRAND not in settings.PLATFORM_BRAND_NAME.lower()


def test_the_legal_entity_is_configured_separately_from_the_brand():
    """They are different facts. The receipt needs both."""
    assert getattr(settings, 'PLATFORM_LEGAL_ENTITY', None)
    assert RETIRED_BRAND not in settings.PLATFORM_LEGAL_ENTITY.lower()


def test_the_contact_domain_is_not_the_retired_one():
    """Driver grievance and fraud addresses are built from this.

    They pointed at vahango.com, so a driver's complaint went to a domain this
    platform does not use. A human still has to confirm the mailboxes exist.
    """
    domain = getattr(settings, 'PLATFORM_CONTACT_DOMAIN', '')
    assert domain, 'PLATFORM_CONTACT_DOMAIN must be set'
    assert RETIRED_BRAND not in domain.lower()


def test_no_emittable_string_or_identifier_mentions_the_retired_brand():
    """The regression guard.

    Comments and docstrings explaining the migration are fine and useful. A string
    the code could actually send to a customer, or an identifier, is not.
    """
    offenders = []
    for path in _python_and_template_sources():
        rel = path.relative_to(BACKEND_ROOT)
        if path.suffix == '.py':
            for lineno, text in _emittable_strings(path):
                if RETIRED_BRAND in text.lower():
                    offenders.append(f'{rel}:{lineno}: {text[:80]!r}')
        else:
            # Templates have no AST; every line is emittable.
            for n, line in _code_lines(path):
                if RETIRED_BRAND in line.lower():
                    offenders.append(f'{rel}:{n}: {line.strip()[:80]}')

    assert not offenders, (
        'the retired brand appears in emittable source:\n  '
        + '\n  '.join(offenders)
    )


@pytest.mark.django_db
def test_the_otp_message_uses_the_brand_setting():
    """The single most visible string on the platform.

    Every rider and every driver receives it before seeing a screen.
    """
    source = (BACKEND_ROOT / 'servers' / 'auth_user' / 'views.py').read_text(
        encoding='utf-8')
    assert 'Your OTP for {settings.PLATFORM_BRAND_NAME}' in source, (
        'the OTP SMS must interpolate the brand setting rather than hardcode a name'
    )


@pytest.mark.django_db
def test_the_wallet_display_name_comes_from_the_brand_setting():
    """The app asks the server what to call the wallet, so this is the real source."""
    from base.config_view import _brand

    assert _brand() == settings.PLATFORM_BRAND_NAME


@pytest.mark.django_db
def test_the_receipt_html_names_one_brand_and_the_legal_entity():
    from decimal import Decimal

    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from servers.ride.models import Trip, TripStatus

    User = get_user_model()
    rider = User.objects.create_user(phone_number='+919760000001', role='rider')
    st, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider, status_id=st,
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3800000'),
        estimated_fare=Decimal('120.00'), payment_method='cash',
        completed_at=timezone.now(),
    )

    from decimal import Decimal as D

    from servers.ride.receipts import _render_receipt_html

    html = _render_receipt_html(
        trip, rider, 'SG-TEST-1', D('5.00'), D('5.71'), None,
    )

    assert settings.PLATFORM_BRAND_NAME in html
    assert settings.PLATFORM_LEGAL_ENTITY in html
    assert 'VahanGo' not in html, 'the retired brand is still on the receipt'
    # The aggregator statement is a legal claim and must survive the rename.
    assert 'Motor Vehicles Aggregator Guidelines 2020' in html


def test_driver_operational_contacts_use_the_configured_domain():
    """These are the addresses a driver is told to use for grievances and fraud."""
    from servers.driver.services import _contact_domain

    assert _contact_domain() == settings.PLATFORM_CONTACT_DOMAIN
    assert RETIRED_BRAND not in _contact_domain().lower()


def test_no_hardcoded_brand_literal_in_customer_facing_modules():
    """Even the CORRECT name should not be hardcoded where the setting exists.

    Otherwise the next rename repeats this exercise. Checked on the modules that
    actually emit customer copy.
    """
    watched = [
        BACKEND_ROOT / 'servers' / 'ride' / 'receipts.py',
        BACKEND_ROOT / 'servers' / 'rider' / 'credits.py',
        BACKEND_ROOT / 'base' / 'config_view.py',
    ]
    brand = settings.PLATFORM_BRAND_NAME
    offenders = []
    for path in watched:
        for n, line in _code_lines(path):
            # A quoted literal of the brand name, not the settings default itself.
            if re.search(rf"""['"]{re.escape(brand)}['"]""", line) and \
                    'PLATFORM_BRAND_NAME' not in line:
                offenders.append(f'{path.name}:{n}: {line.strip()[:90]}')
    assert not offenders, (
        'customer-facing copy hardcodes the brand instead of reading the setting:\n  '
        + '\n  '.join(offenders)
    )
