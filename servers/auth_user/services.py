"""Auth-side service helpers (FCM push, etc.).

`send_push_notification` is the canonical sync entry point — it enqueues
a Celery task and returns immediately. The actual FCM round-trip runs in
the worker so request/WS handlers don't block on Google's latency.
"""

import os
import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)


# Initialize Firebase on module load if credentials exist.
try:
    if not firebase_admin._apps:
        cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', 'firebase-credentials.json')
        if not os.path.isabs(cred_path):
            cred_path = os.path.join(settings.BASE_DIR, cred_path)

        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized successfully.")
        else:
            logger.warning(
                f"Firebase credentials not found at {cred_path}. "
                "Push notifications will be disabled."
            )
except Exception as e:
    logger.error(f"Failed to initialize Firebase Admin: {str(e)}")


def _clear_fcm_token(user_id):
    """Drop a stale FCM token so we don't keep retrying it forever."""
    try:
        User = get_user_model()
        User.objects.filter(pk=user_id).update(fcm_token=None)
        logger.info(f"Cleared stale FCM token for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to clear stale FCM token for user {user_id}: {e}")


@shared_task(
    name='auth_user.send_push_notification_task',
    bind=True,
    max_retries=3,
    default_retry_delay=15,
    acks_late=True,
)
def send_push_notification_task(self, user_id, title, body, data=None):
    """Celery task: send a push notification to a user by id.

    Stale tokens (UnregisteredError, SenderIdMismatchError) are cleared from
    the user row so we stop retrying them. Transient errors are retried up
    to 3 times with a 15s backoff.
    """
    if not firebase_admin._apps:
        logger.warning("Firebase Admin not initialized. Skipping push.")
        return False

    User = get_user_model()
    try:
        user = User.objects.only('id', 'fcm_token').get(pk=user_id)
    except User.DoesNotExist:
        logger.warning(f"FCM task: user {user_id} no longer exists")
        return False

    token = user.fcm_token
    if not token:
        return False

    str_data = {str(k): str(v) for k, v in (data or {}).items()}
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data=str_data,
        token=token,
    )

    try:
        response = messaging.send(message)
        logger.info(f"FCM sent to user {user_id}: {response}")
        return True
    except (messaging.UnregisteredError, messaging.SenderIdMismatchError) as e:
        # Token is permanently invalid — clear it so we stop retrying.
        logger.info(f"FCM token invalid for user {user_id} ({type(e).__name__}); clearing")
        _clear_fcm_token(user_id)
        return False
    except messaging.QuotaExceededError as exc:
        logger.warning(f"FCM quota exceeded for user {user_id}; retrying")
        raise self.retry(exc=exc, countdown=30)
    except Exception as exc:
        logger.error(f"FCM send failed for user {user_id}: {exc}")
        # Transient errors → bounded retry.
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return False


def send_push_notification(user, title, body, data=None):
    """Sync facade — enqueue an FCM task and return immediately.

    Previously this function called messaging.send() inline. WebSocket
    consumers and other request handlers blocked on the Google round-trip
    for every ride event, which serialized the channels workers under load.
    The Celery task does the network call.
    """
    user_id = getattr(user, 'id', None) or getattr(user, 'pk', None)
    if not user_id:
        return False
    if not getattr(user, 'fcm_token', None):
        # Cheap fast-path: if the in-memory user object already shows no
        # token, don't bother enqueuing.
        logger.info(f"User {user_id} has no FCM token. Skipping push notification.")
        return False

    try:
        send_push_notification_task.delay(user_id, title, body, data or {})
        return True
    except Exception as e:
        logger.error(f"Failed to enqueue FCM task for user {user_id}: {e}")
        return False
