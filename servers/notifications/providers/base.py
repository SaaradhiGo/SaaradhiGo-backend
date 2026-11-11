"""The provider interface: one `send()` and one `parse_webhook()` per adapter.

    send(outbound)             -> SendResult(provider_message_id, status)
    verify_webhook(request)    -> bool        (staticmethod: needs no credentials object)
    parse_webhook(request)     -> [WebhookEvent]

Adapters raise `TransientProviderError` for anything worth retrying (timeouts, 429, 5xx,
throttling) and `PermanentProviderError` for anything that will not get better (a malformed
number, an unverified SES recipient, a rejected template). The dispatcher retries the first
with backoff up to the attempt limit and fails the second at once.

NO ADAPTER IS EVER CONSTRUCTED UNLESS `config.live_send_enabled()`: see `providers.build`.
Tests mock every HTTP and SDK call; nothing here is exercised against a real provider.
"""

from dataclasses import dataclass, field


class ProviderError(Exception):
    """Base of the adapter errors. The message is stored (masked) on the message row."""


class TransientProviderError(ProviderError):
    pass


class PermanentProviderError(ProviderError):
    pass


class LiveSendDisabled(Exception):
    """A live adapter was asked for while NOTIFY_LIVE_SEND is off. Always a defect."""


@dataclass
class Outbound:
    """Everything an adapter needs to send one message. Built at send time, never stored."""

    message_id: int
    channel: str
    category: str
    country: str
    user_id: int
    to_phone: str = ''
    to_email: str = ''
    push_token: str = ''
    subject: str = ''
    title: str = ''
    body: str = ''
    #: Ordered slot values (registered-template providers).
    values: list = field(default_factory=list)
    #: sender_id, dlt_template_id, entity_id, provider_template_id, whatsapp_* (may be empty).
    registration: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)


@dataclass
class SendResult:
    provider_message_id: str
    status: str = 'sent'


@dataclass
class WebhookEvent:
    """One thing a provider told us.

    kind: `status` (a delivery state of a message we sent), `stop` (a recipient replied STOP
    to an SMS), `bounce` / `complaint` (SES: the address is bad or the recipient complained).
    """

    kind: str
    provider_message_id: str = ''
    status: str = ''
    detail: str = ''
    phone: str = ''
    email: str = ''


class Provider:
    name = ''
    #: Channels this adapter can carry.
    channels = ()
    #: False for the stub and the in-app writer: nothing leaves the platform.
    external = True

    def send(self, outbound):  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def verify_webhook(request):
        return False

    @staticmethod
    def parse_webhook(request):
        return []


def mask_for_log(text):
    """Remove anything shaped like a phone number or email from a string headed for a log."""
    import re

    text = re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+', '[email]', str(text or ''))
    text = re.sub(r'\+?\d[\d\s-]{7,}\d', '[number]', text)
    return text[:500]
