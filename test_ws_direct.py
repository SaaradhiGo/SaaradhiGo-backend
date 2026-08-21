import asyncio
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'base.settings')
django.setup()

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from channels.testing import WebsocketCommunicator
from base.asgi import application

User = get_user_model()
u = User.objects.first()
token = str(AccessToken.for_user(u))

async def test_communicator():
    print(f"Testing with User ID: {u.id}, Token: {token[:20]}...")
    
    communicator = WebsocketCommunicator(application, f"/ws/ride/request/?token={token}")
    connected, subprotocol = await communicator.connect()
    print(f"Connected: {connected}")
    if connected:
        response = await communicator.receive_json_from(timeout=5)
        print("Received message from server:", response)
        await communicator.disconnect()
    else:
        print("Failed to connect via WebsocketCommunicator")

if __name__ == "__main__":
    asyncio.run(test_communicator())
