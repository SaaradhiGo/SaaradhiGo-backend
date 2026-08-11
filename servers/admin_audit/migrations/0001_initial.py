from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AdminAuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_label', models.CharField(blank=True, help_text='Human-readable snapshot of the actor at action time (survives later deletion/rename of the user).', max_length=255)),
                ('action', models.CharField(db_index=True, max_length=100)),
                ('target_type', models.CharField(db_index=True, max_length=64)),
                ('target_id', models.CharField(blank=True, db_index=True, max_length=64)),
                ('before', models.JSONField(blank=True, default=dict)),
                ('after', models.JSONField(blank=True, default=dict)),
                ('reason', models.TextField(blank=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, max_length=512)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='admin_actions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['target_type', 'target_id'], name='admin_audit_target_idx'),
                    models.Index(fields=['actor', '-created_at'], name='admin_audit_actor_idx'),
                ],
            },
        ),
    ]
