"""The report catalogue: what exists, who may open it, what columns it has.

WHO SEES WHICH REPORT
---------------------
The door to the whole module is `report.read` (Super Admin, City Ops, Finance, Marketing,
Analyst). Behind it, each report names the capability that may open it, held AT THE PLACE the
rows are in (`payments.scope.paths_for`, the same narrowing every Finance surface uses):

  operational    `report.read` alone: trips, supply, riders, retention, support, safety history,
                 fraud counts. Aggregates only, no person, no trip id, no coordinate.
  money          `finance.read` OR `finance.aggregate` (Finance, City Ops, Analyst, Super Admin):
                 revenue, payouts. Exactly the rule `payments.aggregates` states for the Command
                 Centre finance tiles: city totals, never one person's earnings.
  promotion cost `promo.read` OR a money capability: the promo / incentive / referral cost
                 report, which is what a campaign budget already shows. This is what Marketing
                 sees of the money (it holds neither finance capability, by design), plus GMV and trips
                 completed on that report so promo cost reads against GMV (AUDIT 27.59).

Support Agent, Support Lead, Safety Officer and KYC Verifier hold none of these: 403.
"""

from dataclasses import dataclass

REPORT_DOOR = 'report.read'
MONEY_GATE = ('finance.read', 'finance.aggregate')
PROMO_GATE = ('promo.read',) + MONEY_GATE
OPS_GATE = (REPORT_DOOR,)

GRANULARITIES = ('day', 'week', 'month')
GROUPS = ('none', 'vehicle', 'city')
MAX_RANGE_DAYS = 400
DEFAULT_RANGE_DAYS = 30

AREAS = ['Trips', 'Revenue', 'Supply', 'Riders', 'Safety & Support', 'Fraud']

COUNT, MONEY, PERCENT, SECONDS, HOURS, KM, MINUTES, TEXT = (
    'count', 'money', 'percent', 'seconds', 'hours', 'km', 'minutes', 'text')


def col(key, label, kind=COUNT):
    return {'key': key, 'label': label, 'type': kind}


@dataclass(frozen=True)
class Report:
    key: str
    area: str
    title: str
    description: str
    gate: tuple
    money: bool                      # rows carry a currency; totals are per currency
    groups: tuple                    # allowed group_by values
    default_group: str
    columns: tuple
    definitions: tuple = ()

    def as_card(self):
        return {'key': self.key, 'area': self.area, 'title': self.title,
                'description': self.description, 'money': self.money,
                'groups': list(self.groups), 'default_group': self.default_group}


TRIPS = Report(
    key='trips', area='Trips', title='Trips and cancellations',
    description='Requested, accepted and completed trips, who cancelled, no-driver-found, pickup ETA, distance and time.',
    gate=OPS_GATE, money=False, groups=('none', 'vehicle', 'city'), default_group='none',
    columns=(
        col('trips_requested', 'Requested'), col('trips_accepted', 'Accepted'),
        col('trips_completed', 'Completed'), col('cancelled_by_rider', 'Cancelled by rider'),
        col('cancelled_by_driver', 'Cancelled by driver'), col('cancelled_by_ops', 'Cancelled by ops'),
        col('no_driver_found', 'No driver found'), col('completion_rate', 'Completed per request', PERCENT),
        col('acceptance_rate', 'Accepted per request', PERCENT),
        col('avg_eta_seconds', 'Avg pickup ETA', SECONDS), col('eta_p50_seconds', 'Median ETA', SECONDS),
        col('eta_p90_seconds', 'P90 ETA', SECONDS), col('distance_km', 'Distance', KM),
        col('duration_min', 'Trip time', MINUTES),
    ),
    definitions=(
        'Requested, accepted and the cancellation causes are counted on the day the trip was requested; completed, distance and time on the day it completed.',
        'Acceptance is accepted / requested: ride offers are not stored, so a per-offer rate does not exist.',
        'No driver found: cancelled by the system (or unknown) before any driver accepted. Cancelled by ops: an admin cancel, or a system cancel after acceptance.',
        'Pickup ETA is reached minus accepted. The median and P90 are shown only for a row that maps to one city, one day and one vehicle class (percentiles cannot be added up).',
    ),
)

