"""One HTML frame for every email (product standard, 17 Nov 2026).

Black header band with the wordmark, a white card on a light grey ground, 16px body text in the
standard's greys, one gold 48px button, 14px footnotes. Table-based with inline styles, because
email clients ignore stylesheets; fluid (max 560px), so it reads the same on a phone and a desktop.
Callers pass text; everything is escaped here.
"""
from html import escape

INK = '#121212'
TEXT_2 = '#4A4A4A'
TEXT_3 = '#6B6B6B'
BAND = '#F6F6F6'
LINE = '#E4E4E4'
GOLD = '#F5A623'
GOLD_LIGHT = '#F5C451'
FONT = "Inter, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"


def button(label, link):
    return (
        f'<a href="{escape(link, quote=True)}" style="display:inline-block;background:{GOLD};color:{INK};'
        f'font:600 16px/24px {FONT};padding:12px 20px;border-radius:12px;text-decoration:none;">{escape(label)}</a>'
    )


def frame(*, brand, title, body_html, footer_text=''):
    """The whole message around already-built (escaped) body HTML."""
    footer = (f'<p style="margin:16px 0 0;font:400 14px/20px {FONT};color:{TEXT_3};">{escape(footer_text)}</p>'
              if footer_text else '')
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:{BAND};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BAND};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#FFFFFF;border:1px solid {LINE};border-radius:16px;border-collapse:separate;overflow:hidden;">
<tr><td style="background:#000000;padding:16px 24px;border-radius:16px 16px 0 0;font:600 20px/28px {FONT};color:{GOLD_LIGHT};letter-spacing:0.02em;">{escape(brand)}</td></tr>
<tr><td style="padding:24px;font:400 16px/24px {FONT};color:{TEXT_2};">
<h1 style="margin:0 0 16px;font:600 20px/28px {FONT};color:{INK};">{escape(title)}</h1>
{body_html}
{footer}
<!--footer-->
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def paragraph(text, *, small=False):
    size = '14px/20px' if small else '16px/24px'
    colour = TEXT_3 if small else TEXT_2
    return f'<p style="margin:0 0 16px;font:400 {size} {FONT};color:{colour};">{escape(text)}</p>'


def action_email(*, brand, title, lines, button_label=None, link=None, notes=(), footer_text=''):
    """A message with one action: the lines, the button, the link spelled out, then the notes."""
    parts = [paragraph(x) for x in lines]
    if link and button_label:
        parts.append(f'<p style="margin:8px 0 24px;">{button(button_label, link)}</p>')
        parts.append(paragraph(f'Or open this link: {link}', small=True))
    parts += [paragraph(n, small=True) for n in notes]
    return frame(brand=brand, title=title, body_html='\n'.join(parts), footer_text=footer_text)
