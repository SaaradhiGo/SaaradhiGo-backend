"""The standard email frame (17 Nov 2026): escaped text, one button, nothing under 14px."""
import re

from base.email_layout import action_email


def test_action_email_escapes_and_has_one_button():
    html = action_email(brand='SaaradhiGo', title='Reset <your> password', lines=['Hi <b>you</b>'],
                        button_label='Set a new password', link='https://x.test/reset?uid=1&token=a"b', notes=['Once.'])
    assert '&lt;your&gt;' in html and '&lt;b&gt;you&lt;/b&gt;' in html
    assert html.count('<a href=') == 1 and 'token=a&quot;b' in html
    assert '<!--footer-->' in html


def test_no_text_under_14px():
    html = action_email(brand='SaaradhiGo', title='T', lines=['a'], button_label='Go', link='https://x.test', notes=['n'])
    sizes = [int(x) for x in re.findall(r'(\d+)px/\d+px', html)] + [int(x) for x in re.findall(r'font-size:\s*(\d+)px', html)]
    assert sizes and min(sizes) >= 14
