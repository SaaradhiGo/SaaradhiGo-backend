"""Earnings for any period since a driver joined (10 Nov v2), read from the settlement ledger.

Every figure comes from the same rows as /driver/earnings/period/: settled trips
(`earnings.settlement_rows`), the daily platform fee rows (`purpose='platform_fee'`), and payouts
paid (`TransactionHistory` against a withdrawal). So

    fares - platform fee + tips == you earned        (platform fee = commission + daily fee)
    ledger movement in the period == you earned - cash you collected

and the monthly statement (View, CSV, PDF) is this summary plus its trip and payout lines, with
the opening and closing balance from `payments.ledger.build_statement`. Tips do not exist (D5):
they are reported as zero with `tips_enabled: false`. Read only.
"""
import csv
import io
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import BigIntegerField, Sum, Value
from django.db.models.functions import Coalesce

logger = logging.getLogger(__name__)

PERIODS = ('day', 'week', 'month', 'year', 'taxyear', 'custom')
MAX_CUSTOM_DAYS = 3 * 366
ZERO = Decimal('0.00')


class BadPeriod(ValueError):
    pass


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

def _month_end(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def bounds(period, anchor, start=None, end=None):
    """First and last local date of the period holding `anchor`. The Indian tax year runs
    1 April to 31 March."""
    if period == 'day':
        return anchor, anchor
    if period == 'week':
        first = anchor - timedelta(days=anchor.weekday())
        return first, first + timedelta(days=6)
    if period == 'month':
        first = anchor.replace(day=1)
        return first, _month_end(first)
    if period == 'year':
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    if period == 'taxyear':
        y = anchor.year if anchor.month >= 4 else anchor.year - 1
        return date(y, 4, 1), date(y + 1, 3, 31)
    if period == 'custom':
        if start is None or end is None or end < start:
            raise BadPeriod('Choose a start date on or before the end date.')
        if (end - start).days > MAX_CUSTOM_DAYS:
            raise BadPeriod('Choose a range of at most three years.')
        return start, end
    raise BadPeriod('period must be day, week, month, year, taxyear or custom')


def shift(period, first, last, direction):
    """The anchor of the previous (-1) or next (+1) period."""
    if period == 'day':
        return first + timedelta(days=direction)
    if period == 'week':
        return first + timedelta(days=7 * direction)
    if period == 'month':
        return (first - timedelta(days=1)).replace(day=1) if direction < 0 else last + timedelta(days=1)
    if period in ('year', 'taxyear'):
        return first.replace(year=first.year + direction)
    span = (last - first).days + 1
    return first + timedelta(days=span * direction)


def _start(tz, d):
    return datetime.combine(d, time.min, tzinfo=tz)


def chart_unit(period, first, last):
    if period == 'day':
        return 'hour'
    if period in ('year', 'taxyear') or (last - first).days > 62:
        return 'month'
    return 'day'


def _bucket(unit, local_dt):
    if unit == 'hour':
        return f'{local_dt.hour:02d}'
    if unit == 'month':
        return local_dt.strftime('%Y-%m')
    return local_dt.date().isoformat()


def _bucket_keys(unit, first, last):
    if unit == 'hour':
        return [f'{h:02d}' for h in range(24)]
    keys, d = [], first
    while d <= last:
        key = d.strftime('%Y-%m') if unit == 'month' else d.isoformat()
        if key not in keys:
            keys.append(key)
        d += timedelta(days=1)
    return keys


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------

def _fee_rows(driver, since=None, until=None):
    from servers.payments.ledger import counted
    from servers.pricing.fee_plans import PURPOSE
    from servers.rider.models import WalletTransaction

    # A refund for a trip is on that trip's row (`settlement_rows`), on the trip's day; not here.
    qs = counted(WalletTransaction.objects.filter(user_id=driver.user_id_id, purpose=PURPOSE))
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    if until is not None:
        qs = qs.filter(created_at__lt=until)
    return list(qs.order_by('created_at', 'id'))


def _payout_rows(driver, since=None, until=None):
    from servers.payments.models import TransactionHistory

    qs = TransactionHistory.objects.filter(driver_id=driver, withdrawal_request__isnull=False, status='completed')
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    if until is not None:
        qs = qs.filter(created_at__lt=until)
    return list(qs.order_by('created_at', 'id'))


def _fee_amount(row):
    from base.money import from_minor

    amount = from_minor(row.amount_minor, row.currency)
    return amount if row.txn_type == 'debit' else -amount


def _payout_amount(row):
    from base.money import from_minor

    return from_minor(row.amount_minor, row.currency)


class _Data:
    """Settled trips, platform fee rows and payouts for `[since, until)`, loaded once."""

    def __init__(self, driver, since=None, until=None):
        from .earnings import settlement_rows

        self.trips = list(reversed(settlement_rows(driver, since=since, until=until)))
        self.fees = _fee_rows(driver, since, until)
        self.payouts = _payout_rows(driver, since, until)


def summarise_period(trips, fees, payouts, tz):
    from .earnings import summarise

    t = summarise(trips)
    daily_fee = sum((_fee_amount(r) for r in fees), ZERO)
    commission = t['total_commission']
    paid = sum((_payout_amount(r) for r in payouts), ZERO)
    days = {r.created_at.astimezone(tz).date() for r in trips if r.created_at is not None}
    trips_n = t['total_trips']
    return {
        'trips': trips_n,
        'days_driven': len(days),
        'fares': t['total_gross'],
        'avg_fare': (t['total_gross'] / trips_n).quantize(Decimal('0.01')) if trips_n else ZERO,
        'commission': commission,
        'daily_fee': daily_fee,
        'platform_fee': commission + daily_fee,
        'tips': ZERO,
        'tip_riders': 0,
        'earned': t['total_net'] - daily_fee,
        'paid_to_bank': paid,
        'payouts': len(payouts),
        'cash_collected': t['cash_collected'],
        'unresolved_trips': t['unresolved_trips'],
    }


def _strings(summary):
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in summary.items()}


