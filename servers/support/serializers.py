from rest_framework import serializers

from servers.support.models import SupportMessage, SupportTicket


class SupportMessageSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportMessage
        fields = ('id', 'ticket', 'author', 'author_role', 'author_name', 'body', 'created_at')
        read_only_fields = ('id', 'author', 'author_role', 'author_name', 'created_at', 'ticket')

    def get_author_name(self, obj):
        if not obj.author:
            return obj.author_role
        return obj.author.full_name or obj.author.phone_number


class SupportTicketSerializer(serializers.ModelSerializer):
    messages = SupportMessageSerializer(many=True, read_only=True)

    class Meta:
        model = SupportTicket
        fields = (
            'id', 'user_id', 'trip_id', 'issue_type', 'status', 'description',
            'assigned_to', 'created_at', 'updated_at', 'resolved_at', 'messages',
        )
        read_only_fields = (
            'id', 'user_id', 'status', 'assigned_to', 'created_at',
            'updated_at', 'resolved_at', 'messages',
        )
