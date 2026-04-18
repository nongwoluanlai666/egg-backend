from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wxcloudrun', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='eggfeedback',
            name='upstream_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='eggfeedback',
            name='upstream_confirmed_probability',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name='eggfeedback',
            name='upstream_confirmed_rank',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='eggfeedback',
            name='upstream_top1_probability',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name='eggfeedback',
            name='upstream_top1_species',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='eggfeedback',
            name='upstream_verification_status',
            field=models.CharField(
                choices=[
                    ('unknown', 'Unknown'),
                    ('matched', 'Matched'),
                    ('top10', 'Top 10'),
                    ('present', 'Present'),
                    ('mismatch', 'Mismatch'),
                    ('error', 'Error'),
                ],
                db_index=True,
                default='unknown',
                max_length=16,
            ),
        ),
    ]
