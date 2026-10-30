"""The emergency-services integration point. No vendor, no network.

Referring an incident to the police is something an operator DOES (they ring
the number the console shows); the platform's job is to record that it
happened, who did it and when. `EmergencyServicesProvider` is the seam a
real integration would implement later. The only implementation is
`NullProvider`, which makes no network call and records nothing outside the
incident itself: the timeline entry and the audit row are written by the
caller (`services.refer_to_police`).
"""

from abc import ABC, abstractmethod


class EmergencyServicesProvider(ABC):
    name = 'abstract'

    @abstractmethod
    def refer(self, incident, *, actor, reference='', note=''):
        """Hand the incident to emergency services. Returns a dict that is
        stored on the timeline entry (no personal data)."""


class NullProvider(EmergencyServicesProvider):
    """Records the referral and does nothing else. Never touches a network."""

    name = 'null'

    def refer(self, incident, *, actor, reference='', note=''):
        return {'provider': self.name, 'delivered': False, 'reference': reference}


def get_provider():
    return NullProvider()
