"""What a detection rule is.

A rule is a PURE reader over data that already exists: `evaluate(thresholds, country, now)` returns
the hits it finds in a bounded recent window and writes nothing. `fraud.raising` turns a hit into a
flag (or appends it to the open one), so a rule that runs twice over the same data changes nothing
the second time. A rule never acts: it cannot suspend, hold, claw back, notify or touch money.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable


@dataclass(frozen=True)
class Param:
    """One threshold: its bounds (to catch a typo, not to set policy) and the seeded defaults."""

    key: str
    label: str
    unit: str
    low: int
    high: int
    default_in: int
    default_us: int
    hint: str = ''


@dataclass
class Hit:
    """One finding. `refs` are ids only: trip_ids, campaign_ids, counterpart_user_ids."""

    subject: object                       # the user the evidence is about
    severity: str
    score: int
    signals: list
    refs: dict = field(default_factory=dict)
    measures: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    key: str
    title: str
    summary: str
    #: What it reads, and what the platform does not store that would sharpen it. Shown on the
    #: Rules tab so a reviewer knows what a flag can and cannot prove.
    data_used: str
    data_missing: str
    params: tuple
    evaluate: Callable
    #: Who the subject is: 'rider', 'driver' or 'either'.
    subject_role: str = 'either'

    def defaults(self, country):
        return {p.key: (p.default_us if country == 'US' else p.default_in) for p in self.params}

    def param(self, key):
        return next((p for p in self.params if p.key == key), None)


def window_start(now, days=0, hours=0):
    return now - timedelta(days=days, hours=hours)


def clamp_score(value):
    return max(0, min(100, int(round(value))))


def severity_for(score, high=70, medium=40):
    from servers.fraud.models import HIGH, LOW, MEDIUM

    return HIGH if score >= high else MEDIUM if score >= medium else LOW


def country_matches(user, country):
    """Is `user` in `country`? Rules run per country, with that country's numbers."""
    from servers.notifications import recipients

    return recipients.country_of(user) == country


def completed_status_id():
    from servers.ride.models import TripStatus

    return TripStatus.objects.filter(status_code='completed').values_list('id', flat=True).first()


def cancelled_status_id():
    from servers.ride.models import TripStatus

    return TripStatus.objects.filter(status_code='cancelled').values_list('id', flat=True).first()