def fee_note(driver):
    """How the platform fee is worked out, from the driver's own plan (11 Nov): "No fee until
    1 Jan, then ₹10.00 a trip, at most ₹149.00 a day"; never fixed text."""
    from servers.pricing.fee_plans import fee_words

    try:
        return fee_words(driver)
    except Exception:  # noqa: BLE001 - the card shows without its note
        logger.exception('fee note failed driver=%s', driver.id)
        return ''


def joined_on(driver, tz):
    """The first local date there can be earnings: joining, or the first trip if earlier."""
    from servers.rider.models import WalletTransaction
    from .earnings import SETTLEMENT_PURPOSES

    days = []
    joined = getattr(driver.user_id, 'date_joined', None)
    if joined:
        days.append(joined.astimezone(tz).date())
    first = (WalletTransaction.objects.filter(user_id=driver.user_id_id, purpose__in=SETTLEMENT_PURPOSES,
                                              status='completed')
             .order_by('created_at').values_list('created_at', flat=True).first())
    if first:
        days.append(first.astimezone(tz).date())
    return min(days) if days else None


# ---------------------------------------------------------------------------
# The views' bodies
# ---------------------------------------------------------------------------

def history(driver, period, anchor=None, start=None, end=None):
    from base.market_time import driver_timezone
    from .services import driver_currency

    tz = driver_timezone(driver)
    today = datetime.now(tz).date()
    first, last = bounds(period, anchor or today, start, end)
    data = _Data(driver, _start(tz, first), _start(tz, last + timedelta(days=1)))
    summary = summarise_period(data.trips, data.fees, data.payouts, tz)

    unit = chart_unit(period, first, last)
    # Each bar carries its breakdown (11 Nov), for the chart's tap or hover: fares, platform fee, earned.
    bars = {k: {'key': k, 'earned': ZERO, 'trips': 0, 'fares': ZERO, 'platform_fee': ZERO}
            for k in _bucket_keys(unit, first, last)}
    for r in data.trips:
        if r.created_at is None:
            continue
        b = bars.get(_bucket(unit, r.created_at.astimezone(tz)))
        if b is not None:
            b['trips'] += 1
            b['earned'] += r.net or ZERO
            b['fares'] += r.gross or ZERO
            b['platform_fee'] += r.commission or ZERO
    for f in data.fees:
        b = bars.get(_bucket(unit, f.created_at.astimezone(tz)))
        if b is not None:
            b['earned'] -= _fee_amount(f)
            b['platform_fee'] += _fee_amount(f)

    joined = joined_on(driver, tz)
    return {
        'period': period, 'start': first.isoformat(), 'end': last.isoformat(),
        'timezone': getattr(tz, 'key', str(tz)), 'currency': driver_currency(driver),
        'today': today.isoformat(), 'joined': joined.isoformat() if joined else None,
        'has_prev': joined is not None and first > joined,
        'has_next': last < today,
        'prev': shift(period, first, last, -1).isoformat(), 'next': shift(period, first, last, 1).isoformat(),
        'summary': {**_strings(summary), 'fee_note': fee_note(driver), 'tips_enabled': False},
        'chart': {'unit': unit, 'bars': [{**b, 'earned': str(b['earned']), 'fares': str(b['fares']),
                                          'platform_fee': str(b['platform_fee'])} for b in bars.values()]},
    }