REVENUE = Report(
    key='revenue', area='Revenue', title='Revenue and take rate',
    description='GMV, what riders paid, commission, platform costs and take rate by vehicle class and period, per currency.',
    gate=MONEY_GATE, money=True, groups=('vehicle', 'none', 'city'), default_group='vehicle',
    columns=(
        col('trips_completed', 'Completed trips'), col('settled_trips', 'Settled trips'),
        col('gmv_minor', 'GMV (full fare)', MONEY), col('rider_paid_minor', 'Rider paid', MONEY),
        col('commission_minor', 'Commission', MONEY), col('take_rate', 'Take rate', PERCENT),
        col('driver_net_minor', 'Driver net', MONEY), col('promo_cost_minor', 'Promo cost', MONEY),
        col('platform_funded_trip_end_minor', 'Platform-funded trip ends', MONEY),
        col('regulatory_fee_expense_minor', 'Regulatory fee expense', MONEY),
        col('incentive_rewards_minor', 'Incentive rewards', MONEY),
        col('referral_rewards_minor', 'Referral rewards', MONEY),
        col('reward_recovery_minor', 'Reward recovery', MONEY), col('refunds_minor', 'Refunds', MONEY),
        col('platform_cost_minor', 'Platform cost', MONEY), col('net_take_minor', 'Net take', MONEY),
        col('avg_fare_minor', 'Average fare', MONEY), col('gmv_growth_percent', 'GMV vs previous period', PERCENT),
    ),
    definitions=(
        'GMV, commission and driver net come from the settlement of each trip (its current version, on the day it settled): the commission is the rate applied at settlement, not today\'s rate card.',
        'Rider paid is the charged fare (the discounted fare when a promo applied) of trips completed that day; it is the old executive_revenue "gross booking value".',
        'Promo cost comes from the promotions ledger; incentive and referral rewards, platform-funded trip ends and reward recovery from the wallet ledger; the regulatory fee from platform expenses; refunds from trip refunds.',
        'Platform cost = promo + platform-funded trip ends + regulatory fee + incentive + referral rewards - reward recovery. Net take = commission - platform cost. Refunds are shown, not netted.',
        'Money is never added across currencies: each currency has its own rows and totals.',
        'Rewards, recovery and a vehicle filter: rewards are not tied to a vehicle class, so they appear only under "Not vehicle-specific" and not at all when a vehicle class is chosen.',
    ),
)

PROMOTIONS = Report(
    key='promotions_cost', area='Revenue', title='Promotion and incentive cost',
    description='What the platform spent on rider promos, driver incentives and referrals, per currency.',
    gate=PROMO_GATE, money=True, groups=('none', 'city'), default_group='none',
    columns=(
        col('trips_completed', 'Completed trips'), col('gmv_minor', 'GMV (full fare)', MONEY),
        col('promo_cost_minor', 'Promo cost', MONEY),
        col('incentive_rewards_minor', 'Incentive rewards', MONEY),
        col('referral_rewards_minor', 'Referral rewards', MONEY),
        col('reward_recovery_minor', 'Reward recovery', MONEY),
        col('total_cost_minor', 'Net promotion cost', MONEY),
        col('cost_per_trip_minor', 'Cost per completed trip', MONEY),
        col('promo_cost_percent_of_gmv', 'Promo cost per GMV', PERCENT),
        col('total_cost_percent_of_gmv', 'Net promotion cost per GMV', PERCENT),
    ),
    definitions=(
        'Promo cost is the platform-funded discount in the promotions ledger; incentive and referral rewards are credits in the driver and rider wallet ledgers; recovery is the amount taken back after a reviewed fraud decision.',
        'Net promotion cost = promo + incentive + referral - recovery.',
        'GMV is the full fare of the trips settled that day, before any promo. It is shown here so promo cost can be read against it; commission, take rate and platform revenue stay in Revenue (Finance).',
    ),
)

PAYOUTS = Report(
    key='payouts', area='Revenue', title='Driver payouts',
    description='Payouts requested, paid and failed by the driver\'s home city, per currency.',
    gate=MONEY_GATE, money=True, groups=('none', 'city'), default_group='none',
    columns=(
        col('requested_count', 'Requested'), col('requested_minor', 'Requested amount', MONEY),
        col('paid_count', 'Paid'), col('paid_minor', 'Paid amount', MONEY),
        col('failed_count', 'Failed'), col('failed_minor', 'Failed amount', MONEY),
    ),
    definitions=(
        'A payout belongs to the driver\'s home city. Requested and failed are counted on the request day; paid on the day the payout transaction completed.',
        'City totals only: no driver, no payout reference, no account detail.',
    ),
)

