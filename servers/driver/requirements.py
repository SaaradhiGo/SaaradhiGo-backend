"""What a driver must provide, per market (10 Nov), and how far they are.

THE CONFIG
----------
`pricing.MarketRegulation.driver_requirements` holds, per country and optionally per state,
`{item: {"need": "required"|"optional"|"off", "blocks_online": bool}}`, edited in the console
through the same `pricing.change` maker-checker as the rest of the row. A state row overrides
the country row item by item; `min_driver_age`, `max_vehicle_age_years` and `languages` resolve
the same way. The driver web and app read the result from `/driver/requirements/`; neither
screen knows which country wants what.

THE CHECKLIST
-------------
Each item the market asks for, with a status: `missing`, `in_review`, `approved`, `rejected`
(with the reviewer's reason), `expiring` (with the date) or `done` (a detail with nothing to
review). Documents and photos are reviewed one by one in the console (`DriverDocumentReview`,
same rule as before: a review counts for the file it was about).

THE GATE
--------
Going online is still `approved` (qa_eligibility.may_operate). Approval is refused while any
item marked `blocks_online` is missing, rejected or expired (`blocking_unmet`); approving the
driver approves the files the reviewer saw. Drivers approved before 10 Nov stay approved and
see their missing items on the checklist.
"""
from datetime import date, timedelta

from django.utils import timezone

from base.regions import IN_STATES, US_STATES

REQUIRED, OPTIONAL, OFF = 'required', 'optional', 'off'

#: key -> (section, label, what the driver does). Order is the checklist's order.
ITEMS = {
    'profile_photo': ('personal', 'Profile photo', 'A clear photo of your face. Riders see it.'),
    'legal_name': ('personal', 'Full name as on your licence', 'Your name exactly as on your driving licence.'),
    'date_of_birth': ('personal', 'Date of birth', 'You must be old enough to drive here.'),
    'address': ('personal', 'Current address', 'Where you live now.'),
    'permanent_address': ('personal', 'Permanent address', 'If it differs from your current address.'),
    'address_proof': ('personal', 'Address proof', 'A utility bill, rental agreement or similar, under 3 months old.'),
    'emergency_contact': ('personal', 'Emergency contact', 'Someone we call if you need help on a trip.'),
    'driving_licence': ('identity', 'Driving licence', 'Number, expiry date, front and back.'),
    'aadhaar': ('identity', 'Aadhaar', 'The last 4 digits and a copy. We never store the full number.'),
    'pan': ('identity', 'PAN', 'For TDS on your earnings.'),
    'police_verification': ('identity', 'Police verification', 'Your police verification certificate.'),
    'background_consent': ('identity', 'Background check consent',
                           'Read the disclosure, then authorise the check. You enter your SSN only in the provider\'s form.'),
    'background_check': ('identity', 'Background check', 'We start this for you once your details are in.'),
    'accessibility_ack': ('personal', 'Non-discrimination and accessibility',
                          'Agree to serve every rider, including service animals and riders with wheelchairs.'),
    'vehicle': ('vehicle', 'Vehicle details', 'Type, make, model, year, colour and number plate.'),
    'vehicle_photos': ('vehicle', 'Vehicle photos', 'Front, back, both sides and the inside.'),
    'rc': ('vehicle', 'Registration Certificate (RC)', 'Issued by the RTO.'),
    'registration': ('vehicle', 'Vehicle registration', 'Your state registration.'),
    'insurance': ('vehicle', 'Vehicle insurance', 'Valid commercial insurance.'),
    'permit': ('vehicle', 'Commercial permit', 'Passenger permit for your area.'),
    'fitness': ('vehicle', 'Fitness certificate', 'Confirms the vehicle is roadworthy.'),
    'puc': ('vehicle', 'Pollution Under Control (PUC)', 'A valid PUC certificate.'),
    'inspection': ('vehicle', 'Vehicle inspection', 'Where your state requires one.'),
    'payout_method': ('payouts', 'Payout method', 'Where we send your earnings.'),
}

VEHICLE_DOCS = ('rc', 'registration', 'insurance', 'permit', 'fitness', 'puc', 'inspection')
PHOTO_COLUMNS = (('front', 'vehicle_pic'), ('back', 'photo_back'), ('left', 'photo_left'),
                 ('right', 'photo_right'), ('interior', 'photo_interior'))
