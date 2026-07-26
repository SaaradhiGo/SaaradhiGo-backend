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
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
# A simple, table-based receipt that renders identically in most email
# clients (Gmail, Outlook, Apple Mail). Intentionally minimal CSS so
# we do not depend on a styling pipeline.

_RECEIPT_TEMPLATE = """\
<html>
<body style="font-family: Arial, sans-serif; color: #1f2937; max-width: 640px;">
  <h2 style="color: #EEBD2B;">SaaradhiGo / VahanGo</h2>
  <p>Hi {rider_name},</p>
  <p>Thank you for riding with VahanGo. Here is your receipt.</p>

  <table cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #e5e7eb;">
    <tr><td><b>Receipt number</b></td><td>{receipt_number}</td></tr>
    <tr><td><b>Trip ID</b></td><td>#{trip_id}</td></tr>
    <tr><td><b>Date</b></td><td>{date_str}</td></tr>
    <tr><td><b>Pickup</b></td><td>{pickup}</td></tr>
    <tr><td><b>Drop</b></td><td>{drop}</td></tr>
    <tr><td><b>Distance</b></td><td>{distance_km} km</td></tr>
    <tr><td><b>Driver</b></td><td>{driver_name}</td></tr>
    <tr><td><b>Vehicle</b></td><td>{vehicle}</td></tr>
  </table>

  <h3 style="margin-top: 24px;">Fare breakdown</h3>
  <table cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #e5e7eb;">
    <tr><td>Base fare</td><td style="text-align:right;">Rs. {base_fare}</td></tr>
    <tr><td>Distance fare</td><td style="text-align:right;">Rs. {distance_fare}</td></tr>
    <tr><td>Time fare</td><td style="text-align:right;">Rs. {time_fare}</td></tr>
    <tr><td>Surge multiplier</td><td style="text-align:right;">x {surge}</td></tr>
    <tr><td>GST ({gst_rate}%)</td><td style="text-align:right;">Rs. {gst_amount}</td></tr>
    <tr style="background: #f9fafb;"><td><b>Total fare</b></td><td style="text-align:right;"><b>Rs. {total_fare}</b></td></tr>
  </table>

  <p style="margin-top: 16px;"><b>Payment method:</b> {payment_method} &nbsp; <b>Status:</b> {payment_status}</p>

  <p style="margin-top: 24px; color: #6b7280; font-size: 12px;">
    SaaradhiGo Mobility (operating VahanGo) is an aggregator under the
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
        zone = find_zone_for_point(trip.pickup_lat, trip.pickup_long)
        if not zone:
            return None
        vt = None
        if trip.vehicle_id and trip.vehicle_id.vehicle_type_id:
            vt = trip.vehicle_id.vehicle_type_id
        elif trip.requested_vehicle_type:
            vt = trip.requested_vehicle_type
        if not vt:
            return None
        return get_active_rate_card(zone, vt)
    except Exception:  # noqa: BLE001
        logger.exception('rate-card lookup failed for trip=%s', trip.pk)
        return None


def _render_receipt_html(trip, rider, receipt_number, gst_rate, gst_amount, fare_pricing):
    """Build the HTML body. Pure function; no IO."""
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

    total = trip.final_fare or trip.estimated_fare or Decimal('0.00')
    date_str = (trip.completed_at or timezone.now()).strftime('%d %b %Y, %H:%M')

    return _RECEIPT_TEMPLATE.format(
        rider_name=escape(str(rider_name)),
        receipt_number=escape(receipt_number),
        trip_id=trip.id,
        date_str=escape(date_str),
        pickup=escape(str(trip.pickup_address or '—')),
        drop=escape(str(trip.destination_address or '—')),
        distance_km=str(trip.actual_distance_km or trip.estimated_distance_km or '—'),
        driver_name=escape(str(driver_name)),
        vehicle=escape(str(vehicle or '—')),
        base_fare=str(round(base_fare, 2)),
        distance_fare=str(round(distance_fare, 2)),
        time_fare=str(round(time_fare, 2)),
        surge=str(round(surge, 2)),
        gst_rate=str(gst_rate),
        gst_amount=str(round(gst_amount, 2)),
        total_fare=str(round(total, 2)),
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
        title=f'VahanGo receipt {receipt.receipt_number}',
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
    story.append(Paragraph('VahanGo / SaaradhiGo Mobility', h1))
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

    fare_rows = [
        ['Description', 'Amount (Rs.)'],
        ['Base fare', f'{base_fare}'],
        ['Distance fare', f'{distance_fare}'],
        ['Time fare', f'{time_fare}'],
        ['Surge multiplier', f'x {surge}'],
        ['GST', f'{receipt.gst_amount}'],
        ['Total fare', f'{receipt.total_fare}'],
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
        'SaaradhiGo Mobility (operating VahanGo) is an aggregator under the '
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
    """Send via configured email backend. Returns (sent_ok, error_str).

    Attempts to attach the PDF blob as a file attachment; falls back to
    HTML-only if PDF generation fails."""
    to_addr = receipt.sent_to_email
    if not to_addr:
        return False, 'no_email'
    try:
        msg = EmailMultiAlternatives(
            subject=f'Your VahanGo receipt {receipt.receipt_number}',
            body='Your VahanGo trip receipt is enclosed (HTML + PDF).',
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@vahango.in'),
            to=[to_addr],
        )
        msg.attach_alternative(receipt.html_body, 'text/html')
        # Attach the PDF if we managed to render one.
        try:
            if receipt.pdf_file:
                receipt.pdf_file.open('rb')
                msg.attach(
                    f'{receipt.receipt_number}.pdf',
                    receipt.pdf_file.read(),
                    'application/pdf',
                )
                receipt.pdf_file.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning('attach pdf failed for receipt=%s: %s', receipt.id, exc)
        msg.send(fail_silently=False)
        return True, ''
    except Exception as exc:  # noqa: BLE001
        logger.warning('receipt email failed for receipt=%s: %s', receipt.id, exc)
        return False, str(exc)[:512]


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

    card = _get_active_rate_card_for_trip(trip)
    gst_rate = card.gst_percent if card else Decimal('5.00')

    fare_pricing = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    total = trip.final_fare or trip.estimated_fare or Decimal('0.00')
    # Inclusive GST extraction: gst = total * gst_rate / (100 + gst_rate)
    gst_amount = (Decimal(str(total)) * Decimal(str(gst_rate)) /
                  (Decimal('100.00') + Decimal(str(gst_rate)))).quantize(Decimal('0.01'))

    receipt_number = f"SG-{(trip.completed_at or timezone.now()).strftime('%Y%m')}-{trip.id}-v1"

    with transaction.atomic():
        receipt = Receipt.objects.create(
            trip_id=trip,
            user_id=rider,
            receipt_number=receipt_number,
            total_fare=total,
            gst_amount=gst_amount,
            payment_method=trip.payment_method or '',
            payment_status=trip.payment_status or '',
            sent_to_email=rider.email or '',
            html_body=_render_receipt_html(
                trip=trip, rider=rider, receipt_number=receipt_number,
                gst_rate=gst_rate, gst_amount=gst_amount, fare_pricing=fare_pricing,
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
    card = _get_active_rate_card_for_trip(trip)
    gst_rate = card.gst_percent if card else Decimal('5.00')
    from servers.ride.models import FarePricing
    fare_pricing = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    total = trip.final_fare or trip.estimated_fare or Decimal('0.00')
    gst_amount = (Decimal(str(total)) * Decimal(str(gst_rate)) /
                  (Decimal('100.00') + Decimal(str(gst_rate)))).quantize(Decimal('0.01'))
    receipt_number = f"SG-{(trip.completed_at or timezone.now()).strftime('%Y%m')}-{trip.id}-v{new_version}"

    receipt = Receipt.objects.create(
        trip_id=trip, user_id=rider, receipt_number=receipt_number,
        total_fare=total, gst_amount=gst_amount,
        payment_method=trip.payment_method or '',
        payment_status=trip.payment_status or '',
        sent_to_email=rider.email or '',
        html_body=_render_receipt_html(
            trip=trip, rider=rider, receipt_number=receipt_number,
            gst_rate=gst_rate, gst_amount=gst_amount, fare_pricing=fare_pricing,
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
