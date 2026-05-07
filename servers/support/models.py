from django.db import models
from django.contrib.auth import get_user_model

User=get_user_model()
class SupportTicket(models.Model):
    user_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='support_tickets')
    trip_id=models.ForeignKey('ride.Trip',on_delete=models.CASCADE,related_name='support_tickets',blank=True,null=True)
    issue_type=models.CharField(max_length=100)
    status=models.CharField(max_length=20,choices=[
        ('OPEN','Open'),('CLOSED','Closed')
    ],default='OPEN')
    description=models.TextField(blank=True,null=True)
    created_at=models.DateTimeField(auto_now_add=True)
    resolved_at=models.DateTimeField(blank=True,null=True)
    def __str__(self):
        return f'Ticket {self.id} - {self.issue_type}'
