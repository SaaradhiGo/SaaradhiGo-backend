"""Template placeholders: validation, rendering, and the India DLT slot mapping.

Placeholders are `{name}` (lowercase letters, digits, underscore). DLT-approved text uses
`{#var#}` slots instead. For an India SMS the two must line up 1:1: replace every `{name}` in
the body by `{#var#}` and the result must equal the registered DLT text, which also means the
slot COUNT and ORDER match and the fixed words are exactly the approved ones. A carrier
rejects a message whose fixed text drifts from what was registered, so the console refuses it
before it can be activated.
"""

import re

PLACEHOLDER = re.compile(r'\{([a-z][a-z0-9_]*)\}')
DLT_SLOT = '{#var#}'
MAX_VALUE_LENGTH = 300
MAX_BODY_LENGTH = 2000


class MissingVariable(Exception):
    def __init__(self, names):
        super().__init__(f'missing template variables: {", ".join(sorted(names))}')
        self.names = sorted(names)


def placeholders(text):
    """Placeholder names in order of appearance (repeats kept)."""
    return PLACEHOLDER.findall(text or '')


def validate(*, body, subject='', title='', variables=(), personal_variables=()):
    """Problems with a template's copy, as plain-English strings (empty list = fine)."""
    problems = []
    declared = list(variables or [])
    if len(set(declared)) != len(declared):
        problems.append('Declared variables contain duplicates.')
    for name in declared:
        if not re.fullmatch(r'[a-z][a-z0-9_]*', str(name)):
            problems.append(
                f'Variable name {name!r} must be lowercase letters, digits and underscores.')
    if not (body or '').strip():
        problems.append('The body is empty.')
    if len(body or '') > MAX_BODY_LENGTH:
        problems.append(f'The body is longer than {MAX_BODY_LENGTH} characters.')
    for label, text in (('body', body), ('subject', subject), ('title', title)):
        if DLT_SLOT in (text or ''):
            problems.append(
                f'The {label} uses {DLT_SLOT}; write the named placeholder, for example {{name}}.')
        undeclared = sorted(set(placeholders(text)) - set(declared))
        if undeclared:
            problems.append(f'The {label} uses undeclared variables: {", ".join(undeclared)}.')
        leftover = PLACEHOLDER.sub('', (text or '').replace(DLT_SLOT, ''))
        if re.search(r'[{}]', leftover):
            problems.append(f'The {label} has a stray brace or a malformed placeholder.')
    unused = sorted(set(declared) - set(placeholders(body)) - set(placeholders(subject))
                    - set(placeholders(title)))
    if unused:
        problems.append(f'Declared but never used: {", ".join(unused)}.')
    stray = sorted(set(personal_variables or []) - set(declared))
    if stray:
        problems.append(f'Personal variables must be declared variables: {", ".join(stray)}.')
    return problems


def render(text, values):
    """Substitute `{name}`. A missing variable raises; extras are ignored."""
    values = values or {}
    missing = [n for n in set(placeholders(text)) if n not in values or values[n] is None]
    if missing:
        raise MissingVariable(missing)

    def sub(match):
        return str(values[match.group(1)])[:MAX_VALUE_LENGTH]

    return PLACEHOLDER.sub(sub, text or '')


def ordered_values(body, values):
    """The value for each placeholder occurrence in `body`, in order (the provider slots)."""
    return [str((values or {}).get(name, ''))[:MAX_VALUE_LENGTH] for name in placeholders(body)]


def _squash(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def dlt_slot_count(registered_text):
    return (registered_text or '').count(DLT_SLOT)


def dlt_mapping_problems(body, registered_text):
    """Why `body` cannot be sent under the registered DLT text (empty list = it maps 1:1)."""
    if not _squash(registered_text):
        return ['No DLT-approved text is recorded for this template.']
    problems = []
    in_body = placeholders(body)
    slots = dlt_slot_count(registered_text)
    if len(in_body) != slots:
        problems.append(
            f'The body has {len(in_body)} placeholder(s) but the DLT text has {slots} '
            f'{DLT_SLOT} slot(s); they must map one to one, in order.')
    as_slots = _squash(PLACEHOLDER.sub(DLT_SLOT, body or ''))
    if as_slots != _squash(registered_text):
        problems.append(
            'The body does not match the DLT-approved text once placeholders are replaced by '
            f'{DLT_SLOT}. The fixed words must be exactly what was registered.')
    return problems