#: Review keys (DriverDocumentReview.document_key) for the items that are files.
DRIVER_FILES = {
    'profile_photo': 'photo_pending', 'address_proof': 'address_proof_doc', 'aadhaar': 'aadhaar_doc',
    'pan': 'pan_doc', 'police_verification': 'police_verification_doc',
}
EXPIRY_FOR = {'insurance': 'insurance_expiry', 'permit': 'permit_expiry', 'fitness': 'fitness_expiry',
              'puc': 'puc_expiry', 'registration': 'registration_expiry'}
#: What an item is called in a market where it means something else (14 Nov, USA).
LABELS_BY_COUNTRY = {
    'US': {'insurance': ('Personal auto insurance', 'Your own policy for this vehicle, with its expiry date.'),
           'driving_licence': ("Driver's license", 'Number, expiry date, front and back.'),
           'registration': ('Vehicle registration', 'Your state registration, with its expiry date.')},
}
EXPIRING_DAYS = 30

DEFAULT_LANGUAGES = {'IN': ['en', 'te', 'hi'], 'US': ['en', 'es']}


def market_of(driver):
    from .records import market_of as records_market

    return records_market(driver)


def _rows(country, state):
    from servers.pricing.regulation_models import MarketRegulation

    rows = {r.state_code: r for r in MarketRegulation.objects.filter(country=country, state_code__in=['', state])}
    return [r for r in (rows.get(state) if state else None, rows.get('')) if r is not None]


def resolve(country, state=''):
    """The market's requirements: `{items: {key: {need, blocks_online, source}}, min_driver_age,
    max_vehicle_age_years, languages}`. An item no row names is `off`."""
    rows = _rows(country, state)
    items = {}
    for key in ITEMS:
        for row in rows:
            got = (row.driver_requirements or {}).get(key)
            if got:
                items[key] = {'need': got.get('need', OFF), 'blocks_online': bool(got.get('blocks_online')),
                              'source': row.state_code or 'country'}
                break
        else:
            items[key] = {'need': OFF, 'blocks_online': False, 'source': None}

    def pick(field, default=None):
        for row in rows:
            if getattr(row, field) is not None:
                return getattr(row, field)
        return default

    return {'country': country, 'state': state, 'items': items,
            'min_driver_age': pick('min_driver_age'), 'max_vehicle_age_years': pick('max_vehicle_age_years'),
            'min_vehicle_year': pick('min_vehicle_year'), 'vehicle_min_doors': pick('vehicle_min_doors'),
            'languages': pick('languages') or DEFAULT_LANGUAGES.get(country, ['en'])}


def for_driver(driver):
    country, state = market_of(driver)
    return resolve(country or 'IN', state)


# ---------------------------------------------------------------------------
# Status of each item
# ---------------------------------------------------------------------------

def _latest_reviews(driver, vehicle):
    from .models import DriverDocumentReview

    out = {}
    for r in DriverDocumentReview.objects.filter(driver=driver).order_by('reviewed_at', 'id'):
        if r.vehicle_id is None or (vehicle is not None and r.vehicle_id == vehicle.id):
            out[(r.document_key, r.vehicle_id)] = r
    return out


def _file_state(reviews, key, owner, column, vehicle_id=None):
    field = getattr(owner, column, None) if owner is not None else None
    name = getattr(field, 'name', '') if field else ''
    if not name:
        return 'missing', None
    review = reviews.get((key, vehicle_id))
    if review is not None and review.file_name == name:
        return review.state, (review.reason or None) if review.state == 'rejected' else None
    return 'in_review', None


def _worst(states):
    for s in ('rejected', 'missing', 'in_review'):
        for state, reason in states:
            if state == s:
                return state, reason
    return 'approved', None


def age_on(dob, today):
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _vehicle(driver):
    from .models import Vehicle

    return driver.active_vehicle or (Vehicle.objects.filter(driver_id=driver).exclude(status='archived')
                                     .order_by('id').first())


