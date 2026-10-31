"""Trip receipt generation + email dispatch.

A receipt is generated when a trip transitions to `completed`. Rule:

  * receipt_number is unique and stable: `SG-<YYYY>-<trip_id>-v<n>`
  * html_body snapshots the rendered email at issue-time so a re-send
    months later sends the exact same content (auditable).
  * GST is captured from the trip's effective RateCard at issue time
    (snapshot); future RateCard changes do not retroactively alter
    historical receipts.
  * Email is best-effort; if SES is down, the receipt row still gets
    saved with a `send_failure_reason` so support can resend.

Section 31 of the CGST Act 2017 requires us to issue an invoice for
every taxable supply. This implementation produces one. The PDF
flavour is a Phase-1 follow-up; HTML email is sufficient for Phase-0
and saves us a wkhtmltopdf / weasyprint dependency.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from django.conf import settings
from base.auth_email import send_message
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape
from base.countries import get_country
from base.money import (
    DEFAULT_CURRENCY, currency_for_trip, format_money, quantize,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
# A simple, table-based receipt that renders identically in most email
# clients (Gmail, Outlook, Apple Mail). Intentionally minimal CSS so
# we do not depend on a styling pipeline.

def _brand():
    """The customer-facing product name.

    Read from settings rather than hardcoded. This file used to name two different
    brands in the same document: the HTML heading said "SaaradhiGo / VahanGo" and the
    body thanked the rider for riding with VahanGo.

    A helper rather than an inline getattr so the value can be interpolated without
    nesting quotes inside an f-string.
    """
    return getattr(settings, 'PLATFORM_BRAND_NAME', 'SaaradhiGo')


def _legal_entity():
    """The registered entity, which is not the same thing as the brand.

    Appears on the GST receipt beside the MVA-2020 aggregator statement. The name
    itself is unchanged from what was already shipping -- only the "(operating
    VahanGo)" parenthetical was removed. Whether this is the correct registered name,
    and whether a GSTIN and registered address must also appear, is a question for the
    business and its accountants.
    """
    return getattr(settings, 'PLATFORM_LEGAL_ENTITY', 'SaaradhiGo Mobility')


def _total_fare(trip):
    """What the receipt charges. A platform-funded operator end charged 0, which the usual
    `final_fare or estimated_fare` would read as "no final fare" and show the quote."""
    if getattr(trip, 'end_rule', '') == 'platform_funded':
        return Decimal('0.00')
    return trip.final_fare or trip.estimated_fare or Decimal('0.00')


def promo_lines(trip):
    """`[(label, discount, charged)]` for a trip that held a promo, else []. The receipt's total
    stays the FULL fare (that is what the driver's earnings are computed on); the discount is
    platform-funded and the rider is charged the fare less it (module 12)."""
    try:
        from servers.promotions import rider_promos
        from servers.ride.models import PromoRedemption

        discount = rider_promos.discount_minor(trip)
        if not discount:
            return []
        redemption = (PromoRedemption.objects.filter(trip=trip).select_related('promo')
                      .order_by('-id').first())
        label = f'Promo discount ({redemption.promo.code})' if redemption else 'Promo discount'
        return [(label, rider_promos.dec(discount, trip.currency),
                 rider_promos.payable(trip))]
    except Exception:  # noqa: BLE001 - a receipt must still render
        logger.exception('promo receipt lines failed for trip=%s', getattr(trip, 'pk', None))
        return []


def operator_end_note(trip):
    """The extra receipt line for a trip an operator ended, or ''."""
    from servers.ride.operator_end import PLATFORM_FUNDED, RECEIPT_NOT_CHARGED_LINE

    rule = getattr(trip, 'end_rule', '')
    if rule == PLATFORM_FUNDED:
        return RECEIPT_NOT_CHARGED_LINE
    if rule:
        return 'Ended by SaaradhiGo support: the fare is for the distance driven.'
    return ''


_RECEIPT_TEMPLATE = """\
<html>
<body style="font-family: Arial, sans-serif; color: #1f2937; max-width: 640px;">
  <h2 style="color: #EEBD2B;">{brand}</h2>
  <p>Hi {rider_name},</p>
  <p>Thank you for riding with {brand}. Here is your receipt.</p>

  <table cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #e5e7eb;">
    <tr><td><b>Receipt number</b></td><td>{receipt_number}</td></tr>
    <tr><td><b>Trip ID</b></td><td>#{trip_id}</td></tr>
    <tr><td><b>Date</b></td><td>{date_str}</td></tr>
    <tr><td><b>Pickup</b></td><td>{pickup}</td></tr>
    <tr><td><b>Drop</b></td><td>{drop}</td></tr>
    <tr><td><b>Distance</b></td><td>{distance_km}</td></tr>
    <tr><td><b>Driver</b></td><td>{driver_name}</td></tr>
    <tr><td><b>Vehicle</b></td><td>{vehicle}</td></tr>
  </table>

  <h3 style="margin-top: 24px;">Fare breakdown</h3>
  <table cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #e5e7eb;">
    <tr><td>Base fare</td><td style="text-align:right;">{base_fare}</td></tr>
    <tr><td>Distance fare</td><td style="text-align:right;">{distance_fare}</td></tr>
    <tr><td>Time fare</td><td style="text-align:right;">{time_fare}</td></tr>
    <tr><td>Surge multiplier</td><td style="text-align:right;">x {surge}</td></tr>
    <tr><td>{tax_label} ({gst_rate}%)</td><td style="text-align:right;">{gst_amount}</td></tr>
    {note_row}
    <tr style="background: #f9fafb;"><td><b>Total fare</b></td><td style="text-align:right;"><b>{total_fare}</b></td></tr>
    {promo_rows}
  </table>

  <p style="margin-top: 16px;"><b>Payment method:</b> {payment_method} &nbsp; <b>Status:</b> {payment_status}</p>

  <p style="margin-top: 24px; color: #6b7280; font-size: 12px;">
    {legal_entity} is an aggregator under the
    Motor Vehicles Aggregator Guidelines 2020 issued by the Ministry of
    Road Transport &amp; Highways, Government of India. For any dispute
    about this fare, please contact support within 30 days quoting the
    Receipt number above.
  </p>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _get_active_rate_card_for_trip(trip):
    """Best-effort rate card lookup; returns None when no zone or card
    matches (legacy trips before the multi-city work). Caller should
    fall back to defaults in that case."""
    try:
        from servers.pricing.services import find_zone_for_point, get_active_rate_card

        # Prefer the zone STAMPED ON THE TRIP over re-deriving it from the
        # pickup coordinates.
        #
        # The trip records the zone its pickup fell in, for exactly this reason:
        # re-running a point-in-polygon lookup against today's zone table means a
        # re-parented zone, a changed boundary or a deactivated zone silently
        # changes the tax rate on a receipt for a ride that happened months ago.
        # A receipt is a legal document; the rate it shows must be the rate that
        # was charged, which is a fact about the trip and not a lookup.
        #
        # Same reasoning as the already-stored `zone_code` and
        # `rate_card_version`. The coordinate lookup stays as a fallback for
        # legacy trips written before the zone column existed.
        zone = getattr(trip, 'zone', None)
        if zone is None:
            zone = find_zone_for_point(trip.pickup_lat, trip.pickup_long)
        if not zone:
            return None
        vt = None
        if trip.vehicle_id and trip.vehicle_id.vehicle_type_id:
            vt = trip.vehicle_id.vehicle_type_id
        elif trip.requested_vehicle_type:
            vt = trip.requested_vehicle_type
        if vt:
            card = get_active_rate_card(zone, vt)
            if card is not None:
                return card

        # No vehicle type, or no card for it. Fall back to ANY active card in
        # this zone, because the only thing the caller wants from the card is
        # the TAX RATE, and tax is a property of the jurisdiction rather than of
        # the vehicle. Returning None here instead sent the caller to a
        # hardcoded 5.00 -- India's GST rate -- which a multi-country drill
        # caught being applied to a US trip, taxing a $27 fare at 5% instead of
        # the zone's 8.625%.
        from servers.pricing.models import RateCard
        return (RateCard.objects
                .filter(zone=zone, is_active=True)
                .order_by('-version')
                .first())
    except Exception:  # noqa: BLE001
        logger.exception('rate-card lookup failed for trip=%s', trip.pk)
        return None


# Kilometres are what we store; what the rider reads depends on their market.
KM_PER_MILE = Decimal('1.609344')


def format_distance(km, unit='km'):
    """Render a stored metric distance in the zone's unit.

    Storage stays metric everywhere -- converting at write time would make two
    trips in different markets incomparable and break every distance aggregate.
    Conversion happens here, at the edge, for display only.
    """
    if km in (None, ''):
        return '—'
    value = Decimal(str(km))
    if unit == 'mi':
        return f'{(value / KM_PER_MILE).quantize(Decimal("0.01"))} mi'
    return f'{value.quantize(Decimal("0.01"))} km'


def _format_distance(trip):
    """The distance line on a receipt, in the trip's zone's unit."""
    km = trip.actual_distance_km or trip.estimated_distance_km
    zone = getattr(trip, 'zone', None)
    unit = (getattr(zone, 'distance_unit', '') or
            get_country(getattr(zone, 'country', None)).distance_unit)
    return format_distance(km, unit)


def _render_receipt_html(trip, rider, receipt_number, gst_rate, gst_amount,
                         fare_pricing, currency=None, tax_label=None,
                         tax_mode='inclusive'):
    """Build the HTML body. Pure function; no IO.

    `currency` and `tax_label` are parameters rather than constants because this
    document used to say "Rs." and "GST" whatever market the trip was in. A US
    rider would have received a dollar fare labelled in rupees with an Indian tax
    name on it.
    """
    from base.money import DEFAULT_CURRENCY, format_money
    currency = currency or DEFAULT_CURRENCY
    tax_label = tax_label or 'Tax'
    rider_name = (getattr(rider, 'full_name', '') or rider.phone_number) or 'Rider'
    driver_name = ''
    vehicle = ''
    if trip.driver_id and trip.driver_id.user_id:
        driver_name = (
            trip.driver_id.user_id.full_name or trip.driver_id.user_id.phone_number or ''
        )
    if trip.vehicle_id:
        v = trip.vehicle_id
        vehicle = ' '.join(filter(None, [str(v.brand or ''), str(v.model or ''), f'({v.vehicle_number})' if v.vehicle_number else '']))

    surge = getattr(trip, 'surge_multiplier', None) or Decimal('1.00')
    base_fare = Decimal('0.00')
    distance_fare = Decimal('0.00')
    time_fare = Decimal('0.00')
    if fare_pricing is not None:
        base_fare = fare_pricing.base_fare or Decimal('0.00')
        distance_fare = fare_pricing.distance_fare or Decimal('0.00')
        time_fare = fare_pricing.time_fare or Decimal('0.00')
        surge = fare_pricing.surge_multiplier or surge
    if getattr(trip, 'end_rule', '') == 'platform_funded':
        base_fare = distance_fare = time_fare = Decimal('0.00')

    total = _total_fare(trip)
    date_str = (trip.completed_at or timezone.now()).strftime('%d %b %Y, %H:%M')

    return _RECEIPT_TEMPLATE.format(
        # Read from settings, never hardcoded: this document used to name two
        # different brands in the same page.
        brand=escape(str(getattr(settings, 'PLATFORM_BRAND_NAME', 'SaaradhiGo'))),
        legal_entity=escape(str(
            getattr(settings, 'PLATFORM_LEGAL_ENTITY', 'SaaradhiGo Mobility'))),
        rider_name=escape(str(rider_name)),
        receipt_number=escape(receipt_number),
        trip_id=trip.id,
        date_str=escape(date_str),
        pickup=escape(str(trip.pickup_address or '—')),
        drop=escape(str(trip.destination_address or '—')),
        distance_km=escape(_format_distance(trip)),
        driver_name=escape(str(driver_name)),
        vehicle=escape(str(vehicle or '—')),
        base_fare=format_money(base_fare, currency),
        distance_fare=format_money(distance_fare, currency),
        time_fare=format_money(time_fare, currency),
        surge=str(round(surge, 2)),
        gst_rate=str(gst_rate),
        gst_amount=format_money(gst_amount, currency),
        total_fare=format_money(total, currency),
        note_row=(f'<tr><td colspan="2"><b>{escape(operator_end_note(trip))}</b></td></tr>'
                  if operator_end_note(trip) else ''),
        promo_rows=''.join(
            f'<tr><td>{escape(label)}</td><td style="text-align:right;">'
            f'-{format_money(discount, currency)}</td></tr>'
            f'<tr style="background: #f9fafb;"><td><b>Amount charged</b></td>'
            f'<td style="text-align:right;"><b>{format_money(charged, currency)}</b></td></tr>'
            for label, discount, charged in promo_lines(trip)),
        tax_label=escape(str(tax_label)),
        payment_method=escape(str(trip.payment_method or '—')),
        payment_status=escape(str(trip.payment_status or '—')),
    )


def _build_receipt_pdf(receipt) -> Optional[bytes]:
    """Render a PDF blob for a Receipt using ReportLab.

    Pure-Python (no Cairo / Pango / wkhtmltopdf on the deploy host).
    Returns the PDF as bytes, or None if ReportLab is unavailable
    (the receipt still gets emailed as HTML in that case).
    """
    try:
        from io import BytesIO
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        )
    except ImportError:  # noqa: BLE001
        logger.warning('reportlab not installed; PDF skipped for receipt=%s', receipt.id)
        return None

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f'{_brand()} receipt {receipt.receipt_number}',
        author='SaaradhiGo Mobility',
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('h1', parent=styles['Heading1'], textColor=colors.HexColor('#C59A1D'))
    h2 = styles['Heading3']
    body = styles['BodyText']
    small = ParagraphStyle('small', parent=styles['BodyText'], fontSize=8, textColor=colors.grey)

    trip = receipt.trip_id
    rider = receipt.user_id
    rider_name = (getattr(rider, 'full_name', '') or rider.phone_number or 'Rider')
    driver_name = ''
    vehicle = ''
    if trip.driver_id and trip.driver_id.user_id:
        driver_name = trip.driver_id.user_id.full_name or trip.driver_id.user_id.phone_number or ''
    if trip.vehicle_id:
        v = trip.vehicle_id
        vehicle = f"{v.brand or ''} {v.model or ''} ({v.vehicle_number or ''})".strip()

    story = []
    story.append(Paragraph(f'{_brand()} / {_legal_entity()}', h1))
    story.append(Paragraph('Tax invoice / Trip receipt', h2))
    story.append(Spacer(1, 6))

    meta_rows = [
        ['Receipt number', receipt.receipt_number],
        ['Issued to', rider_name],
        ['Trip ID', f'#{trip.id}'],
        ['Date', (trip.completed_at or receipt.issued_at).strftime('%d %b %Y, %H:%M')],
        ['Pickup', trip.pickup_address or '—'],
        ['Drop', trip.destination_address or '—'],
        ['Distance', f"{trip.actual_distance_km or trip.estimated_distance_km or '—'} km"],
        ['Driver', driver_name or '—'],
        ['Vehicle', vehicle or '—'],
    ]
    meta_table = Table(meta_rows, colWidths=[55 * mm, 110 * mm])
    meta_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F9FAFB')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 12))

    # Fare breakdown table -- pulled from the FarePricing snapshot if
    # present, falling back to the trip's totals.
    from servers.ride.models import FarePricing
    fp = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    base_fare = (fp.base_fare if fp else None) or 0
    distance_fare = (fp.distance_fare if fp else None) or 0
    time_fare = (fp.time_fare if fp else None) or 0
    surge = (fp.surge_multiplier if fp else None) or 1
    if getattr(trip, 'end_rule', '') == 'platform_funded':
        base_fare = distance_fare = time_fare = 0

    # Currency and tax label come from the receipt row and its trip's zone, not
    # from a constant. This table said "Amount (Rs.)" and "GST" for every market.
    _currency = getattr(receipt, 'currency', None) or DEFAULT_CURRENCY
    _zone = getattr(getattr(receipt, 'trip_id', None), 'zone', None)
    _tax_label = get_country(getattr(_zone, 'country', None)).tax_label

    fare_rows = [
        ['Description', f'Amount ({_currency})'],
        ['Base fare', format_money(base_fare, _currency, with_symbol=False)],
        ['Distance fare', format_money(distance_fare, _currency, with_symbol=False)],
        ['Time fare', format_money(time_fare, _currency, with_symbol=False)],
        ['Surge multiplier', f'x {surge}'],
        [_tax_label, format_money(receipt.gst_amount, _currency, with_symbol=False)],
        *([[operator_end_note(trip), '']] if operator_end_note(trip) else []),
        ['Total fare', format_money(receipt.total_fare, _currency, with_symbol=False)],
        *[row for label, discount, charged in promo_lines(trip) for row in (
            [label, '-' + format_money(discount, _currency, with_symbol=False)],
            ['Amount charged', format_money(charged, _currency, with_symbol=False)])],
    ]
    fare_table = Table(fare_rows, colWidths=[110 * mm, 55 * mm])
    fare_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#EEBD2B')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#F9FAFB')),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(fare_table)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f'<b>Payment method:</b> {receipt.payment_method or "—"} '
        f'&nbsp;&nbsp;&nbsp;<b>Status:</b> {receipt.payment_status or "—"}',
        body,
    ))
    story.append(Spacer(1, 16))
    story.append(Paragraph(
        f'{_legal_entity()} is an aggregator under the '
        'Motor Vehicles Aggregator Guidelines 2020 issued by the Ministry of '
        'Road Transport &amp; Highways, Government of India. For any dispute '
        'about this fare, please contact support within 30 days quoting the '
        'Receipt number above.',
        small,
    ))

    try:
        doc.build(story)
    except Exception as exc:  # noqa: BLE001
        logger.exception('reportlab PDF build failed for receipt=%s: %s', receipt.id, exc)
        return None
    return buf.getvalue()


