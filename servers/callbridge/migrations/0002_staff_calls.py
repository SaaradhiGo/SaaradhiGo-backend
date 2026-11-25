"""25 Nov (owner ruling): calls from the console. ADDITIVE: two columns and a new role value."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('callbridge', '0001_initial')]

    operations = [
        migrations.AlterField(model_name='callsession', name='from_role',
                              field=models.CharField(choices=[('rider', 'Rider'), ('driver', 'Driver'), ('staff', 'Staff')],
                                                     max_length=6)),
        migrations.AddField(model_name='callsession', name='to_role',
                            field=models.CharField(blank=True, default='', max_length=6)),
        migrations.AddField(model_name='callsession', name='staff_id',
                            field=models.BigIntegerField(blank=True, null=True)),
    ]
