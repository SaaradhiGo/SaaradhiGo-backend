"""Direct FCM push through firebase_admin: the sender `servers.auth_user.services` uses.

This is the OLD direct-push path (ride offers, SOS, safety), moved behind `integrations/` so no
business module imports the vendor SDK. BEHAVIOUR IS UNCHANGED: the app is initialised once, at
import of `servers.auth_user.services`, from FIREBASE_CREDENTIALS_JSON (or the credentials file),
and with no credentials push is disabled and `is_initialised()` is False. The notifications
module's `FcmPush` adapter shares this same app. Rewiring this path onto the notifications
pipeline is the separate app catch-up backend item.

The business layer sees only: `ensure_initialised()`, `is_initialised()`, `send()` and the typed
errors `InvalidTokenError` and `QuotaExceededError`.
"""

import json
import logging
import os

import firebase_admin
from django.conf import settings
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)


class InvalidTokenError(Exception):
    """The device token is permanently invalid (unregistered, or from another sender)."""


class QuotaExceededError(Exception):
    """FCM is throttling this project."""


def _firebase_credentials():
    """Service-account credentials, from the environment or a file.

    FIREBASE_CREDENTIALS_JSON holds the service-account JSON itself, which is what a platform
    with environment variables and no persistent filesystem can actually supply. The file path
    (FIREBASE_CREDENTIALS_PATH) still works for local development. Neither value is ever logged.
    """
    raw = (getattr(settings, 'FIREBASE_CREDENTIALS_JSON', '') or '').strip()
    if raw:
        try:
            return credentials.Certificate(json.loads(raw)), 'FIREBASE_CREDENTIALS_JSON'
        except Exception as exc:  # noqa: BLE001
            # Never log the value: it is a private key.
            logger.error(
                'FIREBASE_CREDENTIALS_JSON is set but unusable (%s): %s',
                type(exc).__name__, exc,
            )
            return None, None

    cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', '') or 'firebase-credentials.json'
    if not os.path.isabs(cred_path):
        cred_path = os.path.join(settings.BASE_DIR, cred_path)
    if os.path.exists(cred_path):
        return credentials.Certificate(cred_path), cred_path
    return None, None


def ensure_initialised():
    """Initialise the firebase_admin app if credentials exist. Never raises."""
    try:
        if not firebase_admin._apps:
            cred, source = _firebase_credentials()
            if cred is not None:
                firebase_admin.initialize_app(cred)
                logger.info('Firebase Admin initialized from %s.', source)
            else:
                logger.warning(
                    'No Firebase credentials. Push notifications are DISABLED. '
                    'Set FIREBASE_CREDENTIALS_JSON (the service-account JSON) or '
                    'place the file at FIREBASE_CREDENTIALS_PATH.'
                )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to initialize Firebase Admin: {str(e)}")


def is_initialised():
    return bool(firebase_admin._apps)


def send(token, title, body, data):
    """Send one push. Returns the FCM message id.

    Raises InvalidTokenError (clear the token), QuotaExceededError (retry later) or the
    provider's own exception for anything else (the caller treats that as transient).
    """
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data={str(k): str(v) for k, v in (data or {}).items()},
        token=token,
    )
    try:
        return messaging.send(message)
    except (messaging.UnregisteredError, messaging.SenderIdMismatchError) as exc:
        raise InvalidTokenError(type(exc).__name__) from exc
    except messaging.QuotaExceededError as exc:
        raise QuotaExceededError(type(exc).__name__) from exc