SUPPLY = Report(
    key='supply', area='Supply', title='Supply and trips funnel',
    description='Active and online drivers, online hours, new approvals and the request-to-completion funnel.',
    gate=OPS_GATE, money=False, groups=('none', 'city'), default_group='none',
    columns=(
        col('active_drivers', 'Active drivers'), col('online_drivers', 'Online drivers'),
        col('online_hours', 'Online hours', HOURS), col('new_approved_drivers', 'New approved drivers'),
        col('trips_requested', 'Requested'), col('trips_accepted', 'Accepted'),
        col('trips_completed', 'Completed'), col('trips_per_active_driver', 'Trips per active driver', COUNT),
        col('utilisation', 'Time on trips per online hour', PERCENT),
    ),
    definitions=(
        'Active driver: at least one completed trip that day, by the driver\'s home city. Online driver and online hours come from the driver sessions written when a driver goes online and offline (clipped to the local day; a session still open counts up to now).',
        'New approved drivers counts approvals by the time the document review was last updated, so a later re-review moves the date.',
        'At week and month granularity, active and online drivers are the sum of the daily counts (driver-days), not distinct drivers.',
        'Utilisation = minutes of completed trips / minutes online. It is approximate: trips are placed by completion day, sessions by driver home city.',
    ),
)

RIDERS = Report(
    key='riders', area='Riders', title='Rider growth',
    description='New riders, active riders and first-trip riders by day, week or month.',
    gate=OPS_GATE, money=False, groups=('none', 'city'), default_group='none',
    columns=(
        col('new_riders', 'New riders'), col('active_riders', 'Active riders'),
        col('first_trip_riders', 'First-trip riders'),
    ),
    definitions=(
        'A rider is placed in their home city when one is set, else the city of their first trip; a rider with neither is in no city. Active: requested at least one trip in the city that day. First-trip: first ever completed trip, here.',
        'At week and month granularity, active riders is the sum of the daily counts, not distinct riders.',
    ),
)

RETENTION = Report(
    key='rider_retention', area='Riders', title='Rider retention by cohort',
    description='Weekly cohorts of riders by first completed trip, and the share who rode again in each following week.',
    gate=OPS_GATE, money=False, groups=('none',), default_group='none',
    columns=(),
    definitions=(
        'A cohort is the week (Monday, city-local) of a rider\'s first completed trip, in that city. Week 0 is the cohort week; week N counts riders with a completed trip N weeks later, anywhere.',
        'Cohorts are rebuilt nightly; the date range chooses which cohort weeks are shown.',
    ),
)

SUPPORT = Report(
    key='support', area='Safety & Support', title='Support tickets and SLA',
    description='Tickets opened and resolved, first-response time and SLA breaches.',
    gate=OPS_GATE, money=False, groups=('none', 'city'), default_group='none',
    columns=(
        col('opened', 'Opened'), col('resolved', 'Resolved'),
        col('first_response_breached', 'First-response breaches'),
        col('resolution_breached', 'Resolution breaches'),
        col('breach_rate', 'Tickets with a breach', PERCENT),
        col('avg_first_response_seconds', 'Avg first response', SECONDS),
    ),
    definitions=(
        'Opened and the breach counts are for tickets created that day (a ticket still open past its due time counts as breached as of the last rebuild); resolved is by resolution day.',
        'Placed by the ticket\'s trip city, else the driver\'s home city; a rider ticket with no trip is in no city.',
    ),
)

SAFETY = Report(
    key='safety', area='Safety & Support', title='Safety and SOS history',
    description='Incidents by severity and category, SOS counts and response time. History only: no live SOS, no trip, no location.',
    gate=OPS_GATE, money=False, groups=('none', 'city'), default_group='none',
    columns=(
        col('incidents', 'Incidents'), col('incidents_critical', 'Critical'),
        col('incidents_high', 'High'), col('incidents_medium', 'Medium'), col('incidents_low', 'Low'),
        col('sos_events', 'SOS events'), col('sos_responded', 'SOS acknowledged'),
        col('sos_avg_response_seconds', 'Avg time to acknowledge', SECONDS),
        col('sos_median_response_seconds', 'Median time to acknowledge', SECONDS),
    ),
    definitions=(
        'Counts and times only: this report holds no trip id, no coordinate and no person, so it is the same for every role that may open it.',
        'Response time is raised to first acknowledgement. The median is shown at day granularity (a median cannot be added up).',
    ),
)

FRAUD = Report(
    key='fraud', area='Fraud', title='Fraud flags',
    description='Flags raised, confirmed and dismissed by rule, counts only.',
    gate=OPS_GATE, money=False, groups=('none', 'city'), default_group='none',
    columns=(
        col('raised', 'Raised'), col('confirmed', 'Confirmed'), col('dismissed', 'Dismissed'),
        col('confirm_rate', 'Confirmed of decided', PERCENT),
    ),
    definitions=(
        'Raised is by the day the flag was raised; confirmed and dismissed by the day it was decided.',
        'Counts only: no subject, no evidence.',
    ),
)

REPORTS = {r.key: r for r in (
    TRIPS, REVENUE, PROMOTIONS, PAYOUTS, SUPPLY, RIDERS, RETENTION, SUPPORT, SAFETY, FRAUD)}