def item_status(driver, key, config, *, vehicle=None, reviews=None, today=None):
    """`{status, reason, expires}` for one item."""
    from .models import DriverAddress, DriverBankAccount, DriverEmergencyContact
    from .serializers import has_valid_driver_name

    today = today or timezone.localdate()
    user = driver.user_id
    reviews = reviews if reviews is not None else _latest_reviews(driver, vehicle)
    out = {'status': 'missing', 'reason': None, 'expires': None}

    def done(ok):
        out['status'] = 'done' if ok else 'missing'
        return out

    if key == 'profile_photo':
        if getattr(driver.photo_pending, 'name', ''):
            out['status'], out['reason'] = _file_state(reviews, key, driver, 'photo_pending')
            if out['status'] == 'approved':
                out['status'] = 'approved'
            elif out['status'] == 'rejected' and user.avatar:
                out['reason'] = out['reason']        # the old photo stays; the new one was refused
            return out
        out['status'] = 'approved' if user.avatar else 'missing'
        return out
    if key == 'legal_name':
        if driver.name_pending:
            out['status'] = 'in_review'
            return out
        return done(has_valid_driver_name(user))
    if key == 'date_of_birth':
        dob = getattr(user, 'dob', None)
        if not dob:
            return out
        minimum = config.get('min_driver_age')
        if minimum and age_on(dob, today) < minimum:
            out['status'], out['reason'] = 'rejected', f'You must be at least {minimum} to drive here.'
            return out
        return done(True)
    if key == 'address':
        return done(DriverAddress.objects.filter(driver=driver, kind='current').exists())
    if key == 'permanent_address':
        return done(DriverAddress.objects.filter(driver=driver, kind='permanent').exists())
    if key == 'emergency_contact':
        return done(DriverEmergencyContact.objects.filter(driver=driver, position=1).exists())
    if key in DRIVER_FILES:
        state = _file_state(reviews, key, driver, DRIVER_FILES[key])
        if key == 'aadhaar' and state[0] != 'missing' and len(driver.aadhaar_last4 or '') != 4:
            state = ('missing', None)
        if key == 'pan' and state[0] != 'missing' and not driver.pan_number:
            state = ('missing', None)
        out['status'], out['reason'] = state
        return out
    if key == 'driving_licence':
        if not driver.license_number or not driver.license_expiry:
            states = [('missing', None)]
        else:
            states = [_file_state(reviews, 'license_front', driver, 'license_doc'),
                      _file_state(reviews, 'license_back', driver, 'license_doc_back')]
        out['status'], out['reason'] = _worst(states)
        if driver.license_expiry:
            out['expires'] = driver.license_expiry.isoformat()
            if driver.license_expiry < today:
                out['status'], out['reason'] = 'rejected', 'Your licence has expired. Upload the renewed one.'
            elif out['status'] == 'approved' and driver.license_expiry <= today + timedelta(days=EXPIRING_DAYS):
                out['status'] = 'expiring'
        return out
    if key == 'background_check':
        out['status'] = {'clear': 'approved', 'pending': 'in_review', 'consider': 'in_review',
                         'failed': 'rejected', 'suspended': 'rejected'}.get(driver.background_check_status, 'missing')
        if driver.background_check_status == 'consider':
            out['reason'] = 'Our team is reviewing your report.'
        if driver.background_check_status == 'suspended':
            out['reason'] = 'Your check needs attention. Contact support.'
        return out
    if key == 'background_consent':
        if not driver.background_disclosure_at:
            out['reason'] = 'Read the background check disclosure.'
            return out
        return done(bool(driver.background_consent_at))
    if key == 'accessibility_ack':
        return done(bool(driver.accessibility_ack_at))
    if key == 'vehicle':
        if vehicle is None:
            return out
        if vehicle.custom_make_model:
            out['status'] = 'in_review' if not driver.approved else 'approved'
            return out
        # 14 Nov (USA): the market's oldest model year and minimum doors.
        oldest, doors = config.get('min_vehicle_year'), config.get('vehicle_min_doors')
        if oldest and vehicle.year and vehicle.year < oldest:
            out['status'], out['reason'] = 'rejected', f'The vehicle must be {oldest} or newer here.'
            return out
        if doors and vehicle.doors is not None and vehicle.doors < doors:
            out['status'], out['reason'] = 'rejected', f'The vehicle must have at least {doors} doors here.'
            return out
        return done(bool(vehicle.vehicle_number and vehicle.brand and vehicle.model
                         and (not doors or vehicle.doors)))
    if key == 'vehicle_photos':
        if vehicle is None:
            out['reason'] = 'Add your vehicle first.'
            return out
        out['status'], out['reason'] = _worst([
            _file_state(reviews, f'vehicle_photo_{side}', vehicle, col, vehicle.id) for side, col in PHOTO_COLUMNS])
        return out
    if key in VEHICLE_DOCS:
        if vehicle is None:
            out['reason'] = 'Add your vehicle first.'
            return out
        from .compliance import VEHICLE_DOCUMENT_COLUMNS

        out['status'], out['reason'] = _file_state(reviews, key, vehicle, VEHICLE_DOCUMENT_COLUMNS[key], vehicle.id)
        expiry = getattr(vehicle, EXPIRY_FOR.get(key, ''), None) if key in EXPIRY_FOR else None
        if expiry:
            out['expires'] = expiry.isoformat()
            if expiry < today:
                out['status'], out['reason'] = 'rejected', 'This document has expired. Upload the renewed one.'
            elif out['status'] == 'approved' and expiry <= today + timedelta(days=EXPIRING_DAYS):
                out['status'] = 'expiring'
        elif key in EXPIRY_FOR and out['status'] == 'approved':
            # A dated paper without its date is not done (11 Nov): the reviewer enters it on approval.
            out['status'], out['reason'] = 'missing', 'Add the expiry date shown on the document.'
        return out
    if key == 'payout_method':
        if config['country'] == 'US':
            out['status'] = {'ready': 'approved', 'in_progress': 'in_review'}.get(
                driver.stripe_onboarding_status, 'missing')
            return out
        bank = DriverBankAccount.objects.filter(driver=driver).first()
        if bank is not None:
            out['status'] = {'verified': 'approved', 'failed': 'rejected'}.get(bank.verification_status, 'in_review')
            if bank.verification_status == 'failed':
                out['reason'] = 'We could not verify this account. Check the details.'
            return out
        return done(bool((driver.upi_id or '').strip()))
    return out


