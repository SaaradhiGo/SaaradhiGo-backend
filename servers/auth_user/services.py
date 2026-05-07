import os
import logging
from django.conf import settings
import firebase_admin
from firebase_admin import credentials, messaging

logger = logging.getLogger(__name__)

# Initialize Firebase on module load if credentials exist
try:
    if not firebase_admin._apps:
        cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', 'firebase-credentials.json')
        # Check if absolute or relative
        if not os.path.isabs(cred_path):
            cred_path = os.path.join(settings.BASE_DIR, cred_path)
            
        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin initialized successfully.")
        else:
            logger.warning(f"Firebase credentials not found at {cred_path}. Push notifications will be disabled.")
except Exception as e:
    logger.error(f"Failed to initialize Firebase Admin: {str(e)}")


def send_push_notification(user, title, body, data=None):
    """
    Send a push notification via Firebase Cloud Messaging.
    """
    if not getattr(user, 'fcm_token', None):
        logger.info(f"User {user.id} has no FCM token. Skipping push notification.")
        return False
        
    if not firebase_admin._apps:
        logger.warning("Firebase Admin not initialized. Skipping push notification.")
        return False

    # Ensure data values are strings as required by FCM messaging.Message data payload
    str_data = {}
    if data:
        for k, v in data.items():
            str_data[str(k)] = str(v)

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=str_data,
        token=user.fcm_token,
    )
    
    try:
        response = messaging.send(message)
        logger.info(f"Successfully sent FCM to user {user.id}: {response}")
        return True
    except Exception as e:
        logger.error(f"Failed to send FCM to user {user.id}: {str(e)}")
        return False
