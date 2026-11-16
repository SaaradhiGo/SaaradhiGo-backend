"""US English for US riders and drivers (owner, 14 Nov); India keeps UK English.

The same word list as the web's `src/i18n/usEnglish.ts`. Used on text the apps show as-is (the
driver's checklist labels). Whole words only; the case is kept. Never run on codes or data.
"""

import re

WORDS = {
    'licence': 'license', 'licences': 'licenses', 'licenced': 'licensed',
    'cancelled': 'canceled', 'cancelling': 'canceling',
    'colour': 'color', 'colours': 'colors', 'coloured': 'colored',
    'centre': 'center', 'centres': 'centers', 'centred': 'centered',
    'labelled': 'labeled', 'labelling': 'labeling',
    'catalogue': 'catalog', 'catalogues': 'catalogs',
    'honour': 'honor', 'honours': 'honors', 'honoured': 'honored', 'honouring': 'honoring',
    'favour': 'favor', 'favours': 'favors', 'favourite': 'favorite', 'favourites': 'favorites',
    'behaviour': 'behavior', 'behaviours': 'behaviors',
    'neighbour': 'neighbor', 'neighbours': 'neighbors', 'neighbourhood': 'neighborhood',
    'grey': 'gray', 'fulfil': 'fulfill', 'fulfils': 'fulfills', 'fulfilment': 'fulfillment',
    'enrol': 'enroll', 'enrols': 'enrolls', 'enrolment': 'enrollment',
    'travelled': 'traveled', 'travelling': 'traveling',
    'kerb': 'curb', 'tyre': 'tire', 'tyres': 'tires',
    'programme': 'program', 'kilometre': 'kilometer', 'kilometres': 'kilometers',
    'metre': 'meter', 'metres': 'meters', 'defence': 'defense', 'offence': 'offense',
    'enquiry': 'inquiry', 'enquiries': 'inquiries', 'cheque': 'check',
    'analyse': 'analyze', 'analysed': 'analyzed', 'practise': 'practice',
}
for _stem in ('authoris', 'recognis', 'organis', 'apologis', 'realis', 'customis', 'personalis',
              'prioritis', 'summaris', 'finalis', 'minimis', 'maximis', 'optimis', 'utilis',
              'itemis', 'emphasis', 'standardis', 'categoris', 'specialis'):
    for _end in ('e', 'ed', 'es', 'ing', 'ation', 'ations'):
        WORDS[_stem + _end] = _stem[:-1] + 'z' + _end

_PATTERN = re.compile(r'\b(' + '|'.join(sorted(WORDS, key=len, reverse=True)) + r')\b', re.IGNORECASE)


def _keep_case(original, us):
    if original.isupper():
        return us.upper()
    if original[0].isupper():
        return us[0].upper() + us[1:]
    return us


def us_english(text):
    """`text` respelled in US English."""
    if not text:
        return text
    return _PATTERN.sub(lambda m: _keep_case(m.group(0), WORDS[m.group(0).lower()]), text)