DONE_STATES = ('approved', 'done', 'expiring')


def checklist(driver, today=None):
    """Every item the driver's market asks for, in order, with its status and progress."""
    config = for_driver(driver)
    vehicle = _vehicle(driver)
    reviews = _latest_reviews(driver, vehicle)
    rows = []
    labels = LABELS_BY_COUNTRY.get(config['country'], {})
    for key, (section, label, help_text) in ITEMS.items():
        need = config['items'][key]
        if need['need'] == OFF:
            continue
        label, help_text = labels.get(key, (label, help_text))
        st = item_status(driver, key, config, vehicle=vehicle, reviews=reviews, today=today)
        rows.append({'key': key, 'section': section, 'label': label, 'help': help_text,
                     'need': need['need'], 'blocks_online': need['blocks_online'], **st,
                     'step': STEP_OF[key], 'files': item_files(driver, key, vehicle, reviews)})
    if config['country'] == 'US':
        # 14 Nov (owner): US drivers read US English ("License, front"); India keeps UK English.
        from base.us_english import us_english

        for r in rows:
            r['label'], r['help'] = us_english(r['label']), us_english(r['help'])
            for f in r['files']:
                f['label'] = us_english(f['label'])
    required = [r for r in rows if r['need'] == REQUIRED]
    steps = step_summary(rows)
    return {'market': {'country': config['country'], 'state': config['state']},
            'items': rows,
            'done': sum(1 for r in required if r['status'] in DONE_STATES),
            'total': len(required),
            'blocking': [r['key'] for r in rows if r['blocks_online'] and r['need'] == REQUIRED
                         and r['status'] in ('missing', 'rejected')],
            'approved': driver.approved,
            'steps': steps,
            'steps_done': sum(1 for st in steps if st['status'] == 'done'),
            'next_step': next_step(steps),
            'minutes_left': sum(MINUTES.get(r['key'], 2) for r in required if r['status'] in ('missing', 'rejected'))}


# ---------------------------------------------------------------------------
# Guided setup (10 Nov v2): six steps, each the items the market asks for
# ---------------------------------------------------------------------------

