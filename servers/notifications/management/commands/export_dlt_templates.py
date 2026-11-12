"""Write the India SMS templates Srini submits for DLT registration (13 Nov).

    python manage.py export_dlt_templates --out approval-kit/dlt-templates.csv

One row per SMS: the message type, the exact text with DLT variables ({#var#}, a link {#url#}),
the DLT category, a filled-in sample and the purpose. Two sources: base/sms_templates.py (sign-in
codes, shares, SOS, pages) and the notifications pipeline's India SMS templates (seed_data.py).
"""
import csv
import sys

from django.core.management.base import BaseCommand

from base import sms_templates
from servers.notifications import seed_data

#: Pipeline templates by key: DLT category (all are service-implicit: account and trip notices).
_PIPELINE_CATEGORY = {seed_data.PROMO_AVAILABLE: 'promotional'}


def rows():
    out = []
    for kind, t in sms_templates.TEMPLATES.items():
        out.append({'type': kind, 'text': sms_templates.dlt_text(kind), 'category': t.category,
                    'sample': sms_templates.render(kind, **t.sample), 'purpose': t.purpose})
    for key, channel, country, copy, _active in seed_data.seed_rows():
        if channel != 'sms' or country != 'IN':
            continue
        body = copy['body']
        out.append({'type': key, 'text': seed_data.dlt_text(body),
                    'category': _PIPELINE_CATEGORY.get(key, 'service'),
                    'sample': body.format(**copy.get('sample', {})) if copy.get('sample') else body,
                    'purpose': copy.get('title') or key})
    return out


class Command(BaseCommand):
    help = 'Write the DLT template list (CSV) for India SMS registration.'

    def add_arguments(self, parser):
        parser.add_argument('--out', default='', help='file path; stdout when omitted')

    def handle(self, *args, **options):
        fields = ['type', 'text', 'category', 'sample', 'purpose']
        if options['out']:
            with open(options['out'], 'w', newline='', encoding='utf-8') as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows())
            self.stdout.write(f"wrote {len(rows())} templates to {options['out']}")
        else:
            writer = csv.DictWriter(sys.stdout, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows())
