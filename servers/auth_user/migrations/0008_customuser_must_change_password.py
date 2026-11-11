from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auth_user', '0007_customuser_is_break_glass'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='must_change_password',
            field=models.BooleanField(default=False, help_text='Set on an account whose password was issued by someone else. Sign-in demands a new password first and issues no token until it is set.'),
        ),
    ]
