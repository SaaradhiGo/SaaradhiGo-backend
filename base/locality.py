"""The locality of an address, for lists: "Banjara Hills", not the street.

Lists in the console show where a trip went at locality level ("Banjara Hills
→ Madhapur"); the full address is behind the logged reveal (owner,
2026-11-03). Trips store the address the rider picked as one string, so the
locality is read from it rather than from a second geocoding call:

    "Road No. 3, Banjara Hills, Hyderabad, Telangana 500034, India"
        -> the part before the city's name: "Banjara Hills"
    "Inorbit Mall, Madhapur"   (no city in it)
        -> the last part that is not a postcode, state or country: "Madhapur"

It is derived from the stored address only. When nothing usable is left it
returns None and the console shows nothing, never a guess.
"""

import re

_HAS_DIGIT = re.compile(r'\d')
#: Trailing parts that are not a locality. Lower case.
_NOT_LOCALITY = {
    'india', 'united states', 'usa', 'us', 'united states of america', 'canada',
    'united kingdom', 'uk', 'australia',
    'telangana', 'andhra pradesh', 'karnataka', 'maharashtra', 'tamil nadu', 'kerala',
    'delhi', 'gujarat', 'rajasthan', 'west bengal', 'uttar pradesh', 'texas',
    'california', 'ontario', 'england', 'new south wales',
}
_MAX = 40


def _clean(part):
    return re.sub(r'\s+', ' ', part).strip()


def locality(address, city_name=None):
    if not address:
        return None
    parts = [_clean(p) for p in str(address).split(',') if _clean(p)]
    if not parts:
        return None
    city = (city_name or '').strip().lower()
    if city:
        for i, part in enumerate(parts):
            if part.lower() == city or part.lower().startswith(city + ' '):
                for prev in reversed(parts[:i]):
                    if not _HAS_DIGIT.search(prev):
                        return prev[:_MAX]
                return None
    useful = [p for p in parts
              if p.lower() not in _NOT_LOCALITY and not _HAS_DIGIT.search(p)
              and (not city or p.lower() != city)]
    if not useful:
        return None
    return useful[-1][:_MAX]
