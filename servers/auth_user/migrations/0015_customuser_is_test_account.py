from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive: a test-account flag, default False (22 Nov)."""

    dependencies = [
        ('auth_user', '0014_closure_owed'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='is_test_account',
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
