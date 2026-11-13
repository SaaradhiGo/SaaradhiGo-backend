"""A short label for a trip's place (13 Nov): the place name plus its area, e.g.
"Charminar, Ghansi Bazaar" from "Charminar, Charminar Road, Char Kaman, Ghansi Bazaar, Hyderabad,
Telangana, India". Lists and cards show the label; the full address stays for the trip detail.

Google's formatted addresses read from the most specific part to the country. The label keeps the
first part (the place) and the part just before the city (the area), and drops the city, the
state with its PIN or ZIP code, and the country.
"""

import re

COUNTRIES = {'india', 'usa', 'united states', 'united states of america', 'us'}
STATES = {
    'andhra pradesh', 'telangana', 'karnataka', 'tamil nadu', 'kerala', 'maharashtra', 'delhi',
    'gujarat', 'rajasthan', 'west bengal', 'uttar pradesh', 'odisha', 'goa', 'punjab', 'haryana',
}
MAX = 80


def _is_region(part):
    p = part.lower().strip()
    if p in COUNTRIES or p in STATES:
        return True
    # "Telangana 500032", "NY 10001", "500032", "CO 80202-1234"
    return bool(re.fullmatch(r'([a-z .]+ )?\d{5,6}(-\d{4})?', p)) or bool(re.fullmatch(r'[a-z]{2} \d{5}(-\d{4})?', p))


def short_place(address):
    """The place name plus its area, or '' for no address."""
    parts = [p.strip() for p in (address or '').split(',') if p.strip()]
    if not parts:
        return ''
    while len(parts) > 1 and _is_region(parts[-1]):
        parts.pop()
    name = parts[0]
    if len(parts) >= 3:
        area = parts[-2]  # parts[-1] is now the city
    elif len(parts) == 2:
        area = parts[1]
    else:
        area = ''
    label = f'{name}, {area}' if area and area.lower() != name.lower() else name
    return label[:MAX]