def months(driver):
    """Every month since the driver joined, newest first; the current one is `so_far`."""
    from base.market_time import driver_timezone
    from .services import driver_currency

    tz = driver_timezone(driver)
    today = datetime.now(tz).date()
    joined = joined_on(driver, tz) or today
    data = _Data(driver)

    def month_of(dt):
        return dt.astimezone(tz).strftime('%Y-%m')

    out = []
    m = today.replace(day=1)
    while m >= joined.replace(day=1):
        key = m.strftime('%Y-%m')
        s = summarise_period([r for r in data.trips if r.created_at and month_of(r.created_at) == key],
                             [f for f in data.fees if month_of(f.created_at) == key],
                             [p for p in data.payouts if month_of(p.created_at) == key], tz)
        out.append({'month': key, 'so_far': m == today.replace(day=1), **_strings(s)})
        m = (m - timedelta(days=1)).replace(day=1)
    return {'currency': driver_currency(driver), 'joined': joined.strftime('%Y-%m'), 'months': out}


def statement(driver, year, month):
    """One month: who, the summary, every trip and payout line, and the ledger's opening and closing."""
    from base.market_time import driver_timezone
    from servers.payments.ledger import build_statement
    from . import requirements as req
    from .services import driver_currency

    tz = driver_timezone(driver)
    first = date(year, month, 1)
    last = _month_end(first)
    since, until = _start(tz, first), _start(tz, last + timedelta(days=1))
    data = _Data(driver, since, until)
    summary = summarise_period(data.trips, data.fees, data.payouts, tz)
    ledger = build_statement(driver, since, until)
    country = req.for_driver(driver)['country']
    from base.encrypted import mask

    pan = mask(driver.pan_number) if country == 'IN' and driver.pan_number else None
    from django.conf import settings
    from servers.admin_access.masking import mask_phone
    from servers.pricing.fee_plan_models import DriverDailyFee

    notes = {f'DAY_{r.day:%Y%m%d}': r.explanation for r in DriverDailyFee.objects.filter(
        driver=driver, day__gte=first - timedelta(days=1), day__lte=last)}
    today = datetime.now(tz).date()
    produced = datetime.now(tz)
    home = getattr(driver, 'home_city', None)
    return {
        'driver': driver.user_id.full_name or '', 'pan_masked': pan, 'country': country,
        # Who and from whom (11 Nov): a statement a bank or the tax office can read on its own.
        'phone_masked': mask_phone(driver.user_id.phone_number) or '',
        'city': getattr(home, 'name', '') or '',
        # The entity operating the driver's market (13 Nov, base/legal_entity.py).
        'company': _company(country),
        'number': f'SG-{first:%Y%m}-{driver.id:06d}', 'produced_on': produced.date().isoformat(),
        'fee_note': fee_note(driver),
        'month': first.strftime('%Y-%m'), 'start': first.isoformat(), 'end': last.isoformat(),
        'so_far': first <= today <= last, 'currency': driver_currency(driver),
        'summary': _strings(summary),
        'opening': ledger and str(_minor(ledger['opening_minor'], ledger['currency'])),
        'closing': ledger and str(_minor(ledger['closing_minor'], ledger['currency'])),
        'trips': [{'at': r.created_at.astimezone(tz).isoformat() if r.created_at else None,
                   'trip_id': r.trip_id, 'method': r.method, 'fare': _s(r.gross), 'fee': _s(r.commission),
                   'earned': _s(r.net)} for r in data.trips],
        'fees': [{'at': f.created_at.astimezone(tz).isoformat(), 'amount': str(_fee_amount(f)),
                  'note': notes.get(f.reference_id, '')} for f in data.fees],
        'payouts': [{'at': p.created_at.astimezone(tz).isoformat(), 'amount': str(_payout_amount(p)),
                     'method': p.method, 'withdrawal_id': p.withdrawal_request_id} for p in data.payouts],
    }


