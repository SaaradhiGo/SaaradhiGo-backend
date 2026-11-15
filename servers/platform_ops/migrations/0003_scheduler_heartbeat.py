from django.db import migrations, models
from django.utils import timezone


def seed(apps, schema_editor):
    # Start the clock at migration time, so a scheduler that never starts after this deploy
    # still alerts two hours later instead of never.
    apps.get_model('platform_ops', 'SchedulerHeartbeat').objects.get_or_create(
        name='beat', defaults={'last_run_at': timezone.now()})


class Migration(migrations.Migration):

    dependencies = [
        ('platform_ops', '0002_launch_interest'),
    ]

    operations = [
        migrations.CreateModel(
            name='SchedulerHeartbeat',
            fields=[
                ('name', models.CharField(max_length=40, primary_key=True, serialize=False)),
                ('last_run_at', models.DateTimeField()),
            ],
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
