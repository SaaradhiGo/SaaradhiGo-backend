"""FCM: push for Android and, through FCM, for iOS (APNs).

Uses the firebase_admin app that `integrations.push.fcm_direct` initialises from
FIREBASE_CREDENTIALS_JSON (or the credentials file). With no credentials there is no app and
`FcmPush` refuses to construct: push is disabled, which is the state on QA. Tests patch
`firebase_admin.messaging.send`; nothing here is exercised against Google.

FCM has no delivery webhook. A push is `sent` once FCM accepts it; per-device delivery is not
reported back, so the log shows `sent` for push and never `delivered`.
"""

import logging

from servers.notifications.providers.base import PermanentProviderError, Provider, SendResult, TransientProviderError

logger = logging.getLogger(__name__)


class FcmPush(Provider):
    name = 'fcm'
    channels = ('push',)

    def __init__(self):
        import firebase_admin

        from integrations.push import fcm_direct

        # The app the direct-push path uses; initialised here if nothing has yet.
        fcm_direct.ensure_initialised()

        if not firebase_admin._apps:
            raise PermanentProviderError('Firebase is not initialised: push is disabled.')

    def send(self, outbound):
        from firebase_admin import exceptions as fb_exceptions
        from firebase_admin import messaging

        if not outbound.push_token:
            raise PermanentProviderError('The recipient has no push token.')
        message = messaging.Message(
            token=outbound.push_token,
            notification=messaging.Notification(title=outbound.title or None, body=outbound.body),
            data={str(k): str(v) for k, v in (outbound.data or {}).items()
                  if k not in ('variable_names',)} | {
                      'notification_message_id': str(outbound.message_id),
                      'category': outbound.category},
        )
        try:
            message_id = messaging.send(message)
        except (messaging.UnregisteredError, messaging.SenderIdMismatchError):
            raise PermanentProviderError('The push token is no longer valid.') from None
        except (fb_exceptions.UnavailableError, fb_exceptions.InternalError,
                fb_exceptions.DeadlineExceededError, fb_exceptions.ResourceExhaustedError) as exc:
            raise TransientProviderError(f'FCM unavailable: {type(exc).__name__}') from None
        except (fb_exceptions.FirebaseError, ValueError) as exc:
            raise PermanentProviderError(f'FCM refused the message: {type(exc).__name__}') from None
        return SendResult(provider_message_id=str(message_id), status='sent')
