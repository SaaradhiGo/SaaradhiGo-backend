import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'base.settings')
import django
django.setup()
from django.template.base import Lexer

src = open('templates/admin_pages/driver_onboarding.html', encoding='utf-8').read()
tokens = Lexer(src).tokenize()

stack = []
pairs = {'if': 'endif', 'for': 'endfor', 'block': 'endblock'}
for t in tokens:
    if t.token_type.name == 'BLOCK':
        content = t.contents.strip()
        parts = content.split()
        tag = parts[0] if parts else ''
        if tag in ('if', 'for', 'block'):
            stack.append((tag, t.lineno))
        elif tag in ('endif', 'endfor', 'endblock'):
            if not stack:
                print(f"Line {t.lineno}: EXTRA {tag} (stack empty)")
            else:
                opentag, openline = stack.pop()
                expected = pairs[opentag]
                if tag != expected:
                    print(f"MISMATCH Line {t.lineno}: {tag} expected {expected} (opened {opentag}@{openline})")
                else:
                    print(f"OK {opentag}@{openline}..{t.lineno}")
        elif tag in ('elif', 'else', 'empty'):
            print(f"  -- {tag}@{t.lineno} (in {stack[-1] if stack else 'NONE'})")

print("\nUnclosed:", stack)
