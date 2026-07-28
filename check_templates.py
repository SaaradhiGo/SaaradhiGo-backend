import os, glob, traceback
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'base.settings')
import django
django.setup()
from django.template.loader import get_template

files = sorted(glob.glob('templates/admin_pages/*.html'))
had_error = False
for f in files:
    name = 'admin_pages/' + os.path.basename(f)
    try:
        get_template(name)
        print('OK  :', f)
    except Exception as e:
        had_error = True
        print('FAIL:', f, '->', e)

raise SystemExit(1 if had_error else 0)
