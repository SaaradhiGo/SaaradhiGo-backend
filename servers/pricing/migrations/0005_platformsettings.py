from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0004_seed_vja_wgl_vtz'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PlatformSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(db_index=True, help_text='Stable setting key, e.g. "PLATFORM_COMMISSION_PERCENT".', max_length=256, unique=True)),
                ('value', models.TextField(help_text='Serialized value; typed by setting_type.')),
                ('setting_type', models.CharField(choices=[('decimal', 'Decimal'), ('integer', 'Integer'), ('string', 'String'), ('boolean', 'Boolean'), ('json', 'JSON')], default='string', max_length=20)),
                ('description', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='platform_settings_updates', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name_plural': 'Platform Settings',
                'ordering': ['-updated_at'],
            },
        ),
    ]
