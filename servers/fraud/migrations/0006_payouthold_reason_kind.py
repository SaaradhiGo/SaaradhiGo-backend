"""25 Nov (owner ruling): payout holds for four named reasons. ADDITIVE: a reason column with the
old meaning as its default, and the fraud flag becomes optional (a dispute hold has no flag)."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('fraud', '0005_riskflag_public_ref')]

    operations = [
        migrations.AddField(
            model_name='payouthold', name='reason_kind',
            field=models.CharField(choices=[('suspected_fraud', 'Suspected fraud'), ('dispute', 'Dispute'),
                                            ('compliance_check', 'Compliance check'),
                                            ('payout_details_verification', 'Payout details being verified')],
                                   default='suspected_fraud', max_length=32)),
        migrations.AlterField(
            model_name='payouthold', name='flag',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='payout_holds', to='fraud.riskflag')),
    ]
