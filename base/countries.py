"""What a country means, in one place.

WHY THIS EXISTS
---------------
Before this file, "India" was spread across the codebase as a dozen unrelated
constants: `TIME_ZONE = 'Asia/Kolkata'` in settings, `+91` hardcoded in three phone
helpers, `₹` in thirty UI strings, `per_km_fare` on the rate card, `gst_percent` with
inclusive-tax arithmetic in the receipt, and a vehicle document set naming an Indian
Pollution Under Control certificate.

None of those were wrong. They were just not *attributed* to India, so adding a second
country meant finding them all again — and the ones you miss do not fail loudly. They
charge a US rider ₹-magnitude dollars, or ask them for a PUC certificate, or roll their
earnings over at Indian midnight.

So: one registry. A country is a row here, and everything downstream asks this file
rather than carrying its own assumption.

WHAT A ZONE DOES WITH IT
------------------------
`ServiceZone` carries its own `currency`, `distance_unit`, `tax_mode` and `timezone`,
defaulted from this registry at save time. The zone wins, because the US has one currency
and six timezones, and India has one of each — a per-zone override is the only shape that
covers both. The registry supplies the sensible default so nobody has to set four fields
to create a Hyderabad zone.

WHY A REGISTRY RATHER THAN `phonenumbers`
-----------------------------------------
Phone normalisation here is "given a national number and a default region, produce
E.164". libphonenumber is the correct answer to that at twenty countries. At two, an
explicit table is correct, auditable in one screen, and adds no dependency or 3 MB of
metadata to the image.

The upgrade trigger is named rather than left to taste: **when this registry exceeds
about five countries, or the moment a country with variable-length national numbers or
mobile/landline prefix rules is added, replace `normalize_phone` with
`phonenumbers.parse`.** India and the US both have fixed-length national numbers, which
is exactly why the table works today.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Country:
    """Everything market-specific about one country."""

    code: str                       # ISO 3166-1 alpha-2
    name: str

    # --- money -------------------------------------------------------------
    currency: str                   # ISO 4217
    currency_symbol: str
    # Where the symbol goes. '₹500' in India, '$500' in the US -- both prefix,
    # but the euro and the Scandinavian currencies suffix, so this is a field
    # rather than an assumption.
    symbol_position: str            # 'prefix' | 'suffix'

    # --- phone -------------------------------------------------------------
    calling_code: str               # without the '+'
    national_number_lengths: tuple  # valid lengths of the national part

    # --- units -------------------------------------------------------------
    distance_unit: str              # 'km' | 'mi'

    # --- tax ---------------------------------------------------------------
    # 'inclusive': the quoted fare already contains the tax, and the receipt
    #              extracts it (India's GST convention).
    # 'additive':  the fare is quoted without tax, and the tax is a separate
    #              line added on top (the US sales-tax convention).
    #
    # This is the difference between a receipt that balances and one that
    # understates the fare while misstating the tax.
    tax_mode: str                   # 'inclusive' | 'additive'
    tax_label: str                  # what the rider sees on the receipt

    # --- time --------------------------------------------------------------
    # The DEFAULT zone timezone. A country with several (the US has six) sets
    # this per ServiceZone; this is only what a new zone starts with.
    default_timezone: str

    # Last-resort tax rate when a trip's rate card cannot be found. India has a
    # single national GST rate on taxi services, so 5.00 is a correct answer
    # there. The US has none -- sales tax is state and local -- so the only
    # honest default is 0.00, and a US zone that needs tax MUST carry a rate
    # card saying so. Guessing a US rate would overcharge some riders and
    # undercharge others, and a loud configuration error beats both.
    #
    # Declared after `default_timezone` because a dataclass cannot have a
    # defaulted field before a non-defaulted one.
    default_tax_percent: str = '0.00'

    # --- road-network fallbacks --------------------------------------------
    # Used ONLY when Google Distance Matrix is unavailable, to approximate road
    # distance from the great-circle distance and then a duration from it.
    #
    # These were module constants in `servers/ride/utils.py` tuned for
    # Hyderabad: a 1.4 detour factor for a dense, irregular street network and
    # 25 km/h for its traffic. Applied to a US trip they overstate the duration
    # by roughly 60%, and duration is a priced component -- so a fallback quote
    # in San Francisco charged far more time-fare than the ride would take.
    #
    # India's values here are IDENTICAL to those constants, so no Indian fare
    # moves. The US values are starting points for a grid-ish network at
    # American city speeds and should be re-tuned against real trip data
    # before a US launch; they are reached only when the Maps API fails.
    road_distance_factor: float = 1.4
    fallback_avg_speed_kmh: float = 25.0

    # --- compliance --------------------------------------------------------
    # Document keys a driver and a vehicle must supply in this country. The
    # KYC queue is driven by these, so a US driver is never asked for a
    # Pollution Under Control certificate.
    driver_documents: tuple = field(default_factory=tuple)
    vehicle_documents: tuple = field(default_factory=tuple)

    # --- safety ------------------------------------------------------------
    # `(label, number)` pairs the console shows next to an incident and that
    # `seed_safety_config` copies into `SafetyCountryConfig`. Public emergency
    # numbers are a fact about the country, so they live in the registry and
    # not in safety code. A country with none here shows "not configured".
    emergency_numbers: tuple = field(default_factory=tuple)

    @property
    def e164_length(self):
        """Total digits in a full E.164 number for this country, without the '+'."""
        return tuple(len(self.calling_code) + n for n in self.national_number_lengths)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

INDIA = Country(
    code='IN',
    name='India',
    currency='INR',
    currency_symbol='₹',
    symbol_position='prefix',
    calling_code='91',
    national_number_lengths=(10,),
    distance_unit='km',
    # GST on taxi services is levied on the fare the rider is quoted, so the
    # quoted total already contains it and the receipt extracts it.
    tax_mode='inclusive',
    tax_label='GST',
    default_tax_percent='5.00',
    default_timezone='Asia/Kolkata',
    driver_documents=('license_front', 'license_back'),
    # rc = Registration Certificate, puc = Pollution Under Control,
    # fitness = Fitness Certificate, permit = commercial passenger permit.
    # All four are Indian statutory documents with no US equivalent.
    vehicle_documents=('rc', 'insurance', 'permit', 'fitness', 'puc'),
    emergency_numbers=(('unified', '112'),),
)

UNITED_STATES = Country(
    code='US',
    name='United States',
    currency='USD',
    currency_symbol='$',
    symbol_position='prefix',
    calling_code='1',
    national_number_lengths=(10,),
    distance_unit='mi',
    # US sales tax is quoted separately and added to the fare. Using India's
    # inclusive extraction here would understate the fare on the receipt.
    tax_mode='additive',
    tax_label='Sales tax',
    # Deliberately 0: there is no national US sales-tax rate. A US zone must
    # declare its own, and falling back to anything non-zero here would invent
    # a rate for a jurisdiction we were never told about.
    default_tax_percent='0.00',
    # A default only. US zones must set their own: a single national timezone
    # rolls a California driver's earnings over at 9pm the previous day.
    default_timezone='America/New_York',
    # A grid-ish network at American city speeds. Fallback-only; see the field
    # comments on `Country`.
    road_distance_factor=1.25,
    fallback_avg_speed_kmh=40.0,
    driver_documents=('license_front', 'license_back'),
    # No commercial permit, no PUC, no fitness certificate. Vehicle
    # registration and proof of insurance, plus a safety inspection in the
    # states that require one.
    vehicle_documents=('registration', 'insurance', 'inspection'),
    emergency_numbers=(('unified', '911'),),
)

COUNTRIES = {c.code: c for c in (INDIA, UNITED_STATES)}

DEFAULT_COUNTRY_CODE = 'IN'


def get_country(code):
    """Look up a country, falling back to the default.

    Never raises. An unknown code is a configuration error, and the safe
    behaviour for a function on the request path is to return something usable
    rather than 500 — but it is a real misconfiguration, so it is logged by the
    caller that can see the context (see `ServiceZone.save`).
    """
    if not code:
        return COUNTRIES[DEFAULT_COUNTRY_CODE]
    return COUNTRIES.get(str(code).upper().strip(),
                         COUNTRIES[DEFAULT_COUNTRY_CODE])


def is_supported(code):
    return str(code or '').upper().strip() in COUNTRIES


def supported_codes():
    return tuple(sorted(COUNTRIES))


def currency_for(code):
    return get_country(code).currency


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------

def normalize_phone(raw, default_country=DEFAULT_COUNTRY_CODE):
    """Turn whatever a human typed into E.164, or return None.

    Replaces three hardcoded `f"+91{phone_number}"` call sites. A US operator
    typing their own number into the console used to become `+915551234567`
    and could not sign in.

    Accepts: already-E.164 (`+15551234567`), a national number
    (`5551234567`), a number with the calling code but no plus
    (`15551234567`), and any of those with spaces, dashes, brackets or dots.

    Returns None rather than a guess when the digits do not match a known
    length for any supported country. A guess here is an account that cannot
    be found again.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    had_plus = s.startswith('+')
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not digits:
        return None

    default = get_country(default_country)

    # 1. Already E.164 with a plus: trust the country code in the string, but
    #    only if the total length matches a country we know. Otherwise it is
    #    either a typo or a country we do not serve, and both should fail
    #    loudly rather than be stored.
    if had_plus:
        for country in COUNTRIES.values():
            if (digits.startswith(country.calling_code)
                    and len(digits) in country.e164_length):
                return '+' + digits
        return None

    # 2. A bare national number for the default country.
    if len(digits) in default.national_number_lengths:
        return '+' + default.calling_code + digits

    # 3. Calling code without the plus, for the default country first so an
    #    ambiguous length resolves the way the caller expects.
    ordered = [default] + [c for c in COUNTRIES.values() if c.code != default.code]
    for country in ordered:
        if (digits.startswith(country.calling_code)
                and len(digits) in country.e164_length):
            return '+' + digits

    # 4. A national number for some OTHER supported country. Accepted, because
    #    an Indian operator legitimately onboards a US driver, and refusing
    #    would be worse than resolving it.
    for country in ordered:
        if len(digits) in country.national_number_lengths:
            return '+' + country.calling_code + digits

    return None


def country_of_phone(e164):
    """Which supported country does this E.164 number belong to? None if unknown."""
    if not e164:
        return None
    digits = ''.join(ch for ch in str(e164) if ch.isdigit())
    best = None
    for country in COUNTRIES.values():
        if (digits.startswith(country.calling_code)
                and len(digits) in country.e164_length):
            # Prefer the longest matching calling code, so a hypothetical
            # +1 / +12 overlap resolves to the more specific one.
            if best is None or len(country.calling_code) > len(best.calling_code):
                best = country
    return best
