from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rider', '0020_favoriteplace_label'),
    ]

    operations = [
        migrations.AddField(
            model_name='favoriteplace',
            name='name',
            field=models.CharField(blank=True, default='', max_length=40),
        ),
    ]
