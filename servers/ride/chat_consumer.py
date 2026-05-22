"""In-trip chat WebSocket consumer.

Both rider and driver join the same trip-specific chat group. The
consumer persists every message to ChatMessage and broadcasts to the
peer in real time. System messages can be emitted by other services
(e.g. notifications.py emitting "Driver arrived") -- they land in the
same stream with `is_system=True`.

Connect: ws://host/ws/ride/trip/<trip_id>/chat/?token=<jwt>

Send:
    {"action": "send", "body": "Where are you?"}
    {"action": "read_all"}     -- mark all peer messages read

Receive:
    {"type": "message", "id": int, "sender_role": "driver|rider|system",
     "body": "...", "created_at": iso, "is_system": bool}
    {"type": "connection_established"}
"""
from __future__ import annotations

import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import AnonymousUser

logger = logging.getLogger(__name__)


class TripChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user', AnonymousUser())
        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        self.trip_id = self.scope['url_route']['kwargs']['trip_id']
        self.chat_group = f'trip_chat_{self.trip_id}'

        ok, role = await self._participant_role()
        if not ok:
            await self.close(code=4003)
            return
        self.role = role

        await self.channel_layer.group_add(self.chat_group, self.channel_name)
        await self.accept()
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'trip_id': self.trip_id,
            'role': self.role,
        }))

    async def disconnect(self, close_code):
        if hasattr(self, 'chat_group'):
            await self.channel_layer.group_discard(self.chat_group, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            await self.send(text_data=json.dumps({'type': 'error', 'reason': 'BAD_JSON'}))
            return

        action = data.get('action', 'send')
        if action == 'send':
            body = (data.get('body') or '').strip()
            if not body:
                return
            if len(body) > 2000:
                body = body[:2000]
            msg = await self._persist(body)
            if msg is None:
                return
            await self.channel_layer.group_send(
                self.chat_group,
                {
                    'type': 'chat.message',
                    'payload': {
                        'type': 'message',
                        'id': msg['id'],
                        'sender_role': msg['sender_role'],
                        'body': msg['body'],
                        'created_at': msg['created_at'],
                        'is_system': msg['is_system'],
                    },
                },
            )
        elif action == 'read_all':
            await self._mark_read_all()
        # other actions reserved

    async def chat_message(self, event):
        await self.send(text_data=json.dumps(event['payload']))

    # ---- DB helpers ---------------------------------------------------

    @database_sync_to_async
    def _participant_role(self):
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.select_related('driver_id', 'driver_id__user_id').get(id=self.trip_id)
        except Trip.DoesNotExist:
            return False, None
        if trip.user_id_id == self.user.id:
            return True, 'rider'
        if trip.driver_id and trip.driver_id.user_id_id == self.user.id:
            return True, 'driver'
        return False, None

    @database_sync_to_async
    def _persist(self, body):
        from servers.ride.models import ChatMessage, Trip
        try:
            trip = Trip.objects.get(id=self.trip_id)
        except Trip.DoesNotExist:
            return None
        # Refuse chat after the trip ended -- existing messages stay
        # readable via the REST history endpoint.
        status_code = (trip.status_id.status_code if trip.status_id else '').lower()
        if status_code in ('completed', 'cancelled'):
            return None
        msg = ChatMessage.objects.create(
            trip=trip,
            sender=self.user,
            sender_role=self.role,
            body=body,
        )
        return {
            'id': msg.id,
            'sender_role': msg.sender_role,
            'body': msg.body,
            'created_at': msg.created_at.isoformat(),
            'is_system': msg.is_system,
        }

    @database_sync_to_async
    def _mark_read_all(self):
        from servers.ride.models import ChatMessage
        from django.utils import timezone
        ChatMessage.objects.filter(
            trip_id=self.trip_id,
        ).exclude(sender_role=self.role).filter(read_at__isnull=True).update(
            read_at=timezone.now(),
        )
