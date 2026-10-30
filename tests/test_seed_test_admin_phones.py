"""The seed's QA test logins keep the phone they were first created with.

`seed_admin_access --test-admins` derives each login's phone from its
position in `TEST_ADMINS`. QA already holds the logins seeded so far, so an
entry inserted mid-list hands the next login a phone that belongs to another
one and the re-run stops on a duplicate username (found on QA after the
Support Lead template was inserted).
"""

from servers.admin_access.management.commands.seed_admin_access import TEST_ADMINS

#: As first seeded on QA. New logins go after these.
SEEDED_ON_QA = [
    'qa.cityops@saaradhigo.test',
    'qa.support@saaradhigo.test',
    'qa.safety@saaradhigo.test',
    'qa.kyc@saaradhigo.test',
    'qa.finance@saaradhigo.test',
    'qa.marketing@saaradhigo.test',
    'qa.analyst@saaradhigo.test',
]


def test_logins_already_on_qa_keep_their_positions():
    emails = [email for email, _role, _scope in TEST_ADMINS]
    assert emails[:len(SEEDED_ON_QA)] == SEEDED_ON_QA


def test_every_test_login_has_its_own_email():
    emails = [email for email, _role, _scope in TEST_ADMINS]
    assert len(emails) == len(set(emails))
