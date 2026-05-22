from django.contrib import admin

from servers.support.models import SupportMessage, SupportTicket


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0
    readonly_fields = ('author_role', 'created_at')


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'issue_type', 'status', 'assigned_to', 'created_at')
    list_filter = ('status', 'issue_type')
    search_fields = ('user_id__phone_number', 'description')
    readonly_fields = ('created_at', 'updated_at', 'resolved_at')
    inlines = [SupportMessageInline]


@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'ticket', 'author_role', 'created_at')
    list_filter = ('author_role',)