def _s(v):
    return None if v is None else str(v)


def _minor(v, currency):
    from base.money import from_minor

    return from_minor(v, currency)


def ledger_movement(driver, since, until):
    """The wallet movement from trips and platform fees in `[since, until)` (for the tests)."""
    from servers.payments.ledger import counted, SIGNED
    from servers.pricing.fee_plans import PURPOSE, REFUND_PURPOSE
    from servers.rider.models import WalletTransaction
    from .earnings import SETTLEMENT_PURPOSES

    rows = counted(WalletTransaction.objects.filter(
        user_id=driver.user_id_id, purpose__in=(*SETTLEMENT_PURPOSES, PURPOSE, REFUND_PURPOSE),
        created_at__gte=since, created_at__lt=until))
    return rows.aggregate(t=Coalesce(Sum(SIGNED), Value(0), output_field=BigIntegerField()))['t']


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

SUMMARY_ROWS = (('Trips', 'trips'), ('Trip fares', 'fares'), ('Platform fee', 'platform_fee'), ('Tips', 'tips'),
                ('You earned', 'earned'), ('Paid to your bank', 'paid_to_bank'),
                ('Cash you collected', 'cash_collected'))


def _month_label(st):
    d = date.fromisoformat(st['start'])
    return d.strftime('%B %Y') + (' (so far)' if st['so_far'] else '')


def _day(st, iso):
    """A date in the market's order (11 Nov): dd/mm/yyyy in India, mm/dd/yyyy in the USA."""
    d = date.fromisoformat(iso[:10])
    return d.strftime('%m/%d/%Y' if st['country'] == 'US' else '%d/%m/%Y')


def _when(st, iso):
    """A local date and time as the files print it: "02/11/2026 09:10"."""
    return f'{_day(st, iso)} {iso[11:16]}' if iso else ''


def _company(country):
    from base import legal_entity

    e = legal_entity.entity(country)
    return {'name': e.get('legal_name', ''), 'address': e.get('registered_address', ''),
            'cin': e.get('cin', ''), 'gstin': e.get('gstin', '') if e.get('gst_registered') else ''}


def _identity_rows(st):
    """The statement's header lines (11 Nov): the company, the statement, the driver, the period.
    A company line that is not configured is left out, never invented (docs/config-required.md)."""
    c = st['company']
    rows = [['Issued by', c['name']]] if c['name'] else []
    if c['address']:
        rows.append(['Registered address', c['address']])
    if c.get('cin'):
        rows.append(['CIN', c['cin']])
    if c['gstin']:
        rows.append(['GSTIN', c['gstin']])
    rows += [['Statement number', st['number']], ['Produced on', _day(st, st['produced_on'])], ['Driver', st['driver'] or '-'],
             ['Phone', st['phone_masked'] or '-']]
    if st['pan_masked']:
        rows.append(['PAN', st['pan_masked']])
    if st['city']:
        rows.append(['City', st['city']])
    rows.append(['Period', f"{_month_label(st)}: {_day(st, st['start'])} to {_day(st, st['end'])}"])
    if st.get('fee_note'):
        rows.append(['Platform fee', st['fee_note']])
    return rows


def statement_csv(st):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['SaaradhiGo earnings statement'])
    for row in _identity_rows(st):
        w.writerow(row)
    w.writerow(['Currency', st['currency']])
    w.writerow([])
    for label, key in SUMMARY_ROWS:
        w.writerow([label, st['summary'][key]])
    w.writerow(['Opening balance', st['opening'] or ''])
    w.writerow(['Closing balance', st['closing'] or ''])
    w.writerow([])
    w.writerow(['Trips'])
    w.writerow(['Date and time', 'Trip', 'Paid by', 'Fare', 'Fee', 'You earned'])
    for t in st['trips']:
        w.writerow([_when(st, t['at']), t['trip_id'] or '', t['method'], t['fare'] or '', t['fee'] or '', t['earned'] or ''])
    w.writerow([])
    w.writerow(['Daily platform fees'])
    w.writerow(['Date and time', 'Amount', 'Note'])
    for f in st['fees']:
        w.writerow([_when(st, f['at']), f['amount'], f['note']])
    w.writerow([])
    w.writerow(['Payouts'])
    w.writerow(['Date and time', 'Amount', 'Method'])
    for p in st['payouts']:
        w.writerow([_when(st, p['at']), p['amount'], p['method']])
    return buf.getvalue()