def _attach_pdf_to_receipt(receipt) -> bool:
    """Generate + save the PDF blob onto receipt.pdf_file. Returns True
    if a PDF was successfully attached, False otherwise (also False
    when reportlab is missing, so the caller knows to skip the
    attachment in the email)."""
    if receipt.pdf_file:
        return True
    pdf_bytes = _build_receipt_pdf(receipt)
    if not pdf_bytes:
        return False
    try:
        from django.core.files.base import ContentFile
        receipt.pdf_file.save(
            f'{receipt.receipt_number}.pdf',
            ContentFile(pdf_bytes),
            save=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning('failed to persist PDF blob for receipt=%s: %s', receipt.id, exc)
        return False


def _send_receipt_email(receipt):
    """Send via the one outbound gate. Returns (sent_ok, error_str).

    WHY THIS NO LONGER BUILDS ITS OWN MESSAGE
    -----------------------------------------
    This used to construct an EmailMultiAlternatives and call send() directly.
    It therefore answered to neither of the two safeguards the auth emails go
    through:

      * `delivery_is_configured()` -- so on a deployment with no mail
        configuration it attempted a send to localhost:25, caught the
        exception, and wrote the error into Receipt.send_failure_reason where
        nobody was watching. That is the silent failure this column is full
        of.
      * `AUTH_EMAIL_ALLOWLIST` -- so "an empty QA allowlist blocks all
        outbound email" was false for the one message type that goes to
        customers. QA pointed at real SES, with a real address in a restored
        dump, would have emailed that person a GST receipt for a test trip.

    The subject, body, HTML and PDF attachment are unchanged. Only the way
    out is.
    """
    to_addr = receipt.sent_to_email
    if not to_addr:
        return False, 'no_email'

    attachments = []
    try:
        if receipt.pdf_file:
            receipt.pdf_file.open('rb')
            attachments.append((
                f'{receipt.receipt_number}.pdf',
                receipt.pdf_file.read(),
                'application/pdf',
            ))
            receipt.pdf_file.close()
    except Exception as exc:  # noqa: BLE001
        # HTML-only is a usable receipt; no PDF is not a reason to send none.
        logger.warning('attach pdf failed for receipt=%s: %s', receipt.id, exc)

    sent, reason = send_message(
        to=to_addr,
        subject=f'Your {_brand()} receipt {receipt.receipt_number}',
        body=f'Your {_brand()} trip receipt is enclosed (HTML + PDF).',
        html=receipt.html_body,
        attachments=attachments,
    )
    if not sent:
        logger.warning(
            'receipt email not sent for receipt=%s: %s', receipt.id, reason,
        )
    return sent, reason[:512]


def _resolve_tax_rate(trip):
    """The tax rate this trip's receipt must state, for its market.

    The rate comes from the zone's active rate card. When there is none the
    fallback comes from the country registry -- NOT from a literal 5.00, which
    is India's GST rate on taxi services and was silently applied to every
    market. The US default is deliberately 0.00: there is no national US rate
    to guess at, and guessing one onto a legal document is worse than stating
    zero and logging the misconfiguration.
    """
    card = _get_active_rate_card_for_trip(trip)
    if card is not None:
        return Decimal(str(card.gst_percent or '0'))

    country = get_country(getattr(getattr(trip, 'zone', None), 'country', None))
    rate = Decimal(country.default_tax_percent)
    logger.error(
        'no rate card for trip=%s zone=%s; taxing at the %s default of %s%%. '
        'This is a pricing misconfiguration -- the receipt is a legal '
        'document and this rate is a guess.',
        trip.pk, getattr(getattr(trip, 'zone', None), 'code', None),
        country.code, rate,
    )
    return rate


def _receipt_tax(trip, total, rate):
    """Tax, currency and label for one receipt, in this market's convention.

    This used to be inclusive extraction done unconditionally:

        gst = total * rate / (100 + rate)

    which is right for India, where GST is levied on the fare the rider is
    quoted so the quoted total already contains it. Applied to a US trip it
    carves a notional tax out of a total that never had any tax in it, and so
    both understates the fare and misstates the tax on the document the rider
    keeps.

    `inclusive`  the tax is already inside `total`; extract it for display and
                 leave `total` exactly as quoted. India.
    `additive`   `quote_fare` added the tax on top, so it is already part of
                 `total` as a real charge. Recompute it the way the quote did
                 -- on the pre-tax fare -- so the receipt's lines reconcile
                 with what was charged instead of re-deriving a different
                 number from the post-tax total.

    One function because there are two receipt-issuing paths, and the second
    one -- `reissue_receipt`, used after a fare adjustment -- carried a copy of
    the old India-only arithmetic. A reissued US receipt stated a tax that was
    never charged, at India's rate, in no stated currency. Two copies of tax
    law is one copy too many.
    """
    zone = getattr(trip, 'zone', None)
    mode = (getattr(zone, 'tax_mode', '') or 'inclusive')
    rate = Decimal(str(rate or '0'))
    total = Decimal(str(total))
    currency = currency_for_trip(trip)

    if mode == 'additive':
        per_trip_fee = Decimal(str(getattr(zone, 'per_trip_fee', None) or '0'))
        # total = fare + tax + fee, and tax = fare * rate/100, so
        # fare = (total - fee) / (1 + rate/100).
        taxable = total - per_trip_fee
        divisor = (Decimal('1') + rate / Decimal('100'))
        pre_tax = (taxable / divisor) if divisor else taxable
        amount = quantize(pre_tax * rate / Decimal('100'), currency)
    else:
        amount = quantize(total * rate / (Decimal('100.00') + rate), currency)

    return {
        'amount': amount,
        'mode': mode,
        'label': get_country(getattr(zone, 'country', None)).tax_label,
        'currency': currency,
    }


def issue_receipt(trip, force_resend: bool = False):
    """Generate (and email) a Receipt for a completed trip.

    Idempotent: if a Receipt already exists for the trip and
    `force_resend` is False, returns the existing Receipt without
    creating a new row. With `force_resend=True`, re-sends the existing
    Receipt (does NOT create a new version).

    To create a NEW version (e.g. after a fare adjustment), call
    `reissue_receipt(trip)` instead.
    """
    from servers.ride.models import FarePricing, Receipt

    rider = trip.user_id
    if not rider:
        return None

    existing = Receipt.objects.filter(trip_id=trip).order_by('-version', '-id').first()
    if existing and not force_resend:
        return existing
    if existing and force_resend:
        ok, err = _send_receipt_email(existing)
        existing.last_sent_at = timezone.now() if ok else existing.last_sent_at
        existing.send_failure_reason = err
        existing.save(update_fields=['last_sent_at', 'send_failure_reason'])
        return existing

    gst_rate = _resolve_tax_rate(trip)
    fare_pricing = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    total = _total_fare(trip)

    tax = _receipt_tax(trip, total, gst_rate)
    gst_amount, tax_mode = tax['amount'], tax['mode']
    tax_label, currency = tax['label'], tax['currency']

    receipt_number = f"SG-{(trip.completed_at or timezone.now()).strftime('%Y%m')}-{trip.id}-v1"

    with transaction.atomic():
        receipt = Receipt.objects.create(
            trip_id=trip,
            user_id=rider,
            receipt_number=receipt_number,
            total_fare=total,
            gst_amount=gst_amount,
            currency=currency,
            payment_method=trip.payment_method or '',
            payment_status=trip.payment_status or '',
            sent_to_email=rider.email or '',
            html_body=_render_receipt_html(
                trip=trip, rider=rider, receipt_number=receipt_number,
                gst_rate=gst_rate, gst_amount=gst_amount, fare_pricing=fare_pricing,
                currency=currency, tax_label=tax_label, tax_mode=tax_mode,
            ),
            version=1,
        )

    # Build + attach the PDF blob BEFORE we email so we can include it
    # as an attachment. Failure to render PDF does not block the email.
    _attach_pdf_to_receipt(receipt)

    ok, err = _send_receipt_email(receipt)
    if ok:
        receipt.last_sent_at = timezone.now()
    receipt.send_failure_reason = err
    receipt.save(update_fields=['last_sent_at', 'send_failure_reason'])
    logger.info('Receipt issued trip=%s number=%s sent=%s', trip.id, receipt_number, ok)
    return receipt


def reissue_receipt(trip):
    """Issue a NEW receipt version (e.g. after a fare adjustment)."""
    from servers.ride.models import Receipt
    latest = Receipt.objects.filter(trip_id=trip).order_by('-version').first()
    new_version = (latest.version + 1) if latest else 1
    # Bypass the idempotency check by inlining the body; we want a new row.
    rider = trip.user_id
    if not rider:
        return None
    gst_rate = _resolve_tax_rate(trip)
    from servers.ride.models import FarePricing
    fare_pricing = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    total = _total_fare(trip)
    tax = _receipt_tax(trip, total, gst_rate)
    gst_amount = tax['amount']
    receipt_number = f"SG-{(trip.completed_at or timezone.now()).strftime('%Y%m')}-{trip.id}-v{new_version}"

    receipt = Receipt.objects.create(
        trip_id=trip, user_id=rider, receipt_number=receipt_number,
        total_fare=total, gst_amount=gst_amount,
        currency=tax['currency'],
        payment_method=trip.payment_method or '',
        payment_status=trip.payment_status or '',
        sent_to_email=rider.email or '',
        html_body=_render_receipt_html(
            trip=trip, rider=rider, receipt_number=receipt_number,
            gst_rate=gst_rate, gst_amount=gst_amount, fare_pricing=fare_pricing,
            currency=tax['currency'], tax_label=tax['label'],
            tax_mode=tax['mode'],
        ),
        version=new_version,
    )
    _attach_pdf_to_receipt(receipt)
    ok, err = _send_receipt_email(receipt)
    if ok:
        receipt.last_sent_at = timezone.now()
    receipt.send_failure_reason = err
    receipt.save(update_fields=['last_sent_at', 'send_failure_reason'])
    return receipt
