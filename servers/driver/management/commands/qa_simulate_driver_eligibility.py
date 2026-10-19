"""Give a QA test driver SIMULATED dispatch eligibility. Never an approval.

    python manage.py qa_simulate_driver_eligibility --driver-id 8
    python manage.py qa_simulate_driver_eligibility --driver-id 8 --revert

WHAT IT DOES AND DOES NOT DO
----------------------------
Sets `Driver.qa_simulated_eligibility`. It does NOT touch `approved`,
`doc_status` or `status`, so:

  * no placeholder document is ever recorded as verified;
  * the console still shows the driver as KYC PENDING, because it is;
  * every compliance query that reads `approved` keeps telling the truth;
  * the real approval workflow is unchanged and still the only way to
    approve a driver.

REFUSES UNLESS ALL FOUR HOLD
----------------------------
  1. settings.ENVIRONMENT == 'qa', exactly. That setting defaults to
     'production' when the variable is absent, so an unconfigured
     deployment is refused rather than permitted.
  2. settings.IS_PRODUCTION is false -- an independent second read.
  3. The target's phone number is inside QA_TEST_DRIVER_PHONE_PREFIXES, so
     even inside QA this cannot touch an account that was not provisioned
     for testing.
  4. The driver has an active vehicle, because a driver the app will not let
     go online is not made dispatchable by a flag.

There is no --force and no override. Even if the flag were somehow set in
production, `qa_eligibility.may_operate` would ignore it there.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

# Ranges used by bootstrap_qa_accounts and the QA fixtures. A real subscriber
# number is never inside them.
QA_TEST_DRIVER_PHONE_PREFIXES = ('+9177700010', '+9199990000')

AUDIT_ACTION = 'qa_simulated_driver_eligibility'


class Command(BaseCommand):
    help = (
        'QA ONLY. Grant a designated QA test driver SIMULATED dispatch '
        'eligibility so the ride lifecycle can be certified. Not a KYC '
        'approval; refuses outside a QA environment.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--driver-id', type=int, required=True)
        parser.add_argument(
            '--revert', action='store_true',
            help='Remove simulated eligibility again.',
        )

    def _refuse_outside_qa(self):
        from servers.driver.qa_eligibility import qa_simulation_allowed

        environment = (getattr(settings, 'ENVIRONMENT', '') or '').strip().lower()
        if not qa_simulation_allowed():
            raise CommandError(
                f'refusing to run: ENVIRONMENT is {environment!r} and '
                f'IS_PRODUCTION is {getattr(settings, "IS_PRODUCTION", True)!r}. '
                f'Simulated eligibility exists only for an explicit QA '
                f'environment. Note ENVIRONMENT defaults to "production" when '
                f'unset, which is why an unconfigured deployment lands here.'
            )

    def handle(self, *args, **options):
        self._refuse_outside_qa()

        from servers.admin_audit.models import AdminAuditLog
        from servers.driver.models import Driver

        driver_id = options['driver_id']
        try:
            driver = Driver.objects.select_related('user_id').get(id=driver_id)
        except Driver.DoesNotExist:
            raise CommandError(f'no driver with id {driver_id}')

        phone = (getattr(driver.user_id, 'phone_number', '') or '').strip()
        if not phone.startswith(QA_TEST_DRIVER_PHONE_PREFIXES):
            raise CommandError(
                f'refusing: driver {driver_id} is not a QA test account. Its '
                f'number is outside {list(QA_TEST_DRIVER_PHONE_PREFIXES)}, so '
                f'it is, or could be, a real driver. Real drivers are approved '
                f'in the console after a real document review.'
            )

        revert = options['revert']

        if not revert and driver.active_vehicle_id is None:
            raise CommandError(
                f'refusing: driver {driver_id} has no active vehicle. The '
                f'driver app will not go online without one, so a flag here '
                f'would produce a driver that looks dispatchable and is not.'
            )

        before = {
            'qa_simulated_eligibility': driver.qa_simulated_eligibility,
            'approved': driver.approved,
            'doc_status': driver.doc_status,
        }

        driver.qa_simulated_eligibility = not revert
        # Deliberately NOT touching approved, doc_status or status.
        driver.save(update_fields=['qa_simulated_eligibility'])

        note = (
            'QA test driver given SIMULATED dispatch eligibility. This is NOT '
            'a KYC approval: no document was verified, the documents on this '
            'account are QA placeholders, and `approved` and `doc_status` are '
            'unchanged. Honoured only in a QA environment.'
            if not revert else
            'Simulated eligibility removed from QA test driver.'
        )

        AdminAuditLog.objects.create(
            actor=None,
            actor_label='qa_simulate_driver_eligibility (management command)',
            action=AUDIT_ACTION,
            target_type='driver',
            target_id=str(driver.id),
            before=before,
            after={
                'qa_simulated_eligibility': driver.qa_simulated_eligibility,
                'approved': driver.approved,
                'doc_status': driver.doc_status,
            },
            reason=note,
        )

        driver.doc_status_updated_at = driver.doc_status_updated_at or timezone.now()

        self.stdout.write(self.style.WARNING(
            f'driver {driver.id}: qa_simulated_eligibility='
            f'{driver.qa_simulated_eligibility} '
            f'approved={driver.approved} doc_status={driver.doc_status!r}'
        ))
        self.stdout.write(self.style.WARNING(note))