LOGO = __file__.rsplit('earnings_history.py', 1)[0] + 'data/logo.jpg'


def statement_pdf(st):
    """The statement as a PDF: letterhead and logo, the driver, masked PAN (India), the period,
    the summary, every trip line and every payout line. Amounts carry the currency code, since
    the PDF's built-in font has no rupee sign."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    cur = st['currency']

    def m(v):
        return '' if v in (None, '') else f'{cur} {Decimal(v):,.2f}'

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=f"SaaradhiGo statement {st['month']}", author='SaaradhiGo')
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('h1', parent=styles['Heading1'], textColor=colors.HexColor('#8A5300'), spaceAfter=0)
    h2 = ParagraphStyle('h2', parent=styles['Heading3'], spaceBefore=10)
    grid = TableStyle([('FONTSIZE', (0, 0), (-1, -1), 8.5), ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.grey),
                       ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAF7F2')]),
                       ('ALIGN', (3, 0), (-1, -1), 'RIGHT')])
    head = [Paragraph('SaaradhiGo', h1), Paragraph('Earnings statement', styles['Heading4'])]
    try:
        logo = Image(LOGO, width=18 * mm, height=16 * mm)
        story = [Table([[logo, head]], colWidths=[22 * mm, 150 * mm],
                       style=[('VALIGN', (0, 0), (-1, -1), 'MIDDLE')])]
    except Exception:  # noqa: BLE001 - the letterhead without the logo
        story = head
    cell = ParagraphStyle('cell', parent=styles['Normal'], fontSize=9.5, leading=11.5)
    # The built-in font has neither the rupee sign nor the mask bullet, so "Rs" and "x" stand in.
    who = [[k, Paragraph(str(v).replace('₹', 'Rs ').replace('•', 'x'), cell)] for k, v in _identity_rows(st)]
    story += [Spacer(1, 6), Table(who, colWidths=[40 * mm, 130 * mm], style=[('FONTSIZE', (0, 0), (-1, -1), 9.5),
                                                                             ('VALIGN', (0, 0), (-1, -1), 'TOP')])]
    s = st['summary']
    rows = [[label, str(s[key]) if key == 'trips' else m(s[key])] for label, key in SUMMARY_ROWS]
    rows += [['Opening balance', m(st['opening'])], ['Closing balance', m(st['closing'])]]
    story += [Paragraph('Summary', h2),
              Table(rows, colWidths=[60 * mm, 50 * mm], style=[('FONTSIZE', (0, 0), (-1, -1), 9.5),
                                                               ('ALIGN', (1, 0), (1, -1), 'RIGHT')])]
    trips = [['Date and time', 'Trip', 'Paid by', 'Fare', 'Fee', 'You earned']]
    trips += [[_when(st, t['at']), f"#{t['trip_id']}" if t['trip_id'] else '', t['method'],
               m(t['fare']), m(t['fee']), m(t['earned'])] for t in st['trips']]
    story += [Paragraph(f"Trips ({len(st['trips'])})", h2),
              Table(trips, colWidths=[32 * mm, 18 * mm, 22 * mm, 34 * mm, 30 * mm, 36 * mm], style=grid, repeatRows=1)]
    if st['fees']:
        fees = [['Date and time', 'Amount', 'Note']] + [[_when(st, f['at']), m(f['amount']), f['note'][:70]]
                                                        for f in st['fees']]
        story += [Paragraph('Daily platform fees', h2),
                  Table(fees, colWidths=[32 * mm, 30 * mm, 110 * mm], repeatRows=1,
                        style=TableStyle([('FONTSIZE', (0, 0), (-1, -1), 8.5),
                                          ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.grey)]))]
    payouts = [['Date and time', 'Amount', 'Method']] + [[_when(st, p['at']), m(p['amount']), p['method']]
                                                         for p in st['payouts']]
    story += [Paragraph(f"Payouts ({len(st['payouts'])})", h2),
              Table(payouts, colWidths=[32 * mm, 34 * mm, 40 * mm], repeatRows=1,
                    style=TableStyle([('FONTSIZE', (0, 0), (-1, -1), 8.5),
                                      ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.grey)]))]
    doc.build(story)
    return buf.getvalue()