#: step key -> item keys, in the order the setup shows them. A step with no item the market asks
#: for is not shown.
STEPS = (
    ('personal', ('profile_photo', 'legal_name', 'date_of_birth', 'address', 'permanent_address', 'address_proof',
                  'accessibility_ack')),
    ('contact', ('emergency_contact',)),
    ('identity', ('driving_licence', 'aadhaar', 'pan', 'police_verification', 'background_consent',
                  'background_check')),
    ('vehicle', ('vehicle',)),
    ('vehicle_docs', ('vehicle_photos', 'rc', 'registration', 'insurance', 'permit', 'fitness', 'puc', 'inspection')),
    ('payouts', ('payout_method',)),
)
STEP_OF = {key: step for step, keys in STEPS for key in keys}
#: Rough minutes each item takes, for "About 10 minutes left".
MINUTES = {'profile_photo': 1, 'legal_name': 1, 'date_of_birth': 1, 'address': 2, 'permanent_address': 1,
           'address_proof': 1, 'emergency_contact': 1, 'driving_licence': 2, 'aadhaar': 1, 'pan': 1,
           'police_verification': 1, 'background_check': 0, 'vehicle': 2, 'vehicle_photos': 3,
           'background_consent': 2, 'accessibility_ack': 1,
           'payout_method': 2}


def step_status(items):
    """The worst of a step's required items: action (rejected), todo (missing), in_review, done."""
    states = {i['status'] for i in items if i['need'] == REQUIRED} or {i['status'] for i in items}
    if 'rejected' in states:
        return 'action'
    if 'missing' in states:
        return 'todo'
    if 'in_review' in states:
        return 'in_review'
    return 'done'


def step_summary(rows):
    out = []
    for step, keys in STEPS:
        items = [r for r in rows if r['key'] in keys]
        if items:
            out.append({'key': step, 'number': len(out) + 1, 'items': [i['key'] for i in items],
                        'status': step_status(items)})
    return out


def next_step(steps):
    """The step Continue setup opens: the first needing the driver, else the first in review."""
    for wanted in (('action', 'todo'), ('in_review',)):
        for st in steps:
            if st['status'] in wanted:
                return st['key']
    return None


VEHICLE_PHOTO_LABELS = {'front': 'Front', 'back': 'Back', 'left': 'Left side', 'right': 'Right side',
                        'interior': 'Inside'}
#: The files behind each item, (review key, label, column); vehicle items read the vehicle.
FILE_SLOTS = {
    'profile_photo': (('profile_photo', 'Profile photo', 'photo_pending'),),
    'address_proof': (('address_proof', 'Address proof', 'address_proof_doc'),),
    'aadhaar': (('aadhaar', 'Aadhaar card', 'aadhaar_doc'),),
    'pan': (('pan', 'PAN card', 'pan_doc'),),
    'police_verification': (('police_verification', 'Police verification certificate', 'police_verification_doc'),),
    'driving_licence': (('license_front', 'Licence, front', 'license_doc'),
                        ('license_back', 'Licence, back', 'license_doc_back')),
    'vehicle_photos': tuple((f'vehicle_photo_{side}', VEHICLE_PHOTO_LABELS[side], col) for side, col in PHOTO_COLUMNS),
}


def item_files(driver, key, vehicle, reviews):
    """One tile per file, each with its own status. A file that is uploaded and not yet decided is
    in_review, never missing."""
    from .file_times import uploaded_at

    if key in VEHICLE_DOCS:
        from .compliance import VEHICLE_DOCUMENT_COLUMNS

        slots, on_vehicle = ((key, ITEMS[key][1], VEHICLE_DOCUMENT_COLUMNS[key]),), True
    elif key in FILE_SLOTS:
        slots, on_vehicle = FILE_SLOTS[key], key == 'vehicle_photos'
    else:
        return []
    owner = vehicle if on_vehicle else driver
    out = []
    for review_key, label, column in slots:
        if on_vehicle and vehicle is None:
            state, reason = 'missing', None
        else:
            state, reason = _file_state(reviews, review_key, owner, column, vehicle.id if on_vehicle else None)
        if key == 'profile_photo' and state == 'missing' and driver.user_id.avatar:
            state = 'approved'
        out.append({'key': review_key, 'label': label, 'status': state, 'reason': reason,
                    'uploaded_at': uploaded_at(owner, column) if state != 'missing' else None,
                    'vehicle': on_vehicle})
    return out


def missing_expiry(driver):
    """Labels of the dated papers on file whose expiry date is not recorded (11 Nov)."""
    vehicle = _vehicle(driver)
    if vehicle is None:
        return []
    from .compliance import VEHICLE_DOCUMENT_COLUMNS

    config = for_driver(driver)
    out = []
    for key, column in EXPIRY_FOR.items():
        if config['items'].get(key, {}).get('need') == OFF:
            continue
        if getattr(getattr(vehicle, VEHICLE_DOCUMENT_COLUMNS[key], None), 'name', '') and not getattr(vehicle, column, None):
            out.append(ITEMS[key][1])
    return out


