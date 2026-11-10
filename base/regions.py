"""States per country for driver addresses, licences and plates (10 Nov).

India: ISO 3166-2:IN codes with the first two digits of their PIN codes (postal circles; a
state that shares a circle accepts both). USA: the 50 states and DC; a ZIP is checked for shape
only.
"""
import re

IN_STATES = {
    'AN': ('Andaman and Nicobar Islands', ('74',)), 'AP': ('Andhra Pradesh', ('51', '52', '53')),
    'AR': ('Arunachal Pradesh', ('79',)), 'AS': ('Assam', ('78',)), 'BR': ('Bihar', ('80', '81', '82', '84', '85')),
    'CH': ('Chandigarh', ('16',)), 'CG': ('Chhattisgarh', ('49',)),
    'DH': ('Dadra and Nagar Haveli and Daman and Diu', ('39',)), 'DL': ('Delhi', ('11',)),
    'GA': ('Goa', ('40',)), 'GJ': ('Gujarat', ('36', '37', '38', '39')), 'HR': ('Haryana', ('12', '13')),
    'HP': ('Himachal Pradesh', ('17',)), 'JK': ('Jammu and Kashmir', ('18', '19')),
    'JH': ('Jharkhand', ('81', '82', '83')), 'KA': ('Karnataka', ('56', '57', '58', '59')),
    'KL': ('Kerala', ('67', '68', '69')), 'LA': ('Ladakh', ('19',)), 'LD': ('Lakshadweep', ('68',)),
    'MP': ('Madhya Pradesh', ('45', '46', '47', '48')), 'MH': ('Maharashtra', ('40', '41', '42', '43', '44')),
    'MN': ('Manipur', ('79',)), 'ML': ('Meghalaya', ('79',)), 'MZ': ('Mizoram', ('79',)),
    'NL': ('Nagaland', ('79',)), 'OD': ('Odisha', ('75', '76', '77')), 'PY': ('Puducherry', ('53', '60', '67')),
    'PB': ('Punjab', ('14', '15', '16')), 'RJ': ('Rajasthan', ('30', '31', '32', '33', '34')),
    'SK': ('Sikkim', ('73',)), 'TN': ('Tamil Nadu', ('60', '61', '62', '63', '64')), 'TG': ('Telangana', ('50',)),
    'TR': ('Tripura', ('79',)), 'UP': ('Uttar Pradesh', ('20', '21', '22', '23', '24', '25', '26', '27', '28')),
    'UK': ('Uttarakhand', ('24', '26')), 'WB': ('West Bengal', ('70', '71', '72', '73', '74')),
}

US_STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California', 'CO': 'Colorado',
    'CT': 'Connecticut', 'DE': 'Delaware', 'DC': 'District of Columbia', 'FL': 'Florida', 'GA': 'Georgia',
    'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland', 'MA': 'Massachusetts',
    'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana',
    'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota',
    'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}


def states(country):
    """`[{code, name}]`, sorted by name, for a dropdown."""
    if country == 'IN':
        rows = [(c, n) for c, (n, _) in IN_STATES.items()]
    elif country == 'US':
        rows = list(US_STATES.items())
    else:
        rows = []
    return [{'code': c, 'name': n} for c, n in sorted(rows, key=lambda r: r[1])]


def is_state(country, code):
    return code in (IN_STATES if country == 'IN' else US_STATES if country == 'US' else {})


def postal_problem(country, state_code, postal_code):
    """None when the PIN/ZIP fits the country (and, in India, the state); else the reason."""
    code = (postal_code or '').strip()
    if country == 'IN':
        if not re.fullmatch(r'[1-9]\d{5}', code):
            return 'Enter a 6-digit PIN code.'
        prefixes = IN_STATES.get(state_code, (None, ()))[1]
        if prefixes and code[:2] not in prefixes:
            return f'That PIN code is not in {IN_STATES[state_code][0]}.'
        return None
    if country == 'US':
        return None if re.fullmatch(r'\d{5}', code) else 'Enter a 5-digit ZIP code.'
    return None


# India: two letters (state), 1-2 digits (RTO), 0-3 letters (series), 1-4 digits; or the BH series.
_IN_PLATE = re.compile(r'^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{1,4}$')
_IN_BH = re.compile(r'^\d{2}BH\d{4}[A-Z]{1,2}$')


def plate_problem(country, plate):
    """None when the number plate fits the country's format; else the reason."""
    p = re.sub(r'[\s\-.]', '', (plate or '').upper())
    if not p:
        return 'Enter the number plate.'
    if country == 'IN':
        if _IN_BH.match(p):
            return None
        if not _IN_PLATE.match(p) or p[:2] not in IN_STATES and p[:2] not in ('TS', 'OR', 'UA', 'CT'):
            return 'Enter an Indian number plate, like TS09AB1234.'
        return None
    if country == 'US':
        return None if re.fullmatch(r'[A-Z0-9]{1,8}', p) else 'Enter the plate as shown, up to 8 letters and numbers.'
    return None
