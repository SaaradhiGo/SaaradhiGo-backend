"""Simulated dispatch eligibility for QA test drivers.

WHAT PROBLEM THIS SOLVES
------------------------
Certifying a ride end to end needs one driver dispatch will offer trips to.
The only way to get one was `Driver.approved = True`, which is the output of
a real MVA-2020 KYC review. Setting it for a test account would have recorded
a genuine regulatory approval over placeholder documents, and nothing in the
database could afterwards tell the two apart.

So eligibility is now TWO different things:

    approved                  -- a real human reviewed real documents.
                                 Only the console can set it. Untouched here.
    qa_simulated_eligibility  -- this is a test account and we are pretending,
                                 in QA, so the lifecycle can be exercised.

A QA test driver has the second and NOT the first. `doc_status` stays
`pending`, the console still shows KYC PENDING, and every compliance query
that looks at `approved` continues to give the honest answer.

WHY IT CANNOT LEAK INTO PRODUCTION
----------------------------------
`may_operate` grants the simulated path only when BOTH hold:

    settings.ENVIRONMENT == 'qa'      (exactly)
    settings.IS_PRODUCTION is False

`base/settings.py` defines ENVIRONMENT as
`os.environ.get('ENVIRONMENT', 'production' if not DEBUG else 'development')`,
so a deployment that simply does not set the variable is treated as
production and the flag grants nothing. The failure mode is "the test driver
stops working", never "a test driver takes a real ride".

There is no setting, flag or argument anywhere that relaxes this. A
production database carrying `qa_simulated_eligibility = True` on every row
would still dispatch to none of them.
"""

from django.conf import settings


def qa_simulation_allowed() -> bool:
    """Is simulated eligibility honoured in this environment at all?

    Fail-closed: anything other than an explicit QA environment is a no.
    """
    environment = (getattr(settings, 'ENVIRONMENT', '') or '').strip().lower()
    if environment != 'qa':
        return False
    # Second, independent read. If ENVIRONMENT ever grows a value that is
    # neither 'qa' nor 'production', the two must still agree before any
    # simulation is honoured.
    return not getattr(settings, 'IS_PRODUCTION', True)


def is_qa_test_driver(driver) -> bool:
    """True when this driver is a flagged QA test account.

    Independent of the environment, because the console must label such a
    driver wherever it is displayed -- including, and especially, somewhere
    the simulation is NOT honoured.
    """
    return bool(getattr(driver, 'qa_simulated_eligibility', False))


def may_operate(driver) -> bool:
    """May this driver go online, receive offers and accept trips?

    The single definition. It replaces bare `driver.approved` checks at the
    gates so that the simulated path exists in exactly one place and can be
    reasoned about, rather than being spelled out at each socket.
    """
    if driver is None:
        return False
    if getattr(driver, 'approved', False):
        return True
    return is_qa_test_driver(driver) and qa_simulation_allowed()


def eligibility_label(driver) -> str:
    """How this driver's eligibility should be described to an operator."""
    if getattr(driver, 'approved', False):
        return 'approved'
    if is_qa_test_driver(driver):
        return 'qa-simulated'
    return 'not-eligible'