def summary(driver, data=None):
    """The checklist in a few numbers and labels (12 Nov), for the console's badges, list and KYC
    queue: the same function the driver's own pages and the approval gate use."""
    data = data or checklist(driver)
    required = [r for r in data['items'] if r['need'] == REQUIRED]
    return {'done': data['done'], 'total': data['total'],
            'blocking': [ITEMS[k][1] for k in data['blocking']],
            'outstanding': [r['label'] for r in required if r['status'] not in DONE_STATES],
            'expiring': [r['label'] for r in required if r['status'] == 'expiring']}


def blocking_unmet(driver):
    """Labels of the items that keep this driver from being approved (missing, rejected, expired)."""
    data = checklist(driver)
    return [ITEMS[k][1] for k in data['blocking']]


def market_gates_unmet(driver):
    """What the MARKET still lacks before any driver here may go online (14 Nov, USA): an active
    driver fee plan and the platform's TNC insurance (Idaho Code 41-2519), where required. These
    never block approval: the driver did nothing wrong."""
    from servers.pricing.regulation import market_rules, platform_insurance_ready

    country, state = market_of(driver)
    rules = market_rules(country or 'IN', state)
    out = []
    if rules['platform_insurance_required'] and not platform_insurance_ready(rules):
        out.append('Platform insurance is not set up for your city yet')
    if rules['fee_plan_required']:
        from servers.pricing.fee_plans import local_day, resolve
        from servers.payments.payout_scope import scope_codes_of_node

        home = getattr(driver, 'home_city', None)
        if resolve(country, scope_codes_of_node(home) if home else [], local_day(country)) is None:
            out.append('The driver fee plan for your city is not set yet')
    return out


def approve_reviewed_files(driver, reviewer):
    """On approving a driver: every file of a blocking item nobody has decided on is approved."""
    from .models import DriverDocumentReview

    vehicle = _vehicle(driver)
    files = [(k, driver, col, None) for k, col in DRIVER_FILES.items()]
    files += [('license_front', driver, 'license_doc', None), ('license_back', driver, 'license_doc_back', None)]
    if vehicle is not None:
        from .compliance import VEHICLE_DOCUMENT_COLUMNS

        files += [(k, vehicle, VEHICLE_DOCUMENT_COLUMNS[k], vehicle) for k in VEHICLE_DOCS]
        files += [(f'vehicle_photo_{side}', vehicle, col, vehicle) for side, col in PHOTO_COLUMNS]
    reviews = _latest_reviews(driver, vehicle)
    for key, owner, column, veh in files:
        state, _ = _file_state(reviews, key, owner, column, veh.id if veh else None)
        if veh is not None and key in EXPIRY_FOR and not getattr(veh, EXPIRY_FOR[key], None):
            continue            # approved only with its expiry date (11 Nov)
        if state == 'in_review':
            DriverDocumentReview.objects.create(
                driver=driver, vehicle=veh, document_key=key, state='approved', reviewed_by=reviewer,
                file_name=getattr(getattr(owner, column), 'name', ''))
            on_file_approved(driver, key)


def on_file_approved(driver, key):
    """Side effects of approving some items: the photo and name go live, police status set."""
    user = driver.user_id
    if key == 'profile_photo' and getattr(driver.photo_pending, 'name', ''):
        user.avatar = driver.photo_pending.name
        user.save(update_fields=['avatar'])
    if key == 'police_verification':
        driver.police_verification_status = 'verified'
        driver.save(update_fields=['police_verification_status'])


def approve_name(driver):
    if driver.name_pending:
        user = driver.user_id
        user.full_name = driver.name_pending
        user.save(update_fields=['full_name'])
        driver.name_pending = ''
        driver.save(update_fields=['name_pending'])


def year_range(config, today=None):
    year = (today or date.today()).year
    oldest = config.get('max_vehicle_age_years')
    first = year - oldest if oldest else year - 30
    # 14 Nov (USA): the market's oldest model year (Idaho default 2011) narrows it further.
    if config.get('min_vehicle_year'):
        first = max(first, int(config['min_vehicle_year']))
    return first, year


def state_names(country):
    return IN_STATES if country == 'IN' else US_STATES
